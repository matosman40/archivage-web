"""Fiches des éléments des disques de sauvegarde.

- films et séries : identifiant, titre, année, affiche, genres, note, durée,
  résumé (TMDB) ;
- ebooks : titre et auteur d'après le nom du dossier, formats, nombre de livres,
  jaquette tirée du premier livre qui en a une (même logique que 1fichier).

Les fiches se calculent EN ARRIÈRE-PLAN : la liste des disques ne attend jamais
TMDB. Elles sont gardées dans disk-library.json (par chemin d'élément), donc un
disque débranché garde ses affiches et ses notes.
"""
import asyncio
import os
import time

from . import adult_media, config, ebook_online, disk_library, ebooks as ebooks_mod, onefichier, tmdb

BATCH = 40          # éléments traités par tour
CONCURRENCY = 4     # requêtes TMDB en parallèle
_state = {"running": False, "task": None}  # task : garde une référence (P11) pour que la tâche ne soit pas ramassée


def running() -> bool:
    return _state["running"]


async def _tmdb_meta(kind: str, name: str, adult: bool = False) -> dict | None:
    """Fiche TMDB d'un film ou d'une série, d'après son nom de dossier ou de fichier."""
    if kind == "films":
        match = await tmdb.find_movie(name, adult=adult)
        full = await tmdb.movie_details(match["id"]) if match else None
    else:
        match = await tmdb.find_tv(name)
        full = await tmdb.tv_details(match["id"]) if match else None
    if not match:
        return None
    full = full or {}
    meta = {
        "tmdb_id": match["id"],
        "title": full.get("title") or match.get("title"),
        "original_title": full.get("original_title"),
        "year": full.get("year") or match.get("year"),
        "poster_url": full.get("poster_url") or match.get("poster_url"),
        "backdrop_url": full.get("backdrop_url"),
        "genres": full.get("genres") or match.get("genres") or [],
        "rating": full.get("rating") or match.get("rating"),
        "runtime": full.get("runtime"),
        "summary": full.get("summary") or match.get("summary"),
        "tagline": full.get("tagline"),
        "tmdb_url": full.get("tmdb_url"),
    }
    if kind == "series":
        meta["seasons"] = full.get("seasons")
        meta["episodes"] = full.get("episodes")
    return meta


def _ebook_meta(item: dict) -> dict | None:
    """Fiche d'un dossier d'ebooks (ou d'un livre isolé), sans réseau."""
    unit = item["path"]
    if item["is_dir"]:
        files = onefichier.ebook_files(unit)
        if not files:
            return None
        return onefichier.build_ebook_meta(unit, onefichier.ebook_key(unit), files)
    author, title = ebooks_mod.split_author_title(os.path.splitext(item["name"])[0])
    ext = os.path.splitext(item["name"])[1].lstrip(".").upper()
    return {
        "title": title, "original_title": None, "author": author, "year": None,
        "release_date": None, "runtime": None, "genres": [], "rating": None, "votes": None,
        "summary": None, "tagline": None, "backdrop_url": None, "tmdb_url": None,
        "poster_url": None, "books": 1, "formats": [ext] if ext else [],
        "first_book": item["name"], "cover_source": None, "checked_at": time.time(),
    }


async def _online_ebook_cover(item: dict, meta: dict) -> dict:
    """Pas de jaquette dans les livres : on la cherche en ligne (Open Library)."""
    key = onefichier.ebook_key(item["path"])
    data = await ebook_online.find_cover(key, meta.get("title") or item["name"], meta.get("author"))
    if data:
        name = await asyncio.to_thread(onefichier.store_cover_bytes, key, data)
        meta["poster_url"] = f"/api/1fichier/covers/{name}"
        meta["cover_source"] = "Open Library"
    return meta


async def _fetch(kind: str, item: dict) -> dict | None:
    if kind == "ebooks":
        meta = await asyncio.to_thread(_ebook_meta, item)
        if meta is not None and not meta.get("poster_url"):
            meta = await _online_ebook_cover(item, meta)
        return meta
    adult = kind == "films" and adult_media.is_adult_folder(item.get("folder"))
    return await _tmdb_meta(kind, item["name"], adult=adult)


async def run() -> None:
    """Complète les fiches manquantes des disques CONNECTÉS, par tours de BATCH."""
    try:
        for _round in range(1000):  # garde-fou : chaque tour réduit la liste à traiter
            todo = await asyncio.to_thread(disk_library.pending_items, time.time(),
                                           config.settings.tmdb_enabled)
            if not todo:
                break
            sem = asyncio.Semaphore(CONCURRENCY)
            fiches: list[tuple[str, str, dict]] = []

            async def one(kind: str, root: str, item: dict) -> None:
                async with sem:
                    try:
                        meta = await _fetch(kind, item)
                        if meta is None:
                            meta = {"unavailable": True}
                    except Exception:
                        # Panne réseau : on réessaie plus tôt, sans bloquer les autres.
                        meta = {"unavailable": True, "retry": 3600}
                fiches.append((root, item["path"], meta))

            await asyncio.gather(*(one(*entry) for entry in todo[:BATCH]))
            # Un seul enregistrement par tour, et non un par élément (P4).
            await asyncio.to_thread(disk_library.store_metas, fiches)
    finally:
        _state["running"] = False


def start() -> bool:
    """Lance le calcul en arrière-plan s'il n'est pas déjà en cours."""
    if _state["running"]:
        return False
    _state["running"] = True
    _state["task"] = asyncio.ensure_future(run())
    return True


async def kick() -> bool:
    """Lance le calcul s'il reste des fiches à faire, puis dit s'il tourne.

    Un calcul n'est lancé que s'il a du travail. Lancé à vide, il laisserait le drapeau
    levé (la tâche ne s'exécute qu'après la réponse) : la liste relirait alors les
    disques toutes les 3 s, et chaque relecture relancerait un calcul vide."""
    todo = await asyncio.to_thread(disk_library.pending_items, time.time(), config.settings.tmdb_enabled)
    if todo:
        start()
    return running()
