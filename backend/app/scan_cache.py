"""Cache de la recherche (films, séries, ebooks).

Sans cache, chaque ouverture de page reparcourait tous les disques (et refaisait
les appels TMDB). Ici, la liste complète de chaque type est gardée en mémoire,
déjà enrichie (Plex, TMDB) :
- une recherche ne fait que filtrer cette liste (instantané) ;
- la liste est construite au démarrage, puis remise à jour en arrière-plan
  quand elle est vieille (SEARCH_CACHE_SECONDS, 15 min par défaut) ou quand une
  opération a modifié les sources ;
- tant qu'une mise à jour tourne, la réponse dit `refreshing: true` et
  l'interface relit la liste toute seule.
Une copie est écrite à côté de settings.json (library-cache.json) : au
redémarrage, l'interface affiche cette copie tout de suite, pendant que la
liste se reconstruit en arrière-plan.
"""
import asyncio
import json
import logging
import os
import time
from typing import Awaitable, Callable, Optional

from . import config

KINDS = ("films", "series", "ebooks")
TTL_SECONDS = float(os.environ.get("SEARCH_CACHE_SECONDS", "900"))

log = logging.getLogger("archivage.cache")

_ENTRIES: dict[str, dict] = {}   # kind -> {"items": [...], "built_at": float}
_TASKS: dict[str, asyncio.Task] = {}
_BUILDER: Optional[Callable[[str], Awaitable[list]]] = None
# Incrémenté à chaque invalidation. Une invalidation reçue PENDANT une reconstruction ne doit pas
# être perdue : sinon la liste construite avant le changement serait jugée fraîche pendant 15 min.
_GENERATION: dict[str, int] = {}


def cached_items(kind: str) -> list:
    """Éléments déjà en mémoire pour un type, sans lancer de scan."""
    return list((_ENTRIES.get(kind) or {}).get("items") or [])


def configure(builder: Callable[[str], Awaitable[list]]) -> None:
    """Fonction qui reconstruit la liste d'un type (scan + Plex + TMDB)."""
    global _BUILDER
    _BUILDER = builder


def _is_stale(kind: str) -> bool:
    entry = _ENTRIES.get(kind)
    return entry is None or time.time() - entry["built_at"] > TTL_SECONDS


def is_refreshing(kind: str) -> bool:
    task = _TASKS.get(kind)
    return task is not None and not task.done()


def _start(kind: str) -> asyncio.Task:
    task = _TASKS.get(kind)
    if task is not None and not task.done():
        return task
    task = asyncio.ensure_future(_run(kind))
    _TASKS[kind] = task
    return task


def _snapshot_path() -> str:
    return os.path.join(os.path.dirname(config.SETTINGS_FILE) or ".", "library-cache.json")


def load_snapshot() -> None:
    """Charge la copie disque (si elle existe) : affichage immédiat au démarrage."""
    try:
        with open(_snapshot_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    for kind in KINDS:
        entry = data.get(kind)
        if kind not in _ENTRIES and isinstance(entry, dict) and isinstance(entry.get("items"), list):
            _ENTRIES[kind] = {"items": entry["items"], "built_at": float(entry.get("built_at") or 0)}


def _save_snapshot() -> None:
    path = _snapshot_path()
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_ENTRIES, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        log.exception("copie du cache de recherche impossible (%s)", path)


async def _run(kind: str) -> None:
    gen = _GENERATION.get(kind, 0)
    relancer = False
    try:
        items = await _BUILDER(kind)
        # Changé pendant le calcul : la liste est gardée mais marquée périmée, et reconstruite aussitôt.
        relancer = _GENERATION.get(kind, 0) != gen
        _ENTRIES[kind] = {"items": items, "built_at": 0.0 if relancer else time.time()}
        _save_snapshot()
    except Exception:  # on garde la liste précédente plutôt que de la vider
        log.exception("reconstruction du cache %s impossible", kind)
    finally:
        _TASKS.pop(kind, None)
    if relancer:
        _start(kind)


def invalidate(kinds: tuple = KINDS) -> None:
    """Les sources ont changé. La liste actuelle reste servie. Si on est dans la
    boucle d'événements, la reconstruction démarre tout de suite ; sinon (route
    synchrone), elle démarrera à la prochaine recherche, la liste étant marquée
    périmée."""
    for kind in kinds:
        _GENERATION[kind] = _GENERATION.get(kind, 0) + 1
        if kind in _ENTRIES:
            _ENTRIES[kind]["built_at"] = 0.0
    if _BUILDER is None:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    for kind in kinds:
        _start(kind)


async def warm_all() -> None:
    """Au démarrage : construit les trois listes sans attendre la première recherche."""
    if _BUILDER is None:
        return
    load_snapshot()
    await asyncio.gather(*(_start(k) for k in KINDS), return_exceptions=True)


async def search(kind: str, term: str) -> tuple[list, dict]:
    """Renvoie (éléments filtrés, méta). Ne fait jamais attendre le scan des disques."""
    if _BUILDER is None:
        raise RuntimeError("cache de recherche non configuré")
    if kind not in _ENTRIES or _is_stale(kind):
        # Première fois ou périmée : on ne bloque jamais la page. Si rien n'est
        # encore prêt, la réponse est vide avec `refreshing: true` et l'interface relit.
        _start(kind)
    entry = _ENTRIES.get(kind)
    items = entry["items"] if entry else []
    needle = term.lower().strip()
    if needle:
        items = [it for it in items if needle in it["name"].lower()]
    meta = {
        "built_at": entry["built_at"] if entry else None,
        "refreshing": is_refreshing(kind),
    }
    return items, meta
