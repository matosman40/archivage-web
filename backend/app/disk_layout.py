"""Dossiers des disques externes (version 48).

Trois choses, toutes limitées à un disque CONNECTÉ et à son intérieur :
  - choisir, pour chaque disque et chaque type (films, séries, ebooks), le dossier où
    les copies sont rangées ; sans choix, le sous-dossier global de Réglages s'applique ;
  - parcourir les sous-dossiers d'un disque pour le choisir ;
  - déplacer des éléments dans le MÊME disque vers un autre dossier de la bibliothèque.

Un déplacement dans le même disque est un renommage (os.rename) : rien n'est copié, donc
rien n'est relu. Il n'est permis qu'entre deux dossiers reconnus par la bibliothèque (ceux
du disque pour ce type) : un élément rangé ailleurs n'apparaîtrait plus dans la liste.
"""
import os
from typing import Optional

from . import adult_media, config, disk_library, disk_moves, disks, jobs, security
from .logs import log_action

MAX_LISTING = 300
MAX_DEPTH = 4


class LayoutError(Exception):
    """Erreur présentable à l'utilisateur, avec le code HTTP à renvoyer."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def clean_folder(folder: Optional[str]) -> str:
    """Chemin relatif propre (« Ciné/Films »). Vide = la racine du disque."""
    morceaux = [m for m in str(folder or "").strip().strip("/").split("/") if m]
    if any(m in (".", "..") for m in morceaux):
        raise LayoutError("Dossier : « . » et « .. » ne sont pas permis.")
    if len(morceaux) > MAX_DEPTH:
        raise LayoutError(f"Dossier trop profond ({MAX_DEPTH} niveaux au maximum).")
    return "/".join(morceaux)


def connected_root(path: str) -> str:
    root = os.path.normpath(str(path or ""))
    if not any(os.path.normpath(d["path"]) == root for d in disks.list_destination_disks()):
        raise LayoutError("Ce disque n'est pas connecté : rebranche-le puis rafraîchis la liste.", 403)
    return root


def _inside(root: str, target: str, strict: bool = True) -> bool:
    """Vrai si `target` est dans `root` une fois les liens symboliques résolus.
    Un lien qui sort du disque est donc refusé. strict=False accepte la racine elle-même."""
    real_root = os.path.realpath(root)
    real_target = os.path.realpath(target)
    if real_target == real_root:
        return not strict
    return real_target.startswith(real_root + os.sep)


def browse(path: str, folder: str = "", unlocked: bool = False) -> dict:
    """Sous-dossiers directs d'un dossier du disque (ni fichiers, ni liens, ni dossiers cachés).
    Le dossier des adultes n'est listé qu'avec une session déverrouillée."""
    root = connected_root(path)
    rel = clean_folder(folder)
    current = os.path.join(root, rel) if rel else root
    if not os.path.isdir(current) or not _inside(root, current, strict=False):
        raise LayoutError("Dossier introuvable sur ce disque.", 404)
    if adult_media.is_adult_folder(rel) and not unlocked:
        raise LayoutError("Dossier introuvable sur ce disque.", 404)
    try:
        names = sorted(os.listdir(current), key=str.lower)
    except OSError as exc:
        raise LayoutError(f"Impossible de lire ce dossier : {exc}", 400)
    folders = []
    truncated = False
    for name in names:
        if name.startswith("."):
            continue
        full = os.path.join(current, name)
        if os.path.islink(full) or not os.path.isdir(full):
            continue
        child = "/".join(p for p in (rel, name) if p)
        if adult_media.is_adult_folder(child) and not unlocked:
            continue
        if len(folders) >= MAX_LISTING:
            truncated = True
            break
        folders.append({"name": name, "folder": child})
    parent = None if not rel else "/".join(rel.split("/")[:-1])
    return {
        "root": root,
        "label": os.path.basename(root) or root,
        "folder": rel,
        "parent": parent,
        "folders": folders,
        "truncated": truncated,
    }


def set_folder(path: str, kind: str, folder: str, create: bool = False, unlocked: bool = False) -> dict:
    """Fixe le dossier de destination d'un type sur ce disque. Un dossier vide rétablit le
    sous-dossier global de Réglages."""
    if kind not in config.DISK_FOLDER_KINDS:
        raise LayoutError("Type inconnu : films, séries ou ebooks.")
    root = connected_root(path)
    rel = clean_folder(folder)
    buckets = {r: dict(k) for r, k in (config.settings.disk_folders or {}).items()}
    if not rel:
        buckets.get(root, {}).pop(kind, None)
    else:
        target = os.path.join(root, rel)
        if not _inside(root, target):
            raise LayoutError("Ce dossier sort du disque choisi : refusé.", 400)
        if adult_media.is_adult_folder(rel) and not unlocked:
            raise LayoutError("Dossier introuvable sur ce disque.", 404)
        if security.is_under_source_dirs(target):
            raise LayoutError("Ce dossier chevauche un dossier source : refusé par sécurité.", 403)
        if not os.path.isdir(target):
            if not create:
                raise LayoutError("Ce dossier n'existe pas sur le disque. Coche « créer le dossier » pour le créer.", 404)
            if not config.settings.dry_run:
                try:
                    os.makedirs(target, exist_ok=True)
                except OSError as exc:
                    raise LayoutError(f"Impossible de créer ce dossier : {exc}", 400)
        buckets.setdefault(root, {})[kind] = rel
    buckets = {r: k for r, k in buckets.items() if k}
    try:
        config.update_settings({"disk_folders": buckets})
    except ValueError as exc:
        raise LayoutError(str(exc), 400)
    return {
        "root": root,
        "kind": kind,
        "folder": rel or None,
        "effective": config.settings.subdir_for(root, kind),
    }


def _nearest_existing(path: str) -> str:
    ancestor = path
    while not os.path.exists(ancestor) and os.path.dirname(ancestor) != ancestor:
        ancestor = os.path.dirname(ancestor)
    return ancestor


def _meme_volume(path: str, other: str) -> bool:
    """Vrai si les deux chemins sont sur le même volume : seul un renommage les relie sans copie."""
    try:
        return os.stat(path).st_dev == os.stat(other).st_dev
    except OSError:
        return False


def _occupied_by_job(path: str) -> bool:
    """Un élément qu'une copie ou un déplacement en attente ou en cours utilise ne bouge pas."""
    for job in list(jobs.JOBS.values()):  # copie : le registre peut changer pendant la lecture
        if job.status in ("pending", "running") and path in job.items:
            return True
    return False


def plan_move(items: list[str], target_rel: str, unlocked: bool) -> dict:
    """Prépare le déplacement dans le même disque, sans rien modifier. Lève LayoutError si
    un contrôle échoue ; rien n'est alors touché."""
    if not items:
        raise LayoutError("Aucun élément sélectionné.")
    located = []
    for raw in items:
        path = os.path.normpath(raw)
        loc = disk_library.locate(path)
        if not loc:
            raise LayoutError(f"Élément absent d'un disque connecté : {path}. Rebranche le disque puis rafraîchis la liste.", 403)
        located.append((path, loc[0], loc[1]))
    roots = {r for _p, _k, r in located}
    kinds = {k for _p, k, _r in located}
    if len(roots) != 1:
        raise LayoutError("Sélectionne des éléments d'un seul disque.")
    if len(kinds) != 1:
        raise LayoutError("Sélectionne des éléments d'un seul type (films, séries ou ebooks).")
    root, kind = roots.pop(), kinds.pop()
    rel = clean_folder(target_rel)
    if not rel:
        raise LayoutError("Choisis un dossier de destination.")
    target = os.path.normpath(os.path.join(root, rel))
    allowed = [os.path.normpath(p) for p in disk_library.kind_dirs(root)[kind]]
    if target not in allowed:
        raise LayoutError("Ce dossier n'est pas reconnu par la bibliothèque pour ce type : choisis un dossier MEDIA du disque.")
    if adult_media.is_adult_folder(os.path.relpath(target, root)) and not unlocked:
        raise LayoutError("Élément introuvable. Rafraîchis la liste.", 404)

    ancestor = _nearest_existing(target)
    plan, unchanged = [], []
    for path, _k, _r in located:
        if os.path.dirname(path) == target:
            unchanged.append(path)
            continue
        if target == path or target.startswith(path + os.sep):
            raise LayoutError(f"Impossible de placer un élément dans lui-même : {path}.")
        name = os.path.basename(path)
        dest = os.path.join(target, name)
        if os.path.lexists(dest):
            raise LayoutError(f"Un élément porte déjà ce nom dans ce dossier : {name}. Rien n'a été déplacé.", 409)
        if _occupied_by_job(path):
            raise LayoutError(f"Une copie ou un déplacement utilise déjà {name}. Attends sa fin. Rien n'a été déplacé.", 409)
        if not config.settings.dry_run and not security.source_is_removable(path):
            raise LayoutError(f"Impossible de déplacer cet élément : {name}. Le disque est probablement monté en lecture seule. Rien n'a été déplacé.", 409)
        if not _meme_volume(path, ancestor):
            raise LayoutError(f"{name} est sur un autre volume que ce dossier : utilise la copie, pas le déplacement.", 409)
        plan.append({"source": path, "destination": dest, "name": name})
    adult = any(adult_media.disk_path_is_adult(p["source"], root) or adult_media.is_adult_folder(
        os.path.relpath(target, root)) for p in plan) if plan else False
    return {"root": root, "kind": kind, "target": target, "plan": plan, "unchanged": unchanged, "adult": adult}


def execute_move(result: dict) -> dict:
    """Applique un plan (plan_move). En simulation, rien n'est déplacé."""
    dry = config.settings.dry_run
    kind, root, target = result["kind"], result["root"], result["target"]
    moved, errors = [], []
    if not dry and result["plan"]:
        os.makedirs(target, exist_ok=True)
    for entry in result["plan"]:
        src, dest = entry["source"], entry["destination"]
        if dry:
            log_action("SIMULE", "DEPLACEMENT", f"{src} -> {dest}")
            moved.append(dest)
            continue
        try:
            os.rename(src, dest)
        except OSError as exc:
            errors.append({"path": src, "error": str(exc)})
            log_action("ECHEC", "DEPLACEMENT", f"{src} -> {dest} ({exc})")
            disk_moves.record("disk_move", kind, src, dest, "erreur", f"Déplacement refusé : {exc}",
                          adult=result.get("adult", False))
            continue
        disk_library.moved(root, src, dest)
        adult_media.move_mark(src, dest)
        log_action("OK", "DEPLACEMENT", f"{src} -> {dest}")
        disk_moves.record("disk_move", kind, src, dest, "ok", "Déplacé dans le même disque (renommage, sans copie)",
                          adult=result.get("adult", False))
        moved.append(dest)
    return {
        "ok": not errors,
        "dry_run": dry,
        "target": target,
        "moved": moved,
        "errors": errors,
        "unchanged": len(result["unchanged"]),
    }
