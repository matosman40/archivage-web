"""Identifications faites dans l'application, pour les films et séries du serveur.

Rien n'est renommé sur le serveur : Plex, Sonarr et Radarr gardent leurs noms.
L'identification (titre, année, identifiant TMDB) sert à :
- afficher le bon titre et la bonne année dans la bibliothèque,
- chercher la fiche TMDB à partir du titre identifié,
- nommer les COPIES envoyées vers un disque (et l'identifiant TMDB pour 1fichier).

Stockage : library-identities.json, à côté de settings.json.
Clés : un film = son chemin de fichier ; une série = son nom de dossier
(une même série peut exister dans plusieurs sources).
"""
import json
import os
import threading
import time
from typing import Optional

from . import config

FILE_NAME = "library-identities.json"
_lock = threading.RLock()


def _path() -> str:
    return os.path.join(os.path.dirname(config.SETTINGS_FILE) or ".", FILE_NAME)


# Cache de lecture (P1, version 42) : apply() relisait le fichier pour chaque élément d'une liste.
# Relu seulement si le fichier a changé (date et taille), et invalidé à chaque écriture.
_cache: dict = {"signature": None, "data": {}}


def _read() -> dict:
    """Copie superficielle du contenu : les appelants peuvent modifier le dict renvoyé."""
    try:
        st = os.stat(_path())
    except OSError:
        return {}
    signature = (st.st_mtime_ns, st.st_size)
    if _cache["signature"] != signature:
        try:
            with open(_path(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        _cache["data"] = data if isinstance(data, dict) else {}
        _cache["signature"] = signature
    return dict(_cache["data"])


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
        pass
    finally:
        _cache["signature"] = None  # la prochaine lecture repart du fichier


def key_for(kind: str, item: str) -> str:
    """Clé d'un élément : chemin normalisé pour un film, nom de dossier pour une série."""
    if kind == "films":
        return "films|" + os.path.normpath(item)
    return "series|" + os.path.basename(os.path.normpath(item))


def get(kind: str, item: str) -> Optional[dict]:
    with _lock:
        return _read().get(key_for(kind, item))


def set_many(kind: str, items: list[str], record: dict) -> None:
    with _lock:
        data = _read()
        for item in items:
            data[key_for(kind, item)] = {**record, "identified_at": time.time()}
        _write(data)


def apply(kind: str, items: list[dict]) -> None:
    """Complète EN PLACE la liste d'un type : titre et année identifiés, drapeau."""
    with _lock:
        data = _read()
    if not data:
        return
    for it in items:
        ident = data.get(key_for(kind, it["path"] if kind == "films" else it["name"]))
        if not ident:
            continue
        it["identified"] = True
        it["search_title"] = ident.get("title") or it.get("title")
        it["search_year"] = ident.get("year")
        it["title"] = ident.get("title") or it.get("title")
        if ident.get("year"):
            it["year"] = ident["year"]
            it["year_source"] = "identifié"
        if ident.get("tmdb_id"):
            it["tmdb_id"] = ident["tmdb_id"]


def tmdb_id_for(kind: str, item: str) -> Optional[int]:
    ident = get(kind, item)
    return int(ident["tmdb_id"]) if ident and ident.get("tmdb_id") else None


def dest_name(kind: str, item: str) -> Optional[str]:
    """Nom de la COPIE d'un élément identifié : « Titre (année) » + extension d'origine
    pour un fichier. None si l'élément n'est pas identifié (la copie garde son nom)."""
    ident = get(kind, item)
    if not ident or not ident.get("name"):
        return None
    base = ident["name"]
    if kind == "films" and os.path.isfile(item):
        ext = os.path.splitext(item)[1]
        return base + ext
    return base


def move(kind: str, old: str, new: str) -> None:
    """Suit un film renommé sur le serveur : son identification reste attachée au fichier."""
    with _lock:
        data = _read()
        record = data.pop(key_for(kind, old), None)
        if record is not None:
            data[key_for(kind, new)] = record
            _write(data)
