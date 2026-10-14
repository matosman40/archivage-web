"""Intégration TMDB (The Movie Database) : jaquette, résumé, genres, note et
année, pour les FILMS et les SÉRIES — en complément de Plex, jamais à sa place
pour le statut vu/non vu (que seul Plex connaît).

Il n'existe pas d'API publique officielle IMDb. TMDB est la source standard
utilisée par la plupart des outils de médiathèque et référence l'identifiant
IMDb de chaque titre en interne.

Le matching se fait par recherche de titre (extrait du nom de fichier/dossier)
+ année si connue, avec repli sans l'année. Un titre non trouvé reste sans
métadonnées, jamais d'erreur.
"""
import asyncio
import re
import time

import httpx

from . import config
from .naming import guess_title_year  # noqa: F401  (ré-exporté pour compatibilité)

TMDB_API_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w342"

KINDS = {
    "movie": {
        "search": "/search/movie",
        "detail": "/movie/{id}",
        "genres": "/genre/movie/list",
        "title_key": "title",
        "date_key": "release_date",
        "year_param": "year",
    },
    "tv": {
        "search": "/search/tv",
        "detail": "/tv/{id}",
        "genres": "/genre/tv/list",
        "title_key": "name",
        "date_key": "first_air_date",
        "year_param": "first_air_date_year",
    },
}

# Caches en mémoire (repartent à zéro au redémarrage du conteneur).
_genre_cache: dict[str, dict[int, str]] = {"movie": {}, "tv": {}}
_genre_lang: dict[str, str | None] = {"movie": None, "tv": None}
_match_cache: dict[str, dict | None] = {}
_CACHE_MAX = 5000  # borne : sans elle, le cache grossit sans fin pendant une longue session


def _remember(key: str, value) -> None:
    """Range un résultat ; évince le plus ancien quand le cache est plein."""
    if key not in _match_cache and len(_match_cache) >= _CACHE_MAX:
        _match_cache.pop(next(iter(_match_cache)), None)
    _match_cache[key] = value


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


async def _get_json(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    params = {**params, "api_key": config.settings.tmdb_api_key}
    resp = await client.get(f"{TMDB_API_BASE}{path}", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


async def _ensure_genre_cache(client: httpx.AsyncClient, kind: str) -> None:
    lang = config.settings.tmdb_language
    if _genre_cache[kind] and _genre_lang[kind] == lang:
        return
    data = await _get_json(client, KINDS[kind]["genres"], {"language": lang})
    _genre_cache[kind] = {g["id"]: g["name"] for g in data.get("genres", [])}
    _genre_lang[kind] = lang


def invalidate_cache() -> None:
    """Appelé quand la clé API ou la langue TMDB change depuis les réglages."""
    for kind in KINDS:
        _genre_cache[kind] = {}
        _genre_lang[kind] = None
    _match_cache.clear()


async def _search_one(client: httpx.AsyncClient, kind: str, title: str, year, adult: bool = False):
    cfg = KINDS[kind]
    # Un film adulte se cherche avec les titres pour adultes ; le cache les sépare des autres.
    cache_key = f"{kind}|{title.lower()}|{year or ''}|{config.settings.tmdb_language}|{'adulte' if adult else ''}"
    if cache_key in _match_cache:
        return _match_cache[cache_key]

    params = {"query": title, "language": config.settings.tmdb_language,
              "include_adult": "true" if adult else "false"}
    if year:
        params[cfg["year_param"]] = year

    async def fetch(p):
        """Renvoie (résultats, complet). complet est faux si la requête a échoué (réseau, 5xx) :
        une panne passagère ne doit pas devenir une « absence de résultat » mise en cache."""
        try:
            return (await _get_json(client, cfg["search"], p)).get("results") or [], True
        except Exception:
            return [], False

    results, complet = await fetch(params)
    if not results and year:
        # Repli sans l'année : le tag extrait du nom n'est pas toujours l'année de sortie.
        params.pop(cfg["year_param"], None)
        results, ok = await fetch(params)
        complet = complet and ok

    if not results:
        if complet:  # vraie absence de résultat : on la retient pour ne pas redemander
            _remember(cache_key, None)
        return None

    # Préfère un résultat dont le titre est identique à la recherche.
    best = next((r for r in results if _norm(r.get(cfg["title_key"])) == _norm(title)), results[0])

    overview = best.get("overview") or ""
    if not overview and config.settings.tmdb_language != "en-US":
        try:
            detail = await _get_json(client, cfg["detail"].format(id=best["id"]), {"language": "en-US"})
            overview = detail.get("overview") or ""
        except Exception:
            pass

    genres = [_genre_cache[kind].get(gid) for gid in best.get("genre_ids", [])]
    date = best.get(cfg["date_key"]) or ""
    result = {
        "id": best.get("id"),  # identifiant TMDB : sert à nommer le dossier 1fichier
        "title": best.get(cfg["title_key"]) or title,
        "poster_url": f"{TMDB_IMAGE_BASE}{best['poster_path']}" if best.get("poster_path") else None,
        "genres": [g for g in genres if g],
        "rating": round(best["vote_average"], 1) if best.get("vote_average") else None,
        "summary": overview or None,
        "year": int(date[:4]) if date[:4].isdigit() else None,
    }
    _remember(cache_key, result)
    return result


async def enrich(items: list[dict], kind: str, concurrency: int = 8) -> None:
    """Complète EN PLACE les éléments (films ou séries) auxquels il manque
    jaquette, genres, note, résumé ou année. Ne remplace jamais une donnée
    déjà fournie (Plex, ou année lue dans le nom de fichier)."""
    if not config.settings.tmdb_enabled:
        return

    def missing(it):
        return (not it.get("poster_url") or not it.get("genres")
                or it.get("rating") is None or not it.get("summary") or not it.get("year")
                or it.get("tmdb_id") is None)

    targets = [it for it in items if missing(it)]
    if not targets:
        return

    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient() as client:
        try:
            await _ensure_genre_cache(client, kind)
        except Exception:
            pass  # on continue sans noms de genres plutôt que d'échouer la recherche

        async def process(it):
            title, year = guess_title_year(it["name"])
            if it.get("search_title"):
                title = it["search_title"]  # titre identifié dans l'application
                year = it.get("search_year")
            if not title:
                return
            if it.get("year") and not it.get("search_title"):
                year = it["year"]  # l'année déjà connue l'emporte pour la recherche
            async with sem:
                match = await _search_one(client, kind, title, year, adult=bool(it.get("adult")))
            if not match:
                return
            if it.get("tmdb_id") is None and match.get("id") is not None:
                it["tmdb_id"] = match["id"]  # identifiant TMDB : sert aux sagas de films
            if not it.get("poster_url"):
                it["poster_url"] = match["poster_url"]
            if not it.get("genres"):
                it["genres"] = match["genres"]
            if it.get("rating") is None and match["rating"] is not None:
                it["rating"] = match["rating"]
                it["rating_source"] = "tmdb"
            if not it.get("summary"):
                it["summary"] = match["summary"]
            if not it.get("year") and match["year"]:
                it["year"] = match["year"]
                it["year_source"] = "tmdb"

        await asyncio.gather(*(process(it) for it in targets))


async def _find(kind: str, name: str, adult: bool = False) -> dict | None:
    if not config.settings.tmdb_enabled:
        return None
    title, year = guess_title_year(name)
    if not title:
        return None
    async with httpx.AsyncClient() as client:
        try:
            match = await _search_one(client, kind, title, year, adult=adult)
        except Exception:
            return None
    if not match or match.get("id") is None:
        return None
    return match


async def find_movie(name: str, adult: bool = False) -> dict | None:
    """Identifie un film à partir de son nom de fichier ou de dossier.

    Renvoie {"id", "title", "year", ...} ou None (TMDB désactivé, rien trouvé,
    ou erreur réseau). Contrairement à enrich(), ne renvoie un résultat que
    si l'identifiant est connu : c'est lui qui nomme le dossier sur 1fichier.
    `adult` : cherche aussi parmi les titres pour adultes (film adulte)."""
    return await _find("movie", name, adult=adult)


async def find_tv(name: str) -> dict | None:
    """Identifie une série à partir du nom de son dossier (même règle que find_movie)."""
    return await _find("tv", name)


def _year_of(date: str) -> int | None:
    return int(date[:4]) if date[:4].isdigit() else None


def _collection_ref(raw) -> dict | None:
    """Collection (saga) d'un film : identifiant, nom, affiche. None si le film n'en a pas."""
    if not raw or not raw.get("id"):
        return None
    return {"id": raw["id"], "name": raw.get("name") or None,
            "poster_url": f"{TMDB_IMAGE_BASE}{raw['poster_path']}" if raw.get("poster_path") else None}


async def collection(collection_id) -> dict | None:
    """Une saga TMDB : nom, affiche, résumé et TOUS ses films (y compris ceux qu'on n'a pas)."""
    if not config.settings.tmdb_enabled:
        return None
    lang = config.settings.tmdb_language
    async with httpx.AsyncClient() as client:
        try:
            data = await _get_json(client, f"/collection/{int(collection_id)}", {"language": lang})
        except Exception:
            return None
    parts = []
    for part in data.get("parts") or []:
        date = part.get("release_date") or ""
        parts.append({
            "tmdb_id": part.get("id"),
            "title": part.get("title") or part.get("original_title"),
            "year": _year_of(date),
            "release_date": date or None,
            "poster_url": f"{TMDB_IMAGE_BASE}{part['poster_path']}" if part.get("poster_path") else None,
        })
    parts.sort(key=lambda p: (p["release_date"] or "9999", p["title"] or ""))
    return {
        "id": int(collection_id),
        "name": data.get("name") or None,
        "overview": data.get("overview") or None,
        "poster_url": f"{TMDB_IMAGE_BASE}{data['poster_path']}" if data.get("poster_path") else None,
        "parts": parts,
    }


def _not_found(exc: Exception) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404


async def _details(kind: str, tmdb_id, strict: bool = False) -> dict | None:
    """Fiche d'un film ou d'une série. None si TMDB ne la connaît pas (404) ou si
    l'appel échoue. Avec `strict`, une erreur passagère (réseau, limite, 5xx) est
    RELEVÉE : l'appelant doit alors réessayer plus tard au lieu de conclure « introuvable »."""
    if not config.settings.tmdb_enabled:
        return None
    lang = config.settings.tmdb_language
    path = KINDS[kind]["detail"].format(id=int(tmdb_id))
    async with httpx.AsyncClient() as client:
        try:
            data = await _get_json(client, path, {"language": lang})
            overview = data.get("overview") or ""
            if not overview and lang != "en-US":
                overview = (await _get_json(client, path, {"language": "en-US"})).get("overview") or ""
        except Exception as exc:
            if strict and not _not_found(exc):
                raise
            return None
    vote = data.get("vote_average")
    genres = [g["name"] for g in data.get("genres", []) if g.get("name")]
    common = {
        "genres": genres,
        "rating": round(vote, 1) if vote else None,
        "votes": data.get("vote_count") or None,
        "summary": overview or None,
        "poster_url": f"{TMDB_IMAGE_BASE}{data['poster_path']}" if data.get("poster_path") else None,
        "backdrop_url": f"https://image.tmdb.org/t/p/w780{data['backdrop_path']}" if data.get("backdrop_path") else None,
    }
    if kind == "movie":
        date = data.get("release_date") or ""
        return {
            **common,
            "title": data.get("title") or None,
            "original_title": data.get("original_title") or None,
            "year": _year_of(date),
            "release_date": date or None,
            "runtime": data.get("runtime") or None,
            "tagline": data.get("tagline") or None,
            "tmdb_url": f"https://www.themoviedb.org/movie/{int(tmdb_id)}",
            "collection": _collection_ref(data.get("belongs_to_collection")),
        }
    date = data.get("first_air_date") or ""
    runtimes = [m for m in (data.get("episode_run_time") or []) if m]
    return {
        **common,
        "title": data.get("name") or None,
        "original_title": data.get("original_name") or None,
        "year": _year_of(date),
        "release_date": date or None,
        "runtime": runtimes[0] if runtimes else None,   # durée d'un épisode
        "tagline": data.get("tagline") or None,
        "seasons": data.get("number_of_seasons") or None,
        "episodes": data.get("number_of_episodes") or None,
        "status": data.get("status") or None,
        "tmdb_url": f"https://www.themoviedb.org/tv/{int(tmdb_id)}",
    }


async def movie_details(tmdb_id, strict: bool = False) -> dict | None:
    """Fiche complète d'un film (pour la bibliothèque 1fichier) : affiche,
    fond, genres, durée, note, résumé, accroche. None si indisponible.
    Avec `strict`, une erreur passagère est relevée (voir _details)."""
    return await _details("movie", tmdb_id, strict)


async def tv_details(tmdb_id) -> dict | None:
    """Fiche complète d'une série : saisons, épisodes, statut, durée d'un épisode."""
    return await _details("tv", tmdb_id)


# ---------------------------------------------------------------------
# Distribution d'un film (version 35) : nom, rôle et petite photo des premiers rôles
# ---------------------------------------------------------------------

CAST_LIMIT = 12
TMDB_PHOTO_BASE = "https://image.tmdb.org/t/p/w185"  # 185 px : assez pour une vignette de 56 px
_cast_cache: dict[tuple, list[dict]] = {}


async def cast(tmdb_id) -> list[dict]:
    """Premiers rôles d'un film : {name, character, photo_url}, dans l'ordre de facturation.
    Liste vide si TMDB est désactivé, si le film est inconnu ou si l'appel échoue. Un échec
    n'est jamais mis en cache : la fiche réessaiera à l'ouverture suivante."""
    if not config.settings.tmdb_enabled or tmdb_id is None:
        return []
    lang = config.settings.tmdb_language
    key = (int(tmdb_id), lang)
    if key in _cast_cache:
        return _cast_cache[key]
    async with httpx.AsyncClient() as client:
        try:
            data = await _get_json(client, f"/movie/{int(tmdb_id)}/credits", {"language": lang})
        except Exception:
            return []
    result = _billed_cast(data)
    if len(_cast_cache) >= 500:
        _cast_cache.clear()  # borne simple : la mémoire ne grandit pas sans limite
    _cast_cache[key] = result
    return result


# ---------------------------------------------------------------------
# Fiche technique, fiche d'une personne et filmographie (version 38)
# ---------------------------------------------------------------------

TMDB_LOGO_BASE = "https://image.tmdb.org/t/p/w185"          # logos de sociétés (fond transparent)
TMDB_PERSON_PHOTO_BASE = "https://image.tmdb.org/t/p/w342"  # photo de la fiche personne
TMDB_POSTER_SMALL_BASE = "https://image.tmdb.org/t/p/w185"  # affiche d'une carte de filmographie
TEAM_LIMIT = 6
DIRECTOR_JOBS = {"Director"}
WRITER_JOBS = {"Screenplay", "Writer", "Story", "Novel"}
JOB_LABELS = {  # intitulés TMDB (anglais) -> français
    "Director": "Réalisation",
    "Screenplay": "Scénario", "Writer": "Scénario", "Story": "Histoire", "Novel": "Roman",
    "Producer": "Production", "Executive Producer": "Production exécutive",
    "Director of Photography": "Photographie", "Editor": "Montage",
    "Original Music Composer": "Musique", "Music": "Musique",
    "Production Design": "Décors", "Costume Design": "Costumes", "Casting": "Casting",
    "Sound": "Son", "Visual Effects Supervisor": "Effets visuels",
}
_credits_cache: dict[tuple, dict] = {}
_person_cache: dict[tuple, dict] = {}


def _photo(path, base: str) -> str | None:
    return f"{base}{path}" if path else None


def _cached_put(cache: dict, key, value) -> None:
    if len(cache) >= 300:
        cache.clear()  # borne simple : la mémoire ne grandit pas sans limite
    cache[key] = value


def _billed_cast(block: dict) -> list[dict]:
    """Distribution dans l'ordre de facturation, limitée à CAST_LIMIT, avec l'identifiant de chaque acteur."""
    billed = sorted((c for c in block.get("cast") or [] if c.get("name")),
                    key=lambda c: c["order"] if isinstance(c.get("order"), int) else 10**6)
    return [{
        "id": c.get("id"),
        "name": c["name"],
        "character": c.get("character") or None,
        "photo_url": _photo(c.get("profile_path"), TMDB_PHOTO_BASE),
    } for c in billed[:CAST_LIMIT]]


def _people(rows, limit: int | None = None) -> list[dict]:
    """Personnes sans doublon (un même nom revient parfois sous plusieurs départements), avec photo."""
    seen, out = set(), []
    for r in rows:
        pid, name = r.get("id"), (r.get("name") or "").strip()
        if not name or pid in seen:
            continue
        seen.add(pid)
        out.append({"id": pid, "name": name, "photo_url": _photo(r.get("profile_path"), TMDB_PHOTO_BASE)})
    return out[:limit] if limit else out


def empty_credits() -> dict:
    """Fiche technique vide : la même forme pour un film inconnu, un échec et un film pour adultes
    refusé. Une réponse identique ne laisse rien deviner."""
    return {"cast": [], "directors": [], "writers": [], "studios": []}


async def credits(tmdb_id) -> dict:
    """Fiche technique d'un film en un seul appel TMDB (crédits et sociétés de production) :
    réalisateurs, scénaristes, studios avec logo, distribution. Listes vides si TMDB est désactivé,
    si le film est inconnu ou si l'appel échoue ; un échec n'est jamais mis en cache.
    `adult` (drapeau TMDB) est gardé dans le résultat : la route s'en sert pour masquer le film
    sans session. Le résultat en cache n'est jamais modifié ; la route en fait une copie."""
    empty = empty_credits()
    if not config.settings.tmdb_enabled or tmdb_id is None:
        return empty
    lang = config.settings.tmdb_language
    key = (int(tmdb_id), lang)
    if key in _credits_cache:
        return _credits_cache[key]
    async with httpx.AsyncClient() as client:
        try:
            data = await _get_json(client, f"/movie/{int(tmdb_id)}",
                                   {"language": lang, "append_to_response": "credits"})
        except Exception:
            return empty
    block = data.get("credits") or {}
    crew = [c for c in block.get("crew") or [] if isinstance(c, dict)]
    result = {
        "adult": bool(data.get("adult")),
        "cast": _billed_cast(block),
        "directors": _people((c for c in crew if c.get("job") in DIRECTOR_JOBS), TEAM_LIMIT),
        "writers": _people((c for c in crew if c.get("job") in WRITER_JOBS), TEAM_LIMIT),
        "studios": [{
            "id": s.get("id"),
            "name": s["name"].strip(),
            "logo_url": _photo(s.get("logo_path"), TMDB_LOGO_BASE),
            "country": s.get("origin_country") or None,
        } for s in data.get("production_companies") or []
            if isinstance(s, dict) and (s.get("name") or "").strip()][:TEAM_LIMIT],
    }
    _cached_put(_credits_cache, key, result)
    return result


def _filmography(movie_credits: dict) -> list[dict]:
    """Une carte par film, même si la personne y joue et y travaille ; les rôles sont regroupés.
    Du plus récent au plus ancien, les films sans date en dernier."""
    films: dict = {}

    def add(row: dict, group: str, label: str | None) -> None:
        mid = row.get("id")
        title = (row.get("title") or row.get("original_title") or "").strip()
        if not mid or not title:
            return
        entry = films.get(mid)
        if entry is None:
            date = row.get("release_date") or ""
            vote = row.get("vote_average")
            entry = films[mid] = {
                "tmdb_id": mid, "title": title, "year": _year_of(date), "release_date": date or None,
                "poster_url": _photo(row.get("poster_path"), TMDB_POSTER_SMALL_BASE),
                "rating": round(vote, 1) if vote else None, "adult": bool(row.get("adult")), "roles": [],
            }
        role = {"group": group, "label": label}
        if role not in entry["roles"]:
            entry["roles"].append(role)

    for row in movie_credits.get("cast") or []:
        add(row, "jeu", row.get("character") or None)
    for row in movie_credits.get("crew") or []:
        job = row.get("job") or ""
        if job in DIRECTOR_JOBS:
            add(row, "realisation", JOB_LABELS[job])
        elif job in WRITER_JOBS:
            add(row, "scenario", JOB_LABELS.get(job, job))
        else:
            add(row, "autres", JOB_LABELS.get(job, job) or row.get("department") or None)
    out = list(films.values())
    out.sort(key=lambda f: f["release_date"] or "", reverse=True)
    return out


async def person(person_id, unlocked: bool) -> dict | None:
    """Fiche d'une personne (biographie, dates, photo) et sa filmographie. Sans session déverrouillée,
    les titres pour adultes sont retirés. None si TMDB est désactivé ou ne connaît pas la personne (404).
    Une erreur passagère est relevée, et jamais mise en cache."""
    if not config.settings.tmdb_enabled:
        return None
    pid = int(person_id)
    lang = config.settings.tmdb_language
    key = (pid, lang)
    info = _person_cache.get(key)
    if info is None:
        path = f"/person/{pid}"
        async with httpx.AsyncClient() as client:
            try:
                raw = await _get_json(client, path, {"language": lang, "append_to_response": "movie_credits"})
                bio = (raw.get("biography") or "").strip()
                if not bio and lang != "en-US":
                    bio = ((await _get_json(client, path, {"language": "en-US"})).get("biography") or "").strip()
            except Exception as exc:
                if _not_found(exc):
                    return None
                raise
        info = {
            "id": pid,
            "name": raw.get("name") or None,
            "known_for": raw.get("known_for_department") or None,
            "biography": bio or None,
            "birthday": raw.get("birthday") or None,
            "deathday": raw.get("deathday") or None,
            "place_of_birth": raw.get("place_of_birth") or None,
            "photo_url": _photo(raw.get("profile_path"), TMDB_PERSON_PHOTO_BASE),
            "tmdb_url": f"https://www.themoviedb.org/person/{pid}",
            "films": _filmography(raw.get("movie_credits") or {}),
        }
        _cached_put(_person_cache, key, info)
    visible = [dict(f) for f in info["films"] if unlocked or not f["adult"]]
    return {**info, "films": visible}


async def enrich_movies(items: list[dict]) -> None:
    await enrich(items, "movie")


async def enrich_series(items: list[dict]) -> None:
    await enrich(items, "tv")


async def _search_uncached(kind: str, query: str, year: int | None = None, limit: int = 8, adult: bool = False) -> list[dict]:
    """Recherche manuelle (renommage d'un élément d'un disque) : renvoie plusieurs
    résultats {id, title, year, poster_url, overview}. Sans l'année si elle ne
    donne rien. Les erreurs réseau remontent à l'appelant (httpx.HTTPError).
    `adult` : inclut les titres pour adultes (identification d'un film adulte)."""
    cfg = KINDS[kind]
    params = {"query": query, "language": config.settings.tmdb_language,
              "include_adult": "true" if adult else "false"}
    if year:
        params[cfg["year_param"]] = year
    async with httpx.AsyncClient() as client:
        results = (await _get_json(client, cfg["search"], params)).get("results") or []
        if not results and year:
            params.pop(cfg["year_param"], None)
            results = (await _get_json(client, cfg["search"], params)).get("results") or []
    out = []
    for r in results[:limit]:
        date = r.get(cfg["date_key"]) or ""
        out.append({
            "id": r.get("id"),
            "title": r.get(cfg["title_key"]) or "",
            "year": int(date[:4]) if date[:4].isdigit() else None,
            "poster_url": f"{TMDB_IMAGE_BASE}{r['poster_path']}" if r.get("poster_path") else None,
            "overview": r.get("overview") or "",
        })
    return out


SEARCH_CACHE_SECONDS = 600
_search_cache: dict[tuple, tuple[float, list]] = {}


async def search(kind: str, query: str, year: int | None = None, limit: int = 8, adult: bool = False) -> list[dict]:
    """Recherche avec cache court : retaper un titre ou revenir dessus ne refait pas l'appel TMDB.
    Une erreur réseau n'est jamais mise en cache."""
    key = (kind, (query or "").strip().lower(), year, bool(adult), limit, config.settings.tmdb_language)
    hit = _search_cache.get(key)
    if hit and time.time() - hit[0] < SEARCH_CACHE_SECONDS:
        return [dict(r) for r in hit[1]]
    out = await _search_uncached(kind, query, year=year, limit=limit, adult=adult)
    if len(_search_cache) >= 300:
        _search_cache.clear()
    _search_cache[key] = (time.time(), out)
    return [dict(r) for r in out]
