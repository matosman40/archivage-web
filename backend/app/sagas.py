"""Sagas de films : les films possédés, regroupés par collection TMDB
(ex. « Retour vers le futur », « Matrix »), avec l'affiche de la saga et la liste
COMPLÈTE de ses films, y compris ceux qu'on ne possède pas.

- Un film identifié (TMDB) renvoie sa collection dans sa fiche ; la collection donne
  son affiche et tous ses films.
- Les données TMDB sont gardées dans sagas.json (à côté de settings.json) : une fois
  récupérées, elles ne sont plus redemandées, même après un redémarrage.
- Le calcul se fait EN ARRIÈRE-PLAN, comme les fiches des disques : la liste ne
  attend jamais TMDB (le drapeau `running` indique qu'il reste des fiches à récupérer).
"""
import asyncio
import json
import os
import threading
import time

from . import config, tmdb

FILE_NAME = "sagas.json"
UNAVAILABLE_RETRY = 86400  # une fiche introuvable n'est retentée qu'après ce délai
_lock = threading.RLock()
_state: dict = {"task": None}


def _path() -> str:
    return os.path.join(os.path.dirname(config.SETTINGS_FILE) or ".", FILE_NAME)


def _read() -> dict:
    try:
        with open(_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("movies", {})
    data.setdefault("collections", {})
    return data


def _update(fn) -> None:
    with _lock:
        data = _read()
        fn(data)
        tmp = f"{_path()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, _path())
        except OSError:
            pass  # les sagas restent calculables à la demande sans ce cache


def running() -> bool:
    task = _state["task"]
    return task is not None and not task.done()


def _missing(data: dict, movie_ids: list[int]) -> tuple[list[int], list[int]]:
    """Films dont la fiche manque (ou doit être retentée) et collections à récupérer."""
    now = time.time()
    need_movies = []
    for tid in movie_ids:
        rec = data["movies"].get(str(tid))
        if rec is None:
            need_movies.append(tid)
        elif rec.get("unavailable") and now - rec.get("checked_at", 0) > UNAVAILABLE_RETRY:
            need_movies.append(tid)
    known_collections = {str(c) for c in data["collections"]}
    wanted = {m["collection"]["id"] for tid in movie_ids
              if (m := data["movies"].get(str(tid))) and m.get("collection")}
    need_collections = [c for c in wanted if str(c) not in known_collections]
    return need_movies, need_collections


def start(movie_ids: list[int]) -> bool:
    """Lance la récupération des fiches TMDB manquantes (en arrière-plan)."""
    if not config.settings.tmdb_enabled or running():
        return False
    with _lock:
        need_movies, need_collections = _missing(_read(), movie_ids)
    if not need_movies and not need_collections:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    _state["task"] = loop.create_task(_refresh(need_movies, need_collections))
    return True


async def _refresh(movie_ids: list[int], collection_ids: list[int]) -> None:
    for tid in movie_ids:
        try:
            details = await tmdb.movie_details(tid, strict=True)
        except Exception:
            # Erreur passagère (réseau, limite TMDB, 5xx) : le film n'est PAS marqué
            # « introuvable » ; il sera retenté au prochain passage.
            continue
        now = time.time()
        if not details:
            _update(lambda d, t=tid: d["movies"].__setitem__(str(t), {"unavailable": True, "checked_at": now}))
            continue
        record = {
            "title": details.get("title"), "year": details.get("year"),
            "poster_url": details.get("poster_url"), "collection": details.get("collection"),
            "checked_at": now,
        }
        _update(lambda d, t=tid, r=record: d["movies"].__setitem__(str(t), r))
    # Les collections des films connus (y compris ceux de ce tour)
    data = await asyncio.to_thread(_read)
    _, pending = _missing(data, [int(k) for k in data["movies"] if str(k).isdigit()])
    for cid in set(collection_ids) | set(pending):
        coll = await tmdb.collection(cid)
        if coll:
            _update(lambda d, c=cid, v=coll: d["collections"].__setitem__(str(c), v))


def groups(films: list[dict]) -> list[dict]:
    """Sagas dont au moins un film est possédé.

    `films` : [{"tmdb_id", "where", "label"}] — une entrée par possession (serveur,
    disque, 1fichier). Chaque saga liste TOUS ses films, avec ceux possédés."""
    data = _read()
    owned: dict[int, list[dict]] = {}
    for f in films:
        if f.get("tmdb_id") is None:
            continue
        owned.setdefault(int(f["tmdb_id"]), []).append({"where": f["where"], "label": f["label"]})
    by_collection: dict[int, list[int]] = {}
    for tid in owned:
        rec = data["movies"].get(str(tid)) or {}
        coll = rec.get("collection")
        if coll:
            by_collection.setdefault(int(coll["id"]), []).append(tid)
    out = []
    for cid, members in by_collection.items():
        coll = data["collections"].get(str(cid))
        if not coll:
            continue  # collection pas encore récupérée : elle apparaîtra au prochain passage
        parts = [{**p, "owned": owned.get(p.get("tmdb_id"), [])} for p in coll.get("parts") or []]
        covered = [p for p in parts if p["owned"]]
        poster = coll.get("poster_url") or next((p.get("poster_url") for p in covered if p.get("poster_url")), None)
        out.append({
            "id": cid, "name": coll.get("name"), "poster_url": poster, "overview": coll.get("overview"),
            "total": len(parts), "owned_count": len(covered), "missing_count": len(parts) - len(covered),
            "parts": parts,
        })
    out.sort(key=lambda g: (g["name"] or "").lower())
    return out
