"""Suivi des copies et déplacements (version 48).

Chaque élément copié ou déplacé (vers un disque, depuis un disque, ou d'un dossier à un
autre du même disque) laisse une trace dans disk-moves.json, à côté de settings.json :
état (en cours, terminé, erreur, ignoré, partiel, interrompu), chemins source et
destination, date et détail. La trace survit au redémarrage du serveur, comme l'index 1fichier.

Le suivi n'interrompt jamais un transfert : une erreur d'écriture de la trace est ignorée.
En simulation (DRY_RUN), rien n'est consigné : aucun élément réel n'a bougé.
"""
import json
import os
import threading
import time
import uuid
from typing import Optional

from . import config

FILE_NAME = "disk-moves.json"
HISTORY_MAX = 500          # au-delà, les plus anciennes entrées TERMINÉES sont retirées
_lock = threading.RLock()

OPERATION_LABEL = {"copy": "copie", "move": "déplacement", "disk_move": "déplacement sur le disque"}
# Événements de tâche -> état du suivi
_STATUS_BY_EVENT = {
    "item_start": "en_cours",
    "item_done": "ok",
    "item_error": "erreur",
    "item_partial": "partiel",
    "item_skipped": "ignore",
}
STATUS_LABEL = {
    "en_cours": "En cours",
    "ok": "Terminé",
    "erreur": "Erreur",
    "partiel": "Partiel",
    "ignore": "Ignoré",
    "interrompu": "Interrompu",
}
FINISHED = ("ok", "erreur", "partiel", "ignore", "interrompu")


def _path() -> str:
    return os.path.join(os.path.dirname(config.SETTINGS_FILE) or ".", FILE_NAME)


def _read() -> list:
    try:
        with open(_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _write(entries: list) -> None:
    path = _path()
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(entries, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        pass  # le suivi reste utilisable sans cette copie


def _prune(entries: list) -> list:
    """Garde au plus HISTORY_MAX entrées ; les plus anciennes TERMINÉES partent en premier."""
    excedent = len(entries) - HISTORY_MAX
    if excedent <= 0:
        return entries
    for entry in sorted(entries, key=lambda e: e.get("updated", 0)):
        if excedent <= 0:
            break
        if entry.get("status") in FINISHED:
            entries.remove(entry)
            excedent -= 1
    return entries


def _base(path: str) -> str:
    return os.path.basename(str(path).rstrip("/")) or str(path)


def _destination_of(job, item: str) -> str:
    dest_dir = job.dest_dirs.get(item, job.destination) or ""
    return os.path.join(dest_dir, job.dest_names.get(item) or _base(item))


def _find(entries: list, key: str) -> Optional[dict]:
    for entry in entries:
        if entry.get("key") == key:
            return entry
    return None


def _upsert(key: str, fields: dict) -> dict:
    """Crée ou met à jour l'entrée `key`. Appelé sous _lock."""
    entries = _read()
    entry = _find(entries, key)
    now = time.time()
    if entry is None:
        entry = {"key": key, "at": now}
        entries.append(entry)
    entry.update(fields)
    entry["updated"] = now
    _write(_prune(entries))
    return entry


def on_job_event(job, event: dict) -> None:
    """Appelé pour chaque événement d'une tâche de copie ou de déplacement (depuis Job.emit).
    Ne lève jamais d'exception : le suivi ne doit pas casser un transfert."""
    try:
        if config.settings.dry_run:
            return
        status = _STATUS_BY_EVENT.get(event.get("type"))
        name = event.get("item")
        if status is None or not name:
            return
        # Index nom -> chemin, bâti une fois par tâche : évite de parcourir la liste à chaque événement.
        index = getattr(job, "_suivi_index", None)
        if index is None:
            index = {}
            for candidate in job.items:
                index.setdefault(_base(candidate), candidate)
            job._suivi_index = index
        item = index.get(name)
        if item is None:
            return
        fields = {
            "job_id": job.id,
            "operation": OPERATION_LABEL.get(job.operation, job.operation),
            "kind": job.media_type,
            "name": name,
            "source": item,
            "destination": _destination_of(job, item),
            "status": status,
            "adult": bool(getattr(job, "adult", False)),
        }
        detail = event.get("error") or event.get("message") or event.get("reason") or ""
        if status == "ok":
            detail = "Vérifié par empreintes SHA-256" + (" ; source retirée" if job.operation == "move" else "")
        fields["detail"] = detail
        with _lock:
            _upsert(f"{job.id}|{item}", fields)
    except Exception:
        pass


def record(operation: str, kind: str, source: str, destination: str, status: str, detail: str = "",
           adult: bool = False) -> None:
    """Consigne une opération faite hors tâche (déplacement dans le même disque)."""
    try:
        if config.settings.dry_run:
            return
        with _lock:
            _upsert(f"direct|{uuid.uuid4().hex}", {
                "job_id": None,
                "operation": OPERATION_LABEL.get(operation, operation),
                "kind": kind,
                "name": _base(source),
                "source": source,
                "destination": destination,
                "status": status,
                "detail": detail,
                "adult": bool(adult),
            })
    except Exception:
        pass


def marquer_interrompus() -> int:
    """Au démarrage, une entrée restée « en cours » appartient à un processus disparu : elle
    passe à « interrompue ». Une tâche reprise après redémarrage la remettra en cours."""
    with _lock:
        entries = _read()
        n = 0
        for entry in entries:
            if entry.get("status") == "en_cours":
                entry["status"] = "interrompu"
                entry["detail"] = "Arrêt du serveur pendant le transfert ; la reprise est automatique si la tâche était en attente."
                entry["updated"] = time.time()
                n += 1
        if n:
            _write(entries)
        return n


def list_entries(status: Optional[str] = None, limit: int = 200, unlocked: bool = False) -> dict:
    """Entrées les plus récentes d'abord, avec le compte par état. Sans session déverrouillée,
    les transferts de films adultes sont retirés (ni compte, ni nom, ni chemin)."""
    with _lock:
        entries = _read()
    if not unlocked:
        entries = [e for e in entries if not e.get("adult")]
    counts = {s: 0 for s in STATUS_LABEL}
    for e in entries:
        counts[e.get("status", "")] = counts.get(e.get("status", ""), 0) + 1
    chosen = [e for e in entries if status in (None, "", "tous") or
              (status == "terminés" and e.get("status") in FINISHED) or
              (status == "problèmes" and e.get("status") in ("erreur", "partiel", "interrompu")) or
              e.get("status") == status]
    chosen.sort(key=lambda e: e.get("updated", 0), reverse=True)
    items = [_public(e) for e in chosen[: max(1, min(limit, HISTORY_MAX))]]
    return {"items": items, "counts": counts, "total": len(entries), "shown": len(items)}


def _public(entry: dict) -> dict:
    return {
        "id": entry.get("key"),
        "job_id": entry.get("job_id"),
        "operation": entry.get("operation", ""),
        "kind": entry.get("kind", ""),
        "name": entry.get("name", ""),
        "source": entry.get("source", ""),
        "destination": entry.get("destination", ""),
        "status": entry.get("status", ""),
        "status_label": STATUS_LABEL.get(entry.get("status", ""), entry.get("status", "")),
        "detail": entry.get("detail", ""),
        "adult": bool(entry.get("adult")),
        "at": entry.get("at"),
        "updated": entry.get("updated"),
    }


def clear_finished() -> int:
    """Efface l'historique terminé. Les transferts en cours ne sont jamais effacés."""
    with _lock:
        entries = _read()
        keep = [e for e in entries if e.get("status") not in FINISHED]
        removed = len(entries) - len(keep)
        if removed:
            _write(keep)
    return removed


def active_count() -> int:
    with _lock:
        return sum(1 for e in _read() if e.get("status") == "en_cours")
