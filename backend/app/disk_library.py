"""Bibliothèque des disques de sauvegarde.

Les films, séries et ebooks rangés sur un disque externe sous MEDIA/… (voir
Réglages : sous-dossiers) sont listés ici. Un disque CONNECTÉ est scanné ; son
contenu est gardé dans disk-library.json, à côté de settings.json, pour qu'un
disque débranché reste consultable (en lecture seule).

Un élément n'est reconnu que s'il est un ENFANT DIRECT d'un sous-dossier
MEDIA d'un disque connecté : jamais la racine du disque, jamais un dossier
parent, jamais un chemin hors des disques détectés.
"""
import json
import os
import threading
import time
from typing import Optional

from . import adult_media, config, disks

FILE_NAME = "disk-library.json"
REFRESH_SECONDS = 300  # un disque déjà inventorié n'est re-scanné qu'après ce délai (ou sur demande)
KIND_SUBDIR_ATTR = {
    "films": "dest_subdir_films",
    "series": "dest_subdir_series",
    "ebooks": "dest_subdir_ebooks",
}
# Noms de sous-dossiers reconnus en plus du sous-dossier configuré : d'autres outils
# rangent souvent les films dans MEDIA/FILMS, les séries dans MEDIA/SERIE, etc.
# La lecture accepte ces noms ; l'écriture vers un disque reste celle des réglages.
KIND_ALIASES = {
    "films": ("MEDIA/FILM", "MEDIA/FILMS"),
    "series": ("MEDIA/SERIES", "MEDIA/SERIE"),
    "ebooks": ("MEDIA/EBOOKS", "MEDIA/EBOOK"),
}
UNAVAILABLE_RETRY = 86400  # une fiche introuvable n'est retentée qu'après ce délai
_lock = threading.RLock()


def _path() -> str:
    return os.path.join(os.path.dirname(config.SETTINGS_FILE) or ".", FILE_NAME)


def _read() -> dict:
    try:
        with open(_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    path = _path()
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        pass  # la bibliothèque des disques reste utilisable sans cette copie


def kind_dirs(root: str) -> dict[str, list[str]]:
    """Dossiers MEDIA d'un disque, par type : {type: [chemins absolus]}.
    Le sous-dossier configuré vient en premier, suivi des noms courants."""
    root = os.path.normpath(root)
    out = {}
    for kind, attr in KIND_SUBDIR_ATTR.items():
        # Dossier propre à ce disque (version 48) avant le dossier global : les éléments qu'on y range
        # restent reconnus par la bibliothèque.
        configured = (config.settings.subdir_for(root, kind) or "").strip().strip("/")
        names = [configured] if configured else []
        names += [n for n in KIND_ALIASES[kind] if n not in names]
        if kind == "films":  # les copies de films adultes vivent dans leur propre sous-dossier
            adult_sub = adult_media.adult_subdir()
            if adult_sub and adult_sub not in names:
                names.append(adult_sub)
        out[kind] = [os.path.join(root, n) for n in names]
    return out


def locate(path: str) -> Optional[tuple[str, str]]:
    """(type, racine du disque) si `path` est un élément direct d'un sous-dossier
    MEDIA d'un disque CONNECTÉ, sinon None."""
    p = os.path.normpath(path)
    for d in disks.list_destination_disks():
        root = os.path.normpath(d["path"])
        for kind, subs in kind_dirs(root).items():
            for sub in subs:
                if p.startswith(sub + os.sep):
                    rest = p[len(sub) + 1:]
                    if rest and os.sep not in rest:
                        return kind, root
    return None


def meta_for(path: str) -> Optional[dict]:
    """Fiche enregistrée d'un élément de disque (titre, année, identifiant TMDB), si elle existe."""
    loc = locate(path)
    if not loc:
        return None
    with _lock:
        entry = _read().get(os.path.normpath(loc[1])) or {}
    return (entry.get("meta") or {}).get(os.path.normpath(path))


def locate_season(path: str) -> Optional[tuple[str, str]]:
    """(racine du disque, dossier de la série) si `path` est un dossier de saison
    (enfant direct d'une série MEDIA/SERIES d'un disque CONNECTÉ), sinon None."""
    p = os.path.normpath(path)
    for d in disks.list_destination_disks():
        root = os.path.normpath(d["path"])
        for sub in kind_dirs(root)["series"]:
            if p.startswith(sub + os.sep):
                parts = p[len(sub) + 1:].split(os.sep)
                if len(parts) == 2 and all(parts):
                    return root, os.path.join(sub, parts[0])
    return None


def _tree_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _scan(root: str) -> dict:
    items: dict[str, list] = {}
    for kind, directories in kind_dirs(root).items():
        found = []
        for directory in directories:
            if not os.path.isdir(directory):
                continue
            try:
                names = sorted(os.listdir(directory))
            except OSError:
                names = []
            folder = os.path.relpath(directory, root)
            for name in names:
                if name.startswith("."):
                    continue
                full = os.path.join(directory, name)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                is_dir = os.path.isdir(full) and not os.path.islink(full)
                found.append({
                    "name": name,
                    "path": full,
                    "folder": folder,
                    "is_dir": is_dir,
                    "size": _tree_size(full) if is_dir else st.st_size,
                    "mtime": int(st.st_mtime),
                })
        items[kind] = found
    return items


def _volatile(data: dict) -> str:
    """Empreinte de ce qui doit être enregistré : l'espace libre et l'heure de
    dernière vue changent à chaque appel et ne justifient pas une écriture."""
    stable = {k: {kk: vv for kk, vv in v.items() if kk not in ("last_seen", "free_bytes")}
              for k, v in data.items()}
    return json.dumps(stable, sort_keys=True)


# Scans des disques, EN ARRIÈRE-PLAN (version 43). Un scan complet (tous les dossiers, tous les
# fichiers) peut durer plusieurs minutes sur un gros disque ou un partage réseau. Avant, la requête
# attendait la fin du scan : l'interface restait vide, et après 30 s elle abandonnait. Pire, la
# requête suivante, 20 s plus tard, relançait le même scan (le marquage « scanné » n'était écrit
# qu'à la fin). Désormais : un seul scan par racine à la fois ; la réponse attend au plus
# SCAN_WAIT_SECONDS, puis part avec l'état connu et `scanning: true` (l'interface relit alors toutes
# les 3 secondes, comme pour les fiches TMDB).
SCAN_WAIT_SECONDS = 4.0

_scan_lock = threading.Lock()
_scans: dict[str, threading.Thread] = {}        # racine -> scan en cours
_scans_prets: dict[str, dict] = {}              # racine -> {"items", "at", "force"}, à enregistrer


def _demarrer_scan(root: str, force: bool) -> threading.Thread:
    """Lance le scan d'une racine dans un thread. Appelée avec _scan_lock tenu."""
    def travail() -> None:
        try:
            items = _scan(root)
            with _scan_lock:
                _scans_prets[root] = {"items": items, "at": time.time(), "force": force}
        except Exception:
            pass  # un scan raté est refait à la prochaine lecture
        finally:
            with _scan_lock:
                _scans.pop(root, None)

    thread = threading.Thread(target=travail, name=f"scan {root}", daemon=True)
    _scans[root] = thread
    thread.start()
    return thread


def scans_en_cours() -> bool:
    """Vrai tant qu'un disque est en cours de scan."""
    with _scan_lock:
        return bool(_scans)


def refresh(force: bool = False, names_on_1fichier: frozenset = frozenset(),
            attente: float | None = None) -> list[dict]:
    """Met à jour les disques connectés (scan si la copie a plus de REFRESH_SECONDS
    ou si `force`) et renvoie tous les disques connus, connectés en premier.
    Un scan dure au plus `attente` secondes dans cet appel (SCAN_WAIT_SECONDS par défaut) ;
    au-delà, il continue en arrière-plan et sera fusionné à un appel suivant."""
    now = time.time()
    connected = disks.list_destination_disks()
    free_now = {os.path.normpath(d["path"]): d.get("free_bytes") for d in connected}
    with _lock:
        known = _read()
    lances: list[threading.Thread] = []
    with _scan_lock:
        for d in connected:
            root = os.path.normpath(d["path"])
            if root in _scans:          # déjà en cours : on attend ce scan, on n'en relance pas un autre
                lances.append(_scans[root])
                continue
            entry = known.get(root, {})
            if force or now - entry.get("scanned_at", 0) > REFRESH_SECONDS:
                lances.append(_demarrer_scan(root, force))
    fin = time.monotonic() + (SCAN_WAIT_SECONDS if attente is None else attente)
    for thread in lances:
        thread.join(max(0.0, fin - time.monotonic()))
    with _scan_lock:
        prets = {root: pret for root, pret in _scans_prets.items()}
        _scans_prets.clear()

    def _keep_meta(root_entry: dict, present: set[str], retry_unavailable: bool) -> dict:
        kept = {}
        for path, meta in (root_entry.get("meta") or {}).items():
            if path not in present:
                continue  # élément disparu : sa fiche part avec lui
            if retry_unavailable and meta.get("unavailable"):
                continue  # « Rafraîchir » retente les fiches introuvables
            kept[path] = meta
        return kept

    with _lock:
        data = _read()
        before = _volatile(data)
        connected_roots = set()
        for d in connected:
            root = os.path.normpath(d["path"])
            connected_roots.add(root)
            entry = data.get(root, {})
            entry.setdefault("first_seen", now)
            entry.update({"path": root, "label": os.path.basename(root) or root,
                          "last_seen": now, "free_bytes": free_now[root], "connected": True})
            if root in prets:
                pret = prets[root]
                entry.update({"items": pret["items"], "scanned_at": pret["at"]})
                present = {it["path"] for lst in pret["items"].values() for it in lst}
                entry["meta"] = _keep_meta(entry, present, pret["force"])
            data[root] = entry
        for root, entry in data.items():
            if root not in connected_roots:
                entry["connected"] = False
        if prets or _volatile(data) != before:
            _write(data)

    out = []
    for root, entry in data.items():
        items = {}
        metas = entry.get("meta") or {}
        for kind, lst in (entry.get("items") or {}).items():
            items[kind] = [{**it,
                            "on_1fichier": it.get("name") in names_on_1fichier,
                            "adult": kind == "films" and adult_media.is_adult_folder(it.get("folder")),
                            "meta": _public_meta(metas.get(it["path"]))}
                           for it in lst]
        out.append({
            "path": root,
            "label": entry.get("label") or os.path.basename(root),
            "connected": bool(entry.get("connected")),
            "free_bytes": free_now.get(root, entry.get("free_bytes")),
            "first_seen": entry.get("first_seen"),
            "last_seen": entry.get("last_seen"),
            "scanned_at": entry.get("scanned_at"),
            "items": items,
            "subdirs": {k: [os.path.relpath(v, root) for v in vs] for k, vs in kind_dirs(root).items()},
        })
    out.sort(key=lambda d: (not d["connected"], d["label"].lower()))
    return out


def _public_meta(meta: dict | None) -> dict | None:
    """La fiche telle qu'affichée : une fiche introuvable n'est pas une fiche."""
    if not meta or meta.get("unavailable"):
        return None
    return meta


def store_metas(records: list[tuple[str, str, dict]]) -> None:
    """Enregistre plusieurs fiches (racine, chemin, fiche) avec UNE seule réécriture du fichier.
    Réécrire le fichier pour chaque élément coûtait cher sur une grande bibliothèque (P4, version 42)."""
    if not records:
        return
    with _lock:
        data = _read()
        changed = False
        for root, path, meta in records:
            entry = data.get(root)
            if entry is None:
                continue
            record = dict(meta)
            record.setdefault("checked_at", time.time())
            entry.setdefault("meta", {})[path] = record
            changed = True
        if changed:
            _write(data)


def store_meta(root: str, path: str, meta: dict) -> None:
    """Enregistre la fiche d'un élément d'un disque (y compris « introuvable »)."""
    store_metas([(root, path, meta)])


def pending_items(now: float, tmdb_enabled: bool) -> list[tuple[str, str, dict]]:
    """Éléments d'un disque CONNECTÉ sans fiche, ou dont la fiche introuvable doit être retentée."""
    with _lock:
        data = _read()
    out = []
    for root, entry in data.items():
        if not entry.get("connected"):
            continue
        metas = entry.get("meta") or {}
        for kind, lst in (entry.get("items") or {}).items():
            if kind != "ebooks" and not tmdb_enabled:
                continue  # sans TMDB, films et séries restent sans fiche (pas d'« introuvable »)
            for it in lst:
                meta = metas.get(it["path"])
                if meta is not None and (not meta.get("unavailable")
                                         or now - meta.get("checked_at", 0) < meta.get("retry", UNAVAILABLE_RETRY)):
                    continue
                out.append((kind, root, it))
    return out


def renamed(root: str, old_path: str, new_path: str) -> None:
    """Met à jour la copie enregistrée après un renommage sur le disque : l'élément
    prend son nouveau nom et sa fiche est retirée, pour que TMDB le retrouve
    tout de suite sous son nouveau nom (au lieu d'attendre le prochain scan)."""
    with _lock:
        data = _read()
        entry = data.get(os.path.normpath(root))
        if entry is None:
            return
        for lst in (entry.get("items") or {}).values():
            for it in lst:
                if it.get("path") == old_path:
                    it["path"] = new_path
                    it["name"] = os.path.basename(new_path)
        (entry.get("meta") or {}).pop(old_path, None)
        _write(data)


def update_meta(root: str, path: str, fields: dict) -> bool:
    """Complète la fiche EXISTANTE d'un élément (ex. jaquette importée). Sans fiche,
    rien n'est créé : la fiche complète sera calculée normalement."""
    with _lock:
        data = _read()
        entry = data.get(os.path.normpath(root))
        if entry is None or path not in (entry.get("meta") or {}):
            return False
        entry["meta"][path].update(fields)
        _write(data)
    return True


def forget(path: str) -> bool:
    """Retire un disque hors ligne de la liste. Un disque connecté ne peut pas être oublié."""
    root = os.path.normpath(path)
    if any(os.path.normpath(d["path"]) == root for d in disks.list_destination_disks()):
        raise ValueError("ce disque est connecté : il ne peut pas être oublié.")
    with _lock:
        data = _read()
        if root not in data:
            return False
        data.pop(root)
        _write(data)
    return True


def moved(root: str, old_path: str, new_path: str) -> None:
    """Après un déplacement dans le même disque (catégorie Adultes) : l'élément garde sa fiche TMDB
    et son dossier relatif est mis à jour. Contrairement à renamed(), le nom ne change pas."""
    with _lock:
        data = _read()
        entry = data.get(os.path.normpath(root))
        if entry is None:
            return
        rel = os.path.relpath(os.path.dirname(new_path), os.path.normpath(root))
        for lst in (entry.get("items") or {}).values():
            for it in lst:
                if it.get("path") == old_path:
                    it["path"] = new_path
                    it["folder"] = rel
        meta = entry.get("meta") or {}
        if old_path in meta:
            meta[new_path] = meta.pop(old_path)
        _write(data)
