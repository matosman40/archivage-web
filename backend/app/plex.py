"""Intégration Plex : statut vu/non vu, progression des séries et métadonnées
(année, date d'ajout, dernière lecture, durée, jaquette, résumé, genres, note).

Interroge l'API Plex une fois (mise en cache), normalise les titres et fait une
correspondance approximative. Le cache n'est rafraîchi que sur demande explicite
(POST /api/plex/refresh), pas à chaque recherche.

Deux subtilités gérées ici :
- deux films peuvent porter le même titre (remake) : on compare l'année quand
  elle est connue, pour ne pas attribuer les métadonnées de l'un à l'autre ;
- une série partiellement vue n'est PAS « non vue » : on expose la progression.
"""
import os
import re
import time
from urllib.parse import quote

import httpx

from . import config, naming

# movies / shows : dict[titre_normalisé] -> liste de métadonnées (remakes possibles)
_cache: dict = {"loaded_at": 0.0, "movies": {}, "shows": {}}

_RELEASE_TAG_RE = re.compile(
    r"\b(2160p|1080p|720p|576p|480p|4k|uhd|hdr(10)?|dv|bluray|blu-ray|web-?dl|webrip|"
    r"dvdrip|hdtv|x264|x265|hevc|h264|h265|aac|dts(-?hd)?|ddp?5?1?|atmos|truehd|multi|"
    r"vostfr|vff?2?|truefrench|french|remux|repack|proper|saison|season)\b",
    re.IGNORECASE,
)
_PAREN_YEAR_RE = re.compile(r"\(\s*(19|20)\d{2}\s*\)")
_NONALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_title(raw: str) -> str:
    """Clé de comparaison. Seule l'année ENTRE PARENTHÈSES est retirée : une année
    fait partie du titre quand elle n'est pas entre parenthèses ("Blade Runner 2049")."""
    s = raw.lower().replace("_", " ").replace(".", " ")
    s = _PAREN_YEAR_RE.sub(" ", s)
    s = _RELEASE_TAG_RE.sub(" ", s)
    s = _NONALNUM_RE.sub(" ", s)
    return s.strip()


async def _get_json(client: httpx.AsyncClient, path: str) -> dict:
    sep = "&" if "?" in path else "?"
    url = f"{config.settings.plex_url}{path}{sep}X-Plex-Token={config.settings.plex_token}"
    resp = await client.get(url, headers={"Accept": "application/json"}, timeout=config.settings.plex_timeout)
    resp.raise_for_status()
    return resp.json()


def invalidate_cache() -> None:
    """Force un nouveau POST /api/plex/refresh à repartir de zéro, typiquement
    après un changement d'URL ou de token Plex depuis les réglages."""
    _cache["loaded_at"] = 0.0
    _cache["movies"] = {}
    _cache["shows"] = {}


def _rating_of(item: dict):
    rating = item.get("audienceRating")
    if rating is None:
        rating = item.get("rating")
    try:
        return round(float(rating), 1) if rating is not None else None
    except (TypeError, ValueError):
        return None


def _genres_of(item: dict) -> list[str]:
    return [
        g.get("tag") for g in (item.get("Genre") or [])
        if isinstance(g, dict) and g.get("tag")
    ]


def _int_or_none(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def refresh_cache() -> dict:
    if not config.settings.plex_enabled:
        return status()

    movies: dict[str, list[dict]] = {}
    shows: dict[str, list[dict]] = {}

    async with httpx.AsyncClient() as client:
        sections_data = await _get_json(client, "/library/sections")
        sections = sections_data.get("MediaContainer", {}).get("Directory", [])

        for section in sections:
            key = section.get("key")
            stype = section.get("type")
            if stype not in ("movie", "show") or key is None:
                continue

            data = await _get_json(client, f"/library/sections/{key}/all")
            for item in data.get("MediaContainer", {}).get("Metadata", []):
                title = item.get("title")
                if not title:
                    continue
                base = {
                    "title": title,
                    "year": _int_or_none(item.get("year")),
                    "rating": _rating_of(item),
                    "genres": _genres_of(item),
                    "thumb": item.get("thumb"),
                    "summary": item.get("summary"),
                    "added_at": _int_or_none(item.get("addedAt")),
                    "last_viewed": _int_or_none(item.get("lastViewedAt")),
                }

                if stype == "movie":
                    duration_ms = _int_or_none(item.get("duration"))
                    movies.setdefault(normalize_title(title), []).append({
                        **base,
                        "watched": (item.get("viewCount") or 0) >= 1,
                        "duration_min": round(duration_ms / 60000) if duration_ms else None,
                        "content_rating": item.get("contentRating"),
                    })
                else:
                    leaf = _int_or_none(item.get("leafCount")) or 0
                    viewed = _int_or_none(item.get("viewedLeafCount")) or 0
                    shows.setdefault(normalize_title(title), []).append({
                        **base,
                        "seasons": _int_or_none(item.get("childCount")),
                        "episodes": leaf,
                        "viewed_episodes": viewed,
                        "watched": leaf > 0 and viewed >= leaf,
                    })

    _cache["movies"] = movies
    _cache["shows"] = shows
    _cache["loaded_at"] = time.time()
    return status()


def status() -> dict:
    movies = [m for lst in _cache["movies"].values() for m in lst]
    shows = [s for lst in _cache["shows"].values() for s in lst]
    return {
        "enabled": config.settings.plex_enabled,
        "loaded": _cache["loaded_at"] > 0,
        "movie_titles": len(movies),
        "series_titles": len(shows),
        "movies_watched": sum(1 for m in movies if m["watched"]),
        "series_watched": sum(1 for s in shows if s["watched"]),
        "last_refresh": _cache["loaded_at"],
    }


def _lookup(index: dict, norm: str, year: int | None):
    """Trouve la métadonnée Plex correspondant à un titre normalisé.

    Correspondance exacte d'abord, puis par inclusion (titres d'au moins 4
    caractères, pour éviter les faux positifs sur les titres très courts).
    Si l'année est connue et qu'aucun candidat ne la porte, on renonce plutôt
    que de prendre le mauvais film (remake)."""
    if not norm:
        return None
    candidates = list(index.get(norm, []))
    if not candidates and len(norm) >= 4:
        for title, lst in index.items():
            if len(title) >= 4 and (norm in title or title in norm):
                candidates.extend(lst)
    if not candidates:
        return None
    if year:
        same_year = [c for c in candidates if c.get("year") == year]
        if same_year:
            return same_year[0]
        if all(c.get("year") for c in candidates):
            return None
    return candidates[0]


def _poster(thumb):
    return f"/api/plex/poster?path={quote(thumb)}" if thumb else None


def get_movie_plex_info(path: str, year: int | None = None):
    """Métadonnées Plex d'un film, ou None si introuvable (statut inconnu)."""
    if not config.settings.plex_enabled or _cache["loaded_at"] == 0:
        return None

    base = os.path.basename(path)
    title, file_year = naming.guess_title_year(base)
    year = year or file_year
    meta = _lookup(_cache["movies"], normalize_title(title), year)
    if meta is None:
        parent_title, parent_year = naming.guess_title_year(os.path.basename(os.path.dirname(path)))
        meta = _lookup(_cache["movies"], normalize_title(parent_title), parent_year or year)
    if meta is None:
        return None

    return {
        "watched": meta["watched"],
        "rating": meta.get("rating"),
        "genres": meta.get("genres") or [],
        "poster_url": _poster(meta.get("thumb")),
        "summary": meta.get("summary"),
        "year": meta.get("year"),
        "added_at": meta.get("added_at"),
        "last_viewed": meta.get("last_viewed"),
        "duration_min": meta.get("duration_min"),
        "content_rating": meta.get("content_rating"),
    }


def get_series_plex_info(name: str, year: int | None = None):
    """Métadonnées et progression Plex d'une série, ou None si introuvable."""
    if not config.settings.plex_enabled or _cache["loaded_at"] == 0:
        return None
    title, name_year = naming.guess_title_year(name)
    meta = _lookup(_cache["shows"], normalize_title(title), year or name_year)
    if meta is None:
        return None
    return {
        "watched": meta["watched"],
        "viewed_episodes": meta["viewed_episodes"],
        "plex_episodes": meta["episodes"],
        "plex_seasons": meta.get("seasons"),
        "rating": meta.get("rating"),
        "genres": meta.get("genres") or [],
        "poster_url": _poster(meta.get("thumb")),
        "summary": meta.get("summary"),
        "year": meta.get("year"),
        "added_at": meta.get("added_at"),
        "last_viewed": meta.get("last_viewed"),
    }


_CHEMIN_PLEX = re.compile(r"/[A-Za-z0-9_\-./%~+=,:]*")


def chemin_affiche_valide(path: str) -> bool:
    """Un chemin Plex simple (« /library/metadata/… ») et rien d'autre. Un `@`, un `?`, un `#`
    ou un `..` permettrait de changer l'hôte appelé : le jeton partirait vers un autre serveur."""
    return bool(_CHEMIN_PLEX.fullmatch(path or "")) and ".." not in path


async def get_poster_bytes(path: str):
    """Octets d'une affiche Plex (proxy côté serveur : le token n'atteint
    jamais le navigateur). `path` = chemin relatif Plex (champ 'thumb')."""
    if not config.settings.plex_enabled:
        return None
    if not chemin_affiche_valide(path):
        raise ValueError("chemin d'affiche Plex invalide")
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{config.settings.plex_url.rstrip('/')}{path}",
            params={"X-Plex-Token": config.settings.plex_token},
            timeout=config.settings.plex_timeout,
        )
        resp.raise_for_status()
        return resp.content, resp.headers.get("content-type", "image/jpeg")
