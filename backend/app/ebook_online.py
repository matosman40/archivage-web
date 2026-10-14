"""Jaquettes de livres trouvées en ligne, pour les ebooks dont les fichiers n'en ont pas.

Source : Open Library (openlibrary.org), sans clé API. C'est l'équivalent de TMDB
pour les livres : on cherche par titre et auteur tirés du nom du dossier, puis on
prend la jaquette du premier résultat qui en a une.

Politesse : une seule requête à la fois, un User-Agent identifiable, et une
recherche infructueuse n'est pas refaite avant NOT_FOUND_RETRY. Une panne réseau
n'est pas mise en cache : on réessaiera plus tard.
Désactivable : EBOOK_ONLINE_COVERS=false.
"""
import asyncio
import io
import time
from typing import Optional

import httpx

from . import config

SEARCH_URL = "https://openlibrary.org/search.json"
COVER_URL = "https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"
USER_AGENT = "Archivage-mediatheque/1.0 (usage personnel ; jaquettes d'ebooks)"
NOT_FOUND_RETRY = 86400          # un livre introuvable n'est retenté qu'après un jour
MAX_IMAGE_BYTES = 5 * 1024 * 1024

_not_found: dict[str, float] = {}  # clé -> instant de la recherche infructueuse
_lock: Optional[asyncio.Lock] = None  # une requête à la fois vers Open Library (créé par boucle)
_lock_loop = None


def _get_lock() -> asyncio.Lock:
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock, _lock_loop = asyncio.Lock(), loop
    return _lock


class _NotFound(Exception):
    pass


def enabled() -> bool:
    return bool(config.settings.ebook_online_covers)


def _looks_like_image(data: bytes) -> bool:
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img.verify()
        return True
    except Exception:
        return False


async def find_cover(key: str, title: str, author: Optional[str]) -> Optional[bytes]:
    """Octets de la jaquette trouvée en ligne, ou None (désactivé, introuvable, réseau)."""
    if not enabled() or not title:
        return None
    last = _not_found.get(key)
    if last is not None and time.time() - last < NOT_FOUND_RETRY:
        return None
    async with _get_lock():
        try:
            async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=10,
                                         follow_redirects=True) as client:
                params = {"title": title, "limit": 5, "fields": "key,title,author_name,cover_i"}
                if author:
                    params["author"] = author
                resp = await client.get(SEARCH_URL, params=params)
                resp.raise_for_status()
                docs = resp.json().get("docs") or []
                cover_id = next((d.get("cover_i") for d in docs if d.get("cover_i")), None)
                if cover_id is None:
                    raise _NotFound()
                img = await client.get(COVER_URL.format(cover_id=cover_id), params={"default": "false"})
                img.raise_for_status()
                data = img.content
        except _NotFound:
            _not_found[key] = time.time()
            return None
        except Exception:
            return None  # panne ou délai : rien n'est mis en cache
    if not data or len(data) > MAX_IMAGE_BYTES or not _looks_like_image(data):
        _not_found[key] = time.time()
        return None
    return data
