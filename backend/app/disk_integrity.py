"""Intégrité des disques de sauvegarde (version 30).

- « Empreintes » : un SHA-256 par fichier des dossiers MEDIA d'un disque CONNECTÉ. Elles sont gardées
  sur le volume de configuration (disk-manifests/), donc consultables même disque débranché. Un fichier
  déjà empreint, dont la taille et la date n'ont pas changé, n'est pas relu.
- « Vérifier » : relit tous les fichiers et compare. Un fichier dont la taille et la date sont
  identiques mais dont le contenu a changé est une CORRUPTION PROBABLE (dégradation du support),
  signalée à part d'un fichier simplement modifié. Un fichier absent est signalé aussi.

Les deux opérations sont longues (plusieurs heures sur un gros disque) : elles tournent en arrière-plan,
une seule à la fois, et ne démarrent pas pendant une copie ou une restauration.
"""
import asyncio
import hashlib
import json
import os
import tempfile
import threading
import time

from . import config, disk_library, disks, jobs
from .logs import log_action

CHUNK = 8 * 1024 * 1024
REPORT_LIST_LIMIT = 200  # nombre d'exemples gardés par liste dans le rapport (les compteurs sont exacts)
_lock = threading.Lock()
_TASKS: set = set()  # références gardées : une tâche sans référence peut être ramassée en cours de route
_state: dict = {"running": None, "done": 0, "total": 0, "root": None, "started_at": None, "error": None}


class IntegrityError(Exception):
    pass


def _dir() -> str:
    return os.path.join(os.path.dirname(config.SETTINGS_FILE) or ".", "disk-manifests")


def _manifest_path(root: str) -> str:
    return os.path.join(_dir(), root.strip("/").replace("/", "_") + ".json")


def _read_manifest(root: str) -> dict:
    try:
        with open(_manifest_path(root), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_manifest(root: str, data: dict) -> None:
    os.makedirs(_dir(), mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=_dir(), prefix=".manifest-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, _manifest_path(root))


def _media_files(root: str) -> dict[str, tuple[int, float]]:
    """Fichiers des dossiers MEDIA du disque : {chemin relatif: (taille, date de modification)}."""
    out: dict[str, tuple[int, float]] = {}
    for dirs in disk_library.kind_dirs(root).values():
        for base in dirs:
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in filenames:
                    if name.startswith(".") or name.endswith(".part"):
                        continue
                    full = os.path.join(dirpath, name)
                    try:
                        st = os.stat(full)
                    except OSError:
                        continue
                    out[os.path.relpath(full, root)] = (st.st_size, st.st_mtime)
    return out


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_connected(root: str) -> str:
    root = os.path.normpath(root or "")
    connected = {os.path.normpath(d["path"]) for d in disks.list_destination_disks()}
    if root not in connected:
        raise IntegrityError(f"disque non détecté ou non monté : {root or '(aucun)'}")
    busy = [j for j in jobs.JOBS.values() if j.status in ("pending", "running")]
    if busy:
        raise IntegrityError("une copie, un déplacement ou une restauration est en cours : réessaie après.")
    return root


def build_blocking(root: str) -> dict:
    files = _media_files(root)
    previous = _read_manifest(root)
    old = previous.get("files", {})
    with _lock:
        _state.update(done=0, total=len(files))
    out = {}
    for n, (rel, (size, mtime)) in enumerate(sorted(files.items()), 1):
        prev = old.get(rel)
        if prev and prev.get("size") == size and prev.get("mtime") == mtime and prev.get("sha256"):
            digest = prev["sha256"]
        else:
            digest = _sha256(os.path.join(root, rel))
        out[rel] = {"size": size, "mtime": mtime, "sha256": digest}
        with _lock:
            _state["done"] = n
    manifest = {"root": root, "built_at": time.time(), "files": out, "last_verify": previous.get("last_verify")}
    _write_manifest(root, manifest)
    log_action("OK", "EMPREINTES", f"{root} : {len(out)} fichier(s) empreinté(s)")
    return {"files": len(out), "built_at": manifest["built_at"]}


def verify_blocking(root: str) -> dict:
    manifest = _read_manifest(root)
    expected = manifest.get("files") or {}
    if not expected:
        raise IntegrityError("aucune empreinte pour ce disque : lance d'abord « Empreintes ».")
    current = _media_files(root)
    with _lock:
        _state.update(done=0, total=len(expected))
    counts = {"ok": 0, "corrupt": 0, "modified": 0, "missing": 0}
    lists: dict[str, list[str]] = {"corrupt": [], "modified": [], "missing": []}
    for n, (rel, prev) in enumerate(sorted(expected.items()), 1):
        if rel not in current:
            kind = "missing"
        else:
            size, mtime = current[rel]
            if _sha256(os.path.join(root, rel)) == prev.get("sha256"):
                kind = "ok"
            elif size == prev.get("size") and mtime == prev.get("mtime"):
                kind = "corrupt"   # même taille, même date, contenu différent : le support s'est dégradé
            else:
                kind = "modified"  # le fichier a été modifié depuis l'empreinte
        counts[kind] += 1
        if kind in lists and len(lists[kind]) < REPORT_LIST_LIMIT:
            lists[kind].append(rel)
        with _lock:
            _state["done"] = n
    new_files = sorted(set(current) - set(expected))
    report = {"at": time.time(), **counts, "new": len(new_files), "new_examples": new_files[:REPORT_LIST_LIMIT],
              **{f"{k}_examples": v for k, v in lists.items()}}
    manifest["last_verify"] = report
    _write_manifest(root, manifest)
    level = "ALERTE" if counts["corrupt"] or counts["missing"] else "OK"
    log_action(level, "EMPREINTES", f"{root} : {counts['ok']} OK, {counts['corrupt']} corrompu(s), "
                                    f"{counts['modified']} modifié(s), {counts['missing']} absent(s), {len(new_files)} nouveau(x)")
    return report


async def start(root: str, mode: str) -> bool:
    """Lance « build » ou « verify » en arrière-plan. Renvoie False si une opération tourne déjà."""
    if mode not in ("build", "verify"):
        raise IntegrityError("mode inconnu")
    root = _require_connected(root)
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=mode, root=root, done=0, total=0, started_at=time.time(), error=None)
    task = asyncio.ensure_future(_run(root, mode))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return True


async def _run(root: str, mode: str) -> None:
    try:
        if mode == "build":
            await asyncio.to_thread(build_blocking, root)
        else:
            await asyncio.to_thread(verify_blocking, root)
    except Exception as exc:
        with _lock:
            _state["error"] = str(exc)
        log_action("ALERTE", "EMPREINTES", f"{root} : {mode} interrompu ({exc})")
    finally:
        with _lock:
            _state["running"] = None


def status() -> dict:
    roots = {}
    base = _dir()
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            if not name.endswith(".json"):
                continue
            data = _read_manifest_file(os.path.join(base, name))
            root = data.get("root")
            if root:
                roots[root] = {
                    "built_at": data.get("built_at"),
                    "files": len(data.get("files") or {}),
                    "size": sum(f.get("size", 0) for f in (data.get("files") or {}).values()),
                    "last_verify": data.get("last_verify"),
                }
    with _lock:
        running = dict(_state)
    return {"roots": roots, "running": running if running["running"] else None}


def _read_manifest_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
