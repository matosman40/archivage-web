"""Médias adultes (versions 34 et 36).

Un film est ADULTE si :
- il se trouve dans un dossier « Dossiers films adultes » (Réglages › Dossiers sources), ou
- il a été marqué adulte à la main (case « Contenu adulte » de la fenêtre d'identification).

Ce qui change pour un film adulte :
- la recherche TMDB inclut les titres pour adultes (include_adult), comme pour les autres films ;
- une copie vers un disque part dans le sous-dossier « Destination adulte » (MEDIA/ADULTE par défaut) ;
- l'entrée 1fichier porte le drapeau. Le dossier distant reste celui des films (nommé par TMDB) ;
- il n'est pas rapproché de Plex.

Affichage : sans session déverrouillée, ces films n'apparaissent dans AUCUNE liste et ne peuvent être
ni envoyés, ni restaurés, ni identifiés, ni supprimés. Le déverrouillage passe par un code vérifié par le
serveur (voir adult_auth).

Marques manuelles : library-adult.json, à côté de settings.json, par chemin normalisé.
"""
import json
import os
import tempfile
import threading

from . import config

FILE_NAME = "library-adult.json"
_lock = threading.RLock()


def _path() -> str:
    return os.path.join(os.path.dirname(config.SETTINGS_FILE) or ".", FILE_NAME)


def _read() -> set[str]:
    try:
        with open(_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return set()
    paths = data.get("paths") if isinstance(data, dict) else None
    return {p for p in paths if isinstance(p, str)} if isinstance(paths, list) else set()


def _write(paths: set[str]) -> None:
    path = _path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".adult-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"paths": sorted(paths)}, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _norm(path: str) -> str:
    return os.path.normpath(path)


def adult_roots() -> list[str]:
    return [_norm(d) for d in config.settings.source_dirs_adultes]


def adult_subdir() -> str:
    """Sous-dossier de copie des adultes. Vide : les adultes partent dans le dossier films."""
    return (config.settings.dest_subdir_adultes or "").strip().strip("/")


def marked_paths() -> set[str]:
    with _lock:
        return _read()


def mentions_adult(text: str) -> bool:
    """Le texte cite un dossier adulte, le sous-dossier adulte, ou un film marqué : une ligne de
    journal ou une notification qui le contient ne doit pas sortir en clair (version 42)."""
    if not text:
        return False
    if any(root and root in text for root in adult_roots()):
        return True
    sub = adult_subdir()
    if sub and sub in text:
        return True
    return any(m and m in text for m in marked_paths())


def set_marked(paths: list[str], flag: bool) -> None:
    with _lock:
        current = _read()
        for path in paths:
            if flag:
                current.add(_norm(path))
            else:
                current.discard(_norm(path))
        _write(current)


def in_adult_roots(path: str) -> bool:
    p = _norm(path)
    return any(p == root or p.startswith(root + os.sep) for root in adult_roots())


def is_adult_path(path: str, marked: set[str] | None = None) -> bool:
    """Film adulte d'après son chemin sur le serveur (dossier adulte ou marque manuelle)."""
    p = _norm(path)
    marks = marked if marked is not None else marked_paths()
    return p in marks or in_adult_roots(p)


def is_adult_folder(folder: str | None) -> bool:
    """Dossier relatif d'un disque (ex. « MEDIA/ADULTE ») : une copie y est classée adulte."""
    sub = adult_subdir()
    if not folder or not sub:
        return False
    rel = folder.strip("/")
    return rel == sub or rel.startswith(sub + "/")


def is_adult_item(item: dict, marked: set[str] | None = None) -> bool:
    """Élément adulte : déjà marqué (scan, disque, index 1fichier), ou dont le chemin d'origine
    est adulte. Couvre les entrées 1fichier créées avant le drapeau « adult » (version 34).
    `marked` : marques déjà lues ; une boucle sur une liste les lit une seule fois (voir filter_visible)."""
    if item.get("adult"):
        return True
    # Le chemin courant compte aussi : une marque posée après la mise en cache d'une liste
    # (recherche, bibliothèque) doit masquer l'élément sans attendre le prochain rafraîchissement.
    for chemin in (item.get("original_path"), item.get("path")):
        if chemin and is_adult_path(chemin, marked):
            return True
    return False


def visible(item: dict, unlocked: bool, marked: set[str] | None = None) -> bool:
    """Un élément adulte n'est visible que si la session est déverrouillée (voir adult_auth)."""
    return unlocked or not is_adult_item(item, marked)


def filter_visible(items: list[dict], unlocked: bool) -> list[dict]:
    # Les marques sont lues une fois pour toute la liste : les lire par élément coûtait un
    # aller-retour disque par entrée (173 ms pour 3 000 entrées 1fichier, contre 5 ms).
    marks = None if unlocked else marked_paths()
    return [it for it in items if visible(it, unlocked, marks)]


def folder_has_adult(path: str, marks: set[str] | None = None) -> bool:
    """Dossier qui contient un film adulte, ou qui se trouve dans un dossier adulte. Copier ou
    supprimer ce dossier entier emporterait le film adulte : l'opération est donc refusée sans session."""
    d = _norm(path)
    marks = marked_paths() if marks is None else marks
    prefix = d + os.sep
    if any(m.startswith(prefix) for m in marks):
        return True
    if in_adult_roots(d):
        return True
    return any(r.startswith(prefix) for r in adult_roots())


def hidden_paths(paths: list[str], unlocked: bool) -> list[str]:
    """Chemins de films adultes refusés pour une session verrouillée, y compris les dossiers
    qui contiennent un film adulte (version 42)."""
    if unlocked:
        return []
    marks = marked_paths()
    return [p for p in paths if is_adult_path(p, marks)
            or (os.path.isdir(p) and folder_has_adult(p, marks))]


def any_adult(paths: list[str]) -> bool:
    marks = marked_paths()
    return any(is_adult_path(p, marks) for p in paths)


def disk_path_is_adult(path: str, root: str) -> bool:
    """Élément d'un disque : adulte s'il est rangé dans le sous-dossier adulte (ex. MEDIA/ADULTE/Film)."""
    rel = os.path.relpath(os.path.normpath(path), os.path.normpath(root))
    return is_adult_folder(os.path.dirname(rel))


def move_mark(old: str, new: str) -> None:
    """Suit un film renommé sur le serveur : la marque adulte reste attachée au fichier."""
    with _lock:
        current = _read()
        if _norm(old) in current:
            current.discard(_norm(old))
            current.add(_norm(new))
            _write(current)
