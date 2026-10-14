"""API web pour l'archivage de films/séries/ebooks — remplace les read -p du
script bash par des endpoints HTTP/WebSocket appelés depuis le frontend."""
import asyncio
import os
import shutil
import time
from urllib.parse import quote

import httpx
from typing import List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import adult_auth, adult_media, config, disk_health, disk_integrity, disk_library, disk_meta, disks, ebooks as ebooks_mod, hosts, jobs, media, onefichier, plex, scan_cache, scheduler, security, tmdb
from . import series as series_mod
from .logs import log_action, read_log
from .naming import guess_title_year
import base64
from . import ebook_online, identities, sagas as sagas_mod

app = FastAPI(title="Archivage Médias")
# Pas de CORS : l'interface est servie par la même origine. Un site tiers ne peut donc
# ni lire les réponses de l'API, ni les écrire depuis le navigateur d'un visiteur.

MediaType = Literal["films", "series", "ebooks"]


# ---------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------

class SearchRequest(BaseModel):
    media_type: Literal["films", "series", "ebooks", "adultes"]
    term: str = ""


class PlanFlatRequest(BaseModel):
    """Utilisé pour films et ebooks : sélection à plat, avec le choix entre
    fichiers seuls ou dossiers parents dédiés."""
    mode: Literal["files", "parents"] = "files"
    paths: list[str]


class PlanSeriesRequest(BaseModel):
    names_paths: dict[str, list[str]]  # nom de série -> chemins physiques


class SettingsUpdate(BaseModel):
    """Toutes les valeurs sont optionnelles : seuls les champs fournis sont
    modifiés (mise à jour partielle), le reste des réglages est conservé."""
    source_dirs_films: Optional[List[str]] = None
    source_dirs_series: Optional[List[str]] = None
    source_dirs_ebooks: Optional[List[str]] = None
    source_dirs_adultes: Optional[List[str]] = None
    dest_subdir_films: Optional[str] = None
    dest_subdir_series: Optional[str] = None
    dest_subdir_ebooks: Optional[str] = None
    dest_subdir_adultes: Optional[str] = None
    dest_scan_roots: Optional[List[str]] = None
    safety_margin_bytes: Optional[int] = None
    video_extensions: Optional[List[str]] = None
    ebook_extensions: Optional[List[str]] = None
    plex_url: Optional[str] = None
    plex_token: Optional[str] = None
    plex_timeout: Optional[float] = None
    tmdb_api_key: Optional[str] = None
    tmdb_language: Optional[str] = None
    dry_run: Optional[bool] = None
    delete_confirm_text: Optional[str] = None
    onefichier_api_key: Optional[str] = None
    onefichier_staging_dir: Optional[str] = None
    onefichier_compression: Optional[int] = None
    restore_dir_films: Optional[str] = None
    restore_dir_series: Optional[str] = None
    restore_dir_ebooks: Optional[str] = None
    notify_url: Optional[str] = None
    archive_copy_dir: Optional[str] = None
    transfer_windows: Optional[List[dict]] = None  # plages de transfert (validées dans config)


class OneFichierArchiveRequest(BaseModel):
    paths: list[str]
    # keep   : sauvegarde seule, copie locale conservée (comportement d'origine)
    # delete : sauvegarde puis suppression de la copie locale (libère le serveur)
    # move   : sauvegarde, copie sur le disque `destination`, puis retrait de la source
    after: Literal["keep", "delete", "move"] = "keep"
    destination: Optional[str] = None
    confirm_delete_text: Optional[str] = None
    priority: Literal["urgent", "normal", "bas"] = "normal"


class OneFichierRestoreRequest(BaseModel):
    keys: list[str]                    # clés de l'index : "603", "tv:1399", "book:0123abcd4567"
    # origin : emplacement d'origine ; default : dossier de restauration des réglages ;
    # disk : disque externe détecté (champ disk), dans le sous-dossier MEDIA/... du type
    target: Literal["origin", "default", "disk"] = "default"
    disk: Optional[str] = None
    priority: Literal["urgent", "normal", "bas"] = "normal"


class JobRequest(BaseModel):
    operation: Literal["copy", "move", "delete"]
    media_type: MediaType
    items: list[str]
    destination: Optional[str] = None  # racine du disque choisi (sans le sous-dossier)
    merge: bool = False
    confirm_delete_text: Optional[str] = None
    priority: Literal["urgent", "normal", "bas"] = "normal"


class JobPriorityRequest(BaseModel):
    priority: Literal["urgent", "normal", "bas"]


class DiskItemsRequest(BaseModel):
    items: list[str]                   # éléments de la bibliothèque disque (enfants directs MEDIA/…)
    confirm_delete_text: Optional[str] = None


class DiskTransferRequest(BaseModel):
    items: list[str]
    mode: Literal["copy", "move"] = "copy"
    server_dir: str                    # dossier source configuré du serveur (films, séries ou ebooks)
    priority: Literal["urgent", "normal", "bas"] = "normal"


class DiskForgetRequest(BaseModel):
    path: str                          # racine d'un disque hors ligne à retirer de la liste


class DiskRenameRequest(BaseModel):
    path: str                          # élément d'un disque connecté (enfant direct MEDIA/…)
    new_name: str                      # nouveau nom, sans chemin ; l'extension d'origine est gardée


DEST_SUBDIR_BY_TYPE = {
    "films": lambda: config.settings.dest_subdir_films,
    "series": lambda: config.settings.dest_subdir_series,
    "ebooks": lambda: config.settings.dest_subdir_ebooks,
}


def _size_of(path: str) -> int:
    try:
        if os.path.isfile(path):
            return os.path.getsize(path)
        total = 0
        for dirpath, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
        return total
    except OSError:
        return 0


# ---------------------------------------------------------------------
# Config / méta
# ---------------------------------------------------------------------

@app.get("/api/config")
def get_config():
    return {
        "dry_run": config.settings.dry_run,
        "safety_margin_bytes": config.settings.safety_margin_bytes,
        "plex_enabled": config.settings.plex_enabled,
        "tmdb_enabled": config.settings.tmdb_enabled,
        "source_dirs_films": config.settings.source_dirs_films,
        "source_dirs_series": config.settings.source_dirs_series,
        "source_dirs_ebooks": config.settings.source_dirs_ebooks,
        "dest_subdir_films": config.settings.dest_subdir_films,
        "dest_subdir_series": config.settings.dest_subdir_series,
        "dest_subdir_ebooks": config.settings.dest_subdir_ebooks,
        "delete_confirm_text": config.settings.delete_confirm_text,
    }


# ---------------------------------------------------------------------
# Réglages éditables depuis l'interface (persistés dans SETTINGS_FILE)
# ---------------------------------------------------------------------

def _detect_source_overlap(s: "config.Settings") -> list[str]:
    """Avertit si un dossier source (films/séries/ebooks) chevauche un
    dossier source d'un AUTRE type : dans ce cas, les mêmes fichiers peuvent
    apparaître dans plusieurs recherches et finir copiés au mauvais endroit
    (voir l'incident où un épisode de série est parti dans MEDIA/FILM)."""
    groups = {
        "films": s.source_dirs_films,
        "films adultes": s.source_dirs_adultes,
        "séries": s.source_dirs_series,
        "ebooks": s.source_dirs_ebooks,
    }
    warnings: list[str] = []
    names = list(groups.keys())
    for i, name_a in enumerate(names):
        for name_b in names[i + 1:]:
            for a in groups[name_a]:
                an = os.path.normpath(a)
                for b in groups[name_b]:
                    bn = os.path.normpath(b)
                    if an == bn or an.startswith(bn + os.sep) or bn.startswith(an + os.sep):
                        warnings.append(
                            f"'{a}' ({name_a}) et '{b}' ({name_b}) se chevauchent : des fichiers "
                            f"risquent d'apparaître dans les deux recherches et d'être copiés au "
                            f"mauvais endroit."
                        )
    return warnings


def _public_settings(s: "config.Settings") -> dict:
    """Réglages renvoyés à l'interface : la clé API 1fichier n'en sort jamais
    en clair (seuls les 4 derniers caractères restent visibles)."""
    data = s.as_dict()
    # Clés et jetons : seuls les derniers caractères restent visibles (version 42).
    data["onefichier_api_key"] = _masque(data.get("onefichier_api_key") or "", 4)
    data["tmdb_api_key"] = _masque(data.get("tmdb_api_key") or "", 4)
    data["plex_token"] = _masque(data.get("plex_token") or "", 4)
    data["notify_url"] = _masque(data.get("notify_url") or "", 6)  # l'URL peut contenir un jeton (webhook)
    return data


def _masque(valeur: str, visibles: int) -> str:
    return ("•" * 8 + valeur[-visibles:]) if valeur else ""


def _sans_secret(texte: str) -> str:
    """Message d'erreur sans secret. Une erreur httpx contient l'adresse appelée, donc la clé
    ou le jeton : on les remplace avant de renvoyer le message au navigateur."""
    for secret in (config.settings.tmdb_api_key, config.settings.plex_token, config.settings.onefichier_api_key):
        if secret:
            texte = texte.replace(secret, "•••")
    return texte


@app.get("/api/settings")
def get_settings():
    warnings = _detect_source_overlap(config.settings)
    return {"settings": _public_settings(config.settings), "settings_file": config.SETTINGS_FILE, "warnings": warnings}


@app.put("/api/settings")
def put_settings(req: SettingsUpdate):
    patch = {k: v for k, v in req.model_dump().items() if v is not None}
    # Clé 1fichier renvoyée masquée par GET : on ignore alors la valeur masquée
    # (sinon enregistrer le formulaire écraserait la vraie clé).
    # Les secrets renvoyés masqués par GET sont ignorés à l'enregistrement (sinon le formulaire
    # écraserait le vrai secret par le texte masqué).
    for champ in ("onefichier_api_key", "notify_url", "tmdb_api_key", "plex_token"):
        if str(patch.get(champ, "")).startswith("•"):
            patch.pop(champ)
    if not patch:
        return {"settings": _public_settings(config.settings), "warnings": []}

    try:
        updated = config.update_settings(patch)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except OSError as exc:  # réglages non écrits : l'état en mémoire n'a pas changé
        raise HTTPException(500, f"Impossible d'enregistrer les réglages : {exc}")
    scan_cache.invalidate()  # dossiers sources peut-être modifiés

    # Un changement d'URL/token Plex invalide le cache "vu/non vu" précédent.
    if "plex_url" in patch or "plex_token" in patch:
        plex.invalidate_cache()

    # Un changement de clé ou de langue TMDB invalide le cache de matching.
    if "tmdb_api_key" in patch or "tmdb_language" in patch:
        tmdb.invalidate_cache()

    warnings = _detect_source_overlap(updated)

    return {"settings": _public_settings(updated), "warnings": warnings}


@app.post("/api/settings/reset")
def post_settings_reset():
    return {"settings": _public_settings(config.reset_settings())}


@app.get("/api/settings/check-path")
def check_path(path: str):
    """Permet à l'interface de vérifier qu'un chemin saisi est bien visible
    à l'intérieur du conteneur avant de l'enregistrer comme dossier source."""
    exists = os.path.isdir(path)
    readable = exists and os.access(path, os.R_OK)
    return {"path": path, "exists": exists, "is_dir": exists, "readable": readable}


# ---------------------------------------------------------------------
# Recherche
# ---------------------------------------------------------------------

def _apply_plex_movie(it: dict, info: Optional[dict]) -> None:
    """Fusionne les métadonnées Plex dans un film (Plex prime sur le nom de fichier)."""
    if not info:
        it.update({"watched": None, "rating": None, "genres": [], "poster_url": None,
                   "summary": None, "rating_source": None, "last_viewed_at": None,
                   "duration_min": None, "content_rating": None})
        return
    it["watched"] = info["watched"]
    it["rating"] = info["rating"]
    it["rating_source"] = "plex" if info["rating"] is not None else None
    it["genres"] = info["genres"]
    it["poster_url"] = info["poster_url"]
    it["summary"] = info["summary"]
    it["last_viewed_at"] = info["last_viewed"]
    it["duration_min"] = info["duration_min"]
    it["content_rating"] = info["content_rating"]
    if info["year"]:
        it["year"], it["year_source"] = info["year"], "plex"
    if info["added_at"]:
        it["added_at"], it["added_source"] = info["added_at"], "plex"


def _apply_plex_series(it: dict, info: Optional[dict]) -> None:
    if not info:
        it.update({"watched": None, "viewed_episodes": None, "plex_seasons": None,
                   "rating": None, "rating_source": None, "genres": [], "poster_url": None,
                   "summary": None, "last_viewed_at": None})
        return
    it["watched"] = info["watched"]
    it["viewed_episodes"] = info["viewed_episodes"]
    it["plex_seasons"] = info["plex_seasons"]
    it["rating"] = info["rating"]
    it["rating_source"] = "plex" if info["rating"] is not None else None
    it["genres"] = info["genres"]
    it["poster_url"] = info["poster_url"]
    it["summary"] = info["summary"]
    it["last_viewed_at"] = info["last_viewed"]
    if info["year"]:
        it["year"], it["year_source"] = info["year"], "plex"
    if info["added_at"]:
        it["added_at"], it["added_source"] = info["added_at"], "plex"


def _rapprocher_films(items: list[dict]) -> None:
    """Plex et identifications des films, EN PLACE. Appelée dans un thread (version 42)."""
    for it in items:
        if not it.get("adult"):  # un film adulte n'est pas rapproché de Plex
            _apply_plex_movie(it, plex.get_movie_plex_info(it["path"], it.get("year")))
    identities.apply("films", items)


def _rapprocher_series(items: list[dict]) -> None:
    for it in items:
        _apply_plex_series(it, plex.get_series_plex_info(it["name"], it.get("year")))
    identities.apply("series", items)


async def _build_search_items(kind: str) -> list[dict]:
    """Reconstruit la liste complète d'un type (scan des disques, Plex, TMDB).
    Appelée par le cache de recherche : jamais directement par une recherche."""
    if kind == "films":
        items = await asyncio.to_thread(media.search_movies, "")
        # Rapprochement Plex (repli par inclusion sur tout l'index : coûteux) hors de la boucle.
        await asyncio.to_thread(_rapprocher_films, items)
        if config.settings.tmdb_enabled:
            try:
                await tmdb.enrich(items, "movie")
            except Exception:
                pass  # l'enrichissement ne doit jamais faire échouer la liste
        return items
    if kind == "ebooks":
        return await asyncio.to_thread(ebooks_mod.search_ebooks, "")
    items = await asyncio.to_thread(series_mod.search_series, "")
    await asyncio.to_thread(_rapprocher_series, items)
    if config.settings.tmdb_enabled:
        try:
            await tmdb.enrich(items, "tv")
        except Exception:
            pass
    return items


scan_cache.configure(_build_search_items)


@app.on_event("startup")
async def _warm_search_cache() -> None:
    # Les listes se construisent pendant le démarrage : la première ouverture est instantanée.
    asyncio.ensure_future(scan_cache.warm_all())


@app.on_event("startup")
async def _start_onefichier_checks() -> None:
    # Contrôle quotidien des liens et test de restauration mensuel (boucle en arrière-plan).
    task = asyncio.create_task(onefichier.scheduler_loop())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


# ---------------------------------------------------------------------
# Médias adultes : session déverrouillée par code (version 36)
# ---------------------------------------------------------------------

class AdultUnlockRequest(BaseModel):
    code: str = Field(default="", max_length=200)


class AdultCodeRequest(BaseModel):
    current: Optional[str] = Field(default=None, max_length=200)
    new: str = Field(default="", max_length=200)


def adult_unlocked(request: Request) -> bool:
    """Vrai si cette requête porte une session déverrouillée (cookie, voir adult_auth)."""
    return adult_auth.session_is_valid(request.cookies.get(adult_auth.COOKIE))


def _hidden_jobs(unlocked: bool) -> frozenset:
    """Tâches à taire pour une session verrouillée : celles qui touchent un film adulte."""
    return frozenset() if unlocked else jobs.adult_ids()


def _hidden_404() -> HTTPException:
    """Un élément adulte, sans session, répond comme s'il n'existait pas."""
    return HTTPException(404, "Élément introuvable. Rafraîchis la liste.")


def _visible_entry_or_404(key: str, unlocked: bool) -> None:
    entry = onefichier.get_entry(key)
    if entry is not None and not adult_media.visible(entry, unlocked):
        raise _hidden_404()


def _disk_items_for_session(items: list[str], unlocked: bool) -> bool:
    """Éléments de disque : refuse les adultes sans session. Renvoie vrai si la tâche touche un
    film adulte (elle est alors masquée des listes tant que la session est verrouillée)."""
    flags = []
    for path in items:
        loc = disk_library.locate(path)
        flags.append(bool(loc) and adult_media.disk_path_is_adult(path, loc[1]))
    if not unlocked and any(flags):
        raise _hidden_404()
    return any(flags)


def _adult_session_response(response: Response, request: Request, token: str) -> None:
    response.set_cookie(adult_auth.COOKIE, token, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", path="/")


@app.get("/api/adult/state")
def adult_state(unlocked: bool = Depends(adult_unlocked)):
    return {"code_set": adult_auth.code_is_set(), "unlocked": unlocked, "retry_in": adult_auth.retry_in()}


@app.post("/api/adult/unlock")
def adult_unlock(req: AdultUnlockRequest, request: Request, response: Response):
    try:
        token = adult_auth.unlock(req.code)
    except adult_auth.AdultAuthError as exc:
        raise HTTPException(exc.status, exc.message)
    _adult_session_response(response, request, token)
    return {"unlocked": True}


@app.post("/api/adult/lock")
def adult_lock(request: Request, response: Response):
    adult_auth.revoke(request.cookies.get(adult_auth.COOKIE))
    response.delete_cookie(adult_auth.COOKIE, path="/", httponly=True, samesite="strict")
    return {"unlocked": False}


@app.post("/api/adult/code")
def adult_set_code(req: AdultCodeRequest, request: Request, response: Response):
    first_time = not adult_auth.code_is_set()
    try:
        adult_auth.set_code(req.new, current=req.current)
    except adult_auth.AdultAuthError as exc:
        raise HTTPException(exc.status, exc.message)
    _adult_session_response(response, request, adult_auth.create_session())
    log_action("OK", "CODE ADULTE", "créé" if first_time else "changé")
    return {"unlocked": True, "code_set": True}


@app.post("/api/search")
async def search(req: SearchRequest, unlocked: bool = Depends(adult_unlocked)):
    if req.media_type == "adultes":
        # Catégorie Adultes : films adultes seulement, et rien sans session déverrouillée.
        items, meta = await scan_cache.search("films", req.term)
        return _json_rapide({"items": [it for it in items if adult_media.is_adult_item(it)] if unlocked else [], **meta})
    items, meta = await scan_cache.search(req.media_type, req.term)
    # Médias adultes : absents de la liste sans session déverrouillée.
    return _json_rapide({"items": adult_media.filter_visible(items, unlocked), **meta})


# ---------------------------------------------------------------------
# Plex
# ---------------------------------------------------------------------

@app.get("/api/plex/status")
def plex_status():
    return plex.status()


@app.post("/api/plex/refresh")
async def plex_refresh():
    if not config.settings.plex_enabled:
        raise HTTPException(400, "Plex n'est pas configuré (PLEX_URL / PLEX_TOKEN manquants).")
    try:
        result = await plex.refresh_cache()
        scan_cache.invalidate(("films", "series"))  # les correspondances Plex ont changé
        return result
    except Exception as exc:
        raise HTTPException(502, f"Impossible de joindre Plex : {_sans_secret(str(exc))}")


@app.get("/api/plex/poster")
async def plex_poster(path: str):
    """Sert l'affiche d'un film en la relayant depuis Plex, sans jamais
    exposer PLEX_TOKEN au navigateur (le lien renvoyé par /api/search pointe
    ici, pas directement vers le serveur Plex)."""
    if not config.settings.plex_enabled:
        raise HTTPException(404, "Plex n'est pas configuré.")
    try:
        result = await plex.get_poster_bytes(path)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"Impossible de récupérer l'affiche Plex : {_sans_secret(str(exc))}")
    if result is None:
        raise HTTPException(404, "Affiche indisponible.")
    content, content_type = result
    return Response(content=content, media_type=content_type)


# ---------------------------------------------------------------------
# Destinations
# ---------------------------------------------------------------------

@app.get("/api/disks")
def get_disks():
    return {"disks": disks.list_destination_disks()}


# ---------------------------------------------------------------------
# Planification (sélection -> éléments d'action + garde-fous)
# ---------------------------------------------------------------------

def _hide_adult_plan(plan: dict, unlocked: bool) -> dict:
    """Retire du plan les films adultes sans session déverrouillée : ils ne
    peuvent donc pas être envoyés, copiés ou supprimés depuis l'interface ni l'API."""
    hidden = set(adult_media.hidden_paths(plan["items"], unlocked))
    if hidden:
        # Retirés sans avertissement : un message du type « N film(s) adulte(s) ignoré(s) »
        # révélerait leur présence à une session verrouillée (version 42).
        plan["items"] = [i for i in plan["items"] if i not in hidden]
        plan["total_size"] = sum(_size_of(i) for i in plan["items"])
    return plan


@app.post("/api/plan/films")
def plan_films(req: PlanFlatRequest, unlocked: bool = Depends(adult_unlocked)):
    plan = _plan_flat(
        req, source_dirs=config.settings.source_dirs_films + adult_media.adult_roots(),
        extensions=config.settings.video_extensions_tuple, noun="films",
    )
    return _hide_adult_plan(plan, unlocked)


@app.post("/api/plan/ebooks")
def plan_ebooks(req: PlanFlatRequest):
    return _plan_flat(
        req, source_dirs=config.settings.source_dirs_ebooks,
        extensions=config.settings.ebook_extensions_tuple, noun="ebooks", folders=True,
    )


def _plan_flat(req: PlanFlatRequest, source_dirs: list[str], extensions: tuple, noun: str,
               folders: bool = False) -> dict:
    warnings: list[str] = []
    items: list[str] = []
    seen: set[str] = set()
    sources = {os.path.normpath(s) for s in source_dirs}

    for path in req.paths:
        # Une collection d'ebooks (dossier directement dans une source) est une unité entière.
        if folders and os.path.isdir(path) and os.path.normpath(os.path.dirname(path)) in sources:
            if path not in seen:
                seen.add(path)
                items.append(path)
            continue
        if not os.path.isfile(path):
            warnings.append(f"Introuvable, ignoré : {path}")
            continue

        item = path
        if req.mode == "parents":
            parent = os.path.dirname(path)
            if security.is_configured_source_dir(parent, source_dirs):
                warnings.append(
                    f"{os.path.basename(path)} est à la racine d'un dossier source ; "
                    f"seul le fichier sera traité (pas tout le dossier)."
                )
            elif not security.folder_is_dedicated(parent, path, extensions):
                warnings.append(
                    f"Le dossier de {os.path.basename(path)} contient d'autres {noun} ; "
                    f"seul le fichier sera traité (pas tout le dossier)."
                )
            else:
                item = parent

        if item not in seen:
            seen.add(item)
            items.append(item)

    total_size = sum(_size_of(i) for i in items)
    return {"items": items, "total_size": total_size, "warnings": warnings, "merge": False}


@app.post("/api/plan/series")
def plan_series(req: PlanSeriesRequest):
    items: list[str] = []
    seen: set[str] = set()

    for _name, paths in req.names_paths.items():
        for p in paths:
            if p not in seen and os.path.isdir(p):
                seen.add(p)
                items.append(p)

    total_size = sum(_size_of(i) for i in items)
    # Toujours en mode fusion : un même nom de série peut exister dans
    # plusieurs sources, toutes les occurrences doivent être rsyncées vers
    # la même destination sans que l'existence de la première ne fasse
    # ignorer les suivantes.
    return {"items": items, "total_size": total_size, "warnings": [], "merge": True}


# ---------------------------------------------------------------------
# Jobs (copier / déplacer / supprimer)
# ---------------------------------------------------------------------

# Références fortes vers les tâches de fond : sans elles, asyncio peut
# ramasser une tâche en cours (garbage collection) et l'opération s'arrête.
_BACKGROUND_TASKS: set = set()


@app.post("/api/jobs")
async def create_job(req: JobRequest, unlocked: bool = Depends(adult_unlocked)):
    if not req.items:
        raise HTTPException(400, "Aucun élément à traiter.")

    items = [os.path.normpath(i) for i in req.items]
    dry = config.settings.dry_run

    # Médias adultes : introuvables sans session déverrouillée.
    if adult_media.hidden_paths(items, unlocked):
        raise _hidden_404()

    # Contrôle commun à TOUTES les opérations : chaque élément doit être
    # strictement à l'intérieur d'un dossier source. Avant, seule la
    # suppression était contrôlée : un déplacement pouvait viser n'importe
    # quel chemin du conteneur (et donc, avec les montages /media et /mnt
    # en :rw, des fichiers de l'hôte).
    outside = [i for i in items if not security.is_inside_source_dirs(i)]
    if outside:
        raise HTTPException(403, f"Élément hors des dossiers sources gérés : {outside[0]}")

    destination_full: Optional[str] = None

    if req.operation == "delete":
        # Confirmation obligatoire côté serveur, pas seulement côté UI.
        if req.confirm_delete_text != config.settings.delete_confirm_text:
            raise HTTPException(
                403,
                f"Confirmation invalide : 'confirm_delete_text' doit valoir exactement "
                f"'{config.settings.delete_confirm_text}'.",
            )

    # Vérifie AVANT toute copie que la source peut être retirée. Sinon un
    # déplacement copierait tout à destination puis échouerait à supprimer la
    # source : doublons et statut ambigu. Ignoré en simulation (rien n'est
    # modifié dans ce cas).
    if req.operation in ("move", "delete") and not dry:
        blocked = [i for i in items if not security.source_is_removable(i)]
        if blocked:
            raise HTTPException(
                409,
                f"Impossible de retirer cet élément de sa source : {blocked[0]}. Le dossier est "
                f"probablement monté en lecture seule (:ro) ou tes droits ne le permettent pas. "
                f"Rien n'a été copié ni supprimé. Passe le montage en :rw dans docker-compose.yml "
                f"puis `docker compose up -d`.",
            )

    if req.operation != "delete":
        if not req.destination:
            raise HTTPException(400, "Destination manquante.")

        dest_root = os.path.normpath(req.destination)

        # Garde-fou : jamais écrire sur la racine du système de fichiers du
        # conteneur, ni directement dans une racine de scan (/media, /mnt...)
        # sans sous-dossier de disque.
        segments = [s for s in dest_root.split("/") if s]
        if dest_root in ("/", ".", "") or len(segments) < 2:
            raise HTTPException(
                400,
                f"Destination invalide ({dest_root!r}) : choisis le dossier d'un disque externe "
                f"(ex. /media/devmon/MonDisque), pas la racine du système de fichiers.",
            )

        if security.is_under_source_dirs(dest_root):
            raise HTTPException(403, "La destination chevauche un dossier source : refusé par sécurité.")

        subdir = DEST_SUBDIR_BY_TYPE[req.media_type]()
        subdir = subdir.strip().strip("/")  # jamais de chemin absolu ici : voir _as_relative_subdir
        # normpath AVANT le contrôle : « disque/../autre » doit être vu comme « autre ».
        destination_full = os.path.normpath(os.path.join(dest_root, subdir))

        full_segments = [s for s in destination_full.split("/") if s]
        dans_le_disque = destination_full == dest_root or destination_full.startswith(dest_root + os.sep)
        if not dans_le_disque or len(full_segments) < 2:
            raise HTTPException(
                400,
                f"Destination finale invalide ({destination_full!r}) : le sous-dossier configuré "
                f"sort du disque choisi. Vérifie le sous-dossier dans Réglages.",
            )

        if security.is_under_source_dirs(destination_full):
            raise HTTPException(403, "La destination chevauche un dossier source : refusé par sécurité.")

        # En simulation, rien n'est créé : on vérifie seulement le plus proche dossier existant.
        ancetre = destination_full
        while not os.path.exists(ancetre) and os.path.dirname(ancetre) != ancetre:
            ancetre = os.path.dirname(ancetre)
        if not dry:
            try:
                os.makedirs(destination_full, exist_ok=True)
            except OSError as exc:
                raise HTTPException(400, f"Impossible de créer la destination : {exc}")

        if not os.access(ancetre, os.W_OK):
            raise HTTPException(
                400,
                f"Le disque de destination n'est pas accessible en écriture : {destination_full}.",
            )

        total_size = sum(_size_of(i) for i in items)
        try:
            free = shutil.disk_usage(ancetre).free
        except OSError as exc:
            raise HTTPException(400, f"Impossible de lire l'espace libre : {exc}")

        usable = max(0, free - config.settings.safety_margin_bytes)
        if total_size > usable:
            raise HTTPException(
                409,
                f"Espace insuffisant : {total_size} octets requis, {usable} octets utilisables "
                f"(marge de sécurité {config.settings.safety_margin_bytes} octets).",
            )

    dest_names = {}
    if destination_full and req.media_type in ("films", "series"):
        # Une copie vers un disque porte le nom identifié (le serveur, lui, garde son nom).
        for it in items:
            named = identities.dest_name(req.media_type, it)
            if named:
                dest_names[it] = named
    dest_dirs = {}
    if destination_full and req.media_type == "films":
        adult_items = [i for i in items if adult_media.is_adult_path(i)]
        if adult_items and not adult_media.adult_subdir():
            # Sans sous-dossier adulte, le film partirait dans MEDIA/FILM, visible sans session.
            raise HTTPException(
                400,
                "Un film pour adultes ne peut être copié que dans un sous-dossier adulte : "
                "renseigne « Destination adulte » dans Réglages.",
            )
        if adult_items:
            # Un film adulte part dans le sous-dossier adulte du disque, pas dans MEDIA/FILM.
            adult_dir = os.path.normpath(os.path.join(os.path.normpath(req.destination),
                                                      adult_media.adult_subdir()))
            if not adult_dir.startswith(os.path.normpath(req.destination) + os.sep):
                raise HTTPException(400, f"Sous-dossier adulte invalide : {adult_dir!r}.")
            if not dry:
                try:
                    os.makedirs(adult_dir, exist_ok=True)
                except OSError as exc:
                    raise HTTPException(400, f"Impossible de créer le dossier adulte : {exc}")
            for it in adult_items:
                dest_dirs[it] = adult_dir
    if destination_full and req.media_type == "series":
        # Une saison part dans le dossier de sa série, sur le disque (MEDIA/SERIES/<série>/<saison>).
        for it in items:
            show = onefichier.season_show(it)
            if show:
                dest_dirs[it] = os.path.join(destination_full,
                                             identities.dest_name("series", show) or os.path.basename(show))
    job = jobs.create_job(
        operation=req.operation,
        media_type=req.media_type,
        items=items,
        destination=destination_full,
        merge=req.merge,
        dest_names=dest_names,
        dest_dirs=dest_dirs,
        priority=req.priority,
        adult=adult_media.any_adult(items),
    )
    _launch(job)
    return {"job_id": job.id}


@app.get("/api/jobs")
def list_jobs(unlocked: bool = Depends(adult_unlocked)):
    return {"jobs": jobs.list_jobs(_hidden_jobs(unlocked))}


@app.post("/api/jobs/{job_id}/priority")
def set_job_priority(job_id: str, req: JobPriorityRequest, unlocked: bool = Depends(adult_unlocked)):
    """Change la priorité d'une tâche qui n'est pas terminée : elle est relue à chaque élément."""
    job = jobs.get_job(job_id)
    if not job or job.id in _hidden_jobs(unlocked):
        raise HTTPException(404, "Job introuvable.")
    if job.status == "done":
        raise HTTPException(409, "Tâche terminée : la priorité ne peut plus être changée.")
    scheduler.set_priority(job, req.priority)
    return {"id": job.id, "priority": job.priority}


@app.get("/api/transfers/status")
def transfers_status(unlocked: bool = Depends(adult_unlocked)):
    """Plage active (ou pause), prochaine ouverture, vitesses et file d'attente."""
    return scheduler.status(_hidden_jobs(unlocked))


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, unlocked: bool = Depends(adult_unlocked)):
    job = jobs.get_job(job_id)
    if not job or job.id in _hidden_jobs(unlocked):
        raise HTTPException(404, "Job introuvable.")
    return {"id": job.id, "operation": job.operation, "status": job.status, "events": job.events}


@app.websocket("/ws/jobs/{job_id}")
async def ws_job(websocket: WebSocket, job_id: str):
    job = jobs.get_job(job_id)
    cookie = websocket.cookies.get(adult_auth.COOKIE)
    if not job or (job.adult and not adult_auth.session_is_valid(cookie)):
        await websocket.close(code=4404)
        return

    await websocket.accept()
    q = job.subscribe()
    try:
        while True:
            event = await q.get()
            # La session peut être fermée pendant la tâche : un élément adulte n'envoie plus rien alors.
            if job.adult and not adult_auth.session_is_valid(cookie):
                await websocket.close(code=4404)
                break
            await websocket.send_json(event)
            if event.get("type") == "job_done":
                break
    except WebSocketDisconnect:
        pass
    finally:
        job.unsubscribe(q)


# ---------------------------------------------------------------------
# 1fichier (sauvegarde premium des films, archives chiffrées)
# ---------------------------------------------------------------------

def _onefichier_error(exc: Exception, default_status: int = 400) -> HTTPException:
    code = getattr(exc, "status", None) or default_status
    return HTTPException(code, str(exc))


def _launch(job) -> None:
    task = asyncio.create_task(jobs.run_job(job))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    # Une opération peut avoir déplacé, copié ou supprimé des fichiers sources.
    task.add_done_callback(lambda _t: scan_cache.invalidate())


ONEFICHIER_TRANSFER_OPS = ("archive_1fichier", "restore_1fichier")


def _transfer_busy() -> Optional[str]:
    """Pourquoi le cache de travail ne peut pas être vidé maintenant, ou None :
    un envoi ou une restauration 1fichier est en cours ou attend son tour."""
    if onefichier.transfer_active():
        return "un envoi 1fichier est en cours"
    for job in jobs.JOBS.values():
        if job.operation in ONEFICHIER_TRANSFER_OPS and job.status in ("pending", "running"):
            return "un envoi ou une restauration 1fichier est en cours ou en attente"
    return None


def _staging_view() -> dict:
    status = onefichier.staging_status()
    status["busy"] = _transfer_busy()
    return status


def _json_rapide(contenu: dict) -> JSONResponse:
    """Réponse JSON directe : évite le passage de FastAPI par jsonable_encoder, qui coûtait environ
    170 ms sur une grande bibliothèque (version 42). Ces contenus sont déjà des types JSON natifs."""
    return JSONResponse(content=contenu)


@app.get("/api/1fichier")
async def onefichier_state(unlocked: bool = Depends(adult_unlocked)):
    """Répond tout de suite avec l'index. Les fiches TMDB manquantes sont
    récupérées en arrière-plan (meta_running) : la bibliothèque n'attend jamais TMDB."""
    try:
        state = await onefichier.library()
    except onefichier.OneFichierError as exc:
        raise _onefichier_error(exc, 500)
    state["entries"] = adult_media.filter_visible(state["entries"], unlocked)  # films adultes masqués sans session
    return _json_rapide({
        "configured": onefichier.is_configured(),
        "index_file": onefichier.index_path(),
        "restore_dirs": {"film": config.settings.restore_dir_films,
                         "serie": config.settings.restore_dir_series,
                         "ebook": config.settings.restore_dir_ebooks},
        # File d'attente : sans session, les envois adultes ne sont pas listés (leur nom ne sort pas).
        "queue": [q for q in jobs.archive_queue() if unlocked or not adult_media.is_adult_path(q["source"])],
        "staging": _staging_view(),
        "index_copies": onefichier.index_status(),
        "checks": onefichier.checks_status(unlocked),
        **state,
    })


class OneFichierExportRequest(BaseModel):
    passphrase: str


class DiskIntegrityRequest(BaseModel):
    root: str
    mode: Literal["build", "verify"]


def _sans_exemples_adultes(valeur):
    """Retire des listes d'exemples (« *_examples », « new_examples ») les fichiers rangés dans le
    sous-dossier adulte. Les compteurs restent : ils ne donnent pas de nom."""
    if isinstance(valeur, dict):
        return {k: ([p for p in v if not (isinstance(p, str) and adult_media.is_adult_folder(os.path.dirname(p)))]
                    if (k == "new_examples" or k.endswith("_examples")) and isinstance(v, list)
                    else _sans_exemples_adultes(v))
                for k, v in valeur.items()}
    if isinstance(valeur, list):
        return [_sans_exemples_adultes(v) for v in valeur]
    return valeur


@app.get("/api/disks/integrity")
def disks_integrity_status(unlocked: bool = Depends(adult_unlocked)):
    """Empreintes enregistrées par disque, et opération en cours."""
    state = disk_integrity.status()
    return state if unlocked else _sans_exemples_adultes(state)


@app.get("/api/disks/health")
def disks_health_status():
    """État matériel (SMART) des disques, rapporté par l'hôte. Lecture seule."""
    return disk_health.status()


@app.get("/api/hosts")
def hosts_status():
    """Hébergeurs de sauvegarde distante : configuration et actions disponibles."""
    return {"hosts": [host.describe() for host in hosts.all_hosts()]}


@app.post("/api/disks/integrity")
async def disks_integrity_start(req: DiskIntegrityRequest):
    try:
        started = await disk_integrity.start(req.root, req.mode)
    except disk_integrity.IntegrityError as exc:
        raise HTTPException(409, str(exc))
    if not started:
        raise HTTPException(409, "une vérification d'empreintes est déjà en cours.")
    return {"ok": True, "started": True}


@app.get("/api/1fichier/checks/status")
def onefichier_checks_status(unlocked: bool = Depends(adult_unlocked)):
    return onefichier.checks_status(unlocked)


@app.post("/api/1fichier/checks/run")
async def onefichier_checks_run():
    """Lance le contrôle des liens et le test de restauration maintenant, en arrière-plan."""
    if not onefichier.is_configured():
        raise HTTPException(400, "Clé API 1fichier non renseignée (Réglages › 1fichier).")
    if onefichier.checks_status()["running"]:
        return {"ok": True, "started": False}
    if onefichier.transfer_active():
        raise HTTPException(409, "un envoi ou une restauration 1fichier est en cours : réessaie après.")
    task = asyncio.create_task(onefichier.run_checks_safely("manuel"))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return {"ok": True, "started": True}


@app.get("/api/1fichier/index/status")
def onefichier_index_status():
    return onefichier.index_status()


@app.post("/api/1fichier/index/export")
async def onefichier_index_export(req: OneFichierExportRequest, unlocked: bool = Depends(adult_unlocked)):
    """Archive 7z chiffrée de l'index, téléchargée par le navigateur. Rien n'est enregistré
    côté serveur, hors une copie temporaire chiffrée supprimée après envoi."""
    # L'index contient aussi les archives adultes (et leurs mots de passe) : pas d'export sans session.
    # Le refus est le même que l'index contienne ou non une archive adulte : un message différent
    # révélerait leur présence à qui n'a pas la session (version 42).
    if not unlocked:
        raise HTTPException(403, "Déverrouille les médias adultes pour exporter l'index.")
    try:
        path, name = await asyncio.to_thread(onefichier.export_index, req.passphrase)
    except onefichier.OneFichierError as exc:
        raise _onefichier_error(exc, 400)
    return FileResponse(path, filename=name, media_type="application/x-7z-compressed",
                        background=BackgroundTask(shutil.rmtree, os.path.dirname(path), True))


@app.post("/api/1fichier/staging/clear")
async def onefichier_staging_clear():
    """Vide le cache de travail (archives temporaires). Refusé tant qu'un transfert
    est en cours ou en attente. Route async : la vérification et la suppression
    s'enchaînent sans attendre, donc aucun transfert ne démarre entre les deux."""
    busy = _transfer_busy()
    if busy:
        raise HTTPException(status_code=409,
                            detail=f"Vidage impossible : {busy}. Réessaie une fois le transfert terminé.")
    result = onefichier.clear_staging()
    return {**result, "staging": _staging_view()}


@app.post("/api/1fichier/refresh-meta")
async def onefichier_refresh_meta():
    """Recharge toutes les fiches TMDB, en arrière-plan."""
    return {"ok": True, "started": onefichier.start_meta_refresh_all()}


@app.post("/api/1fichier/test")
async def onefichier_test():
    try:
        return {"ok": True, "account": await onefichier.test_connection()}
    except onefichier.OneFichierError as exc:
        raise _onefichier_error(exc, 502)


@app.post("/api/1fichier/archive")
async def onefichier_archive(req: OneFichierArchiveRequest, unlocked: bool = Depends(adult_unlocked)):
    if not req.paths:
        raise HTTPException(400, "Aucun élément sélectionné.")
    if not onefichier.is_configured():
        raise HTTPException(400, "Clé API 1fichier non renseignée (Réglages › 1fichier).")

    if adult_media.hidden_paths([os.path.normpath(p) for p in req.paths], unlocked):
        raise _hidden_404()
    units: list[str] = []
    seen: set[str] = set()
    series_names: set[str] = set()
    for raw in req.paths:
        path = os.path.normpath(raw)
        if not os.path.lexists(path):
            raise HTTPException(400, f"Introuvable : {path}")
        try:
            kind, unit = onefichier.classify(path)
        except onefichier.OneFichierError as exc:
            raise _onefichier_error(exc, 403)
        if kind == onefichier.KIND_EBOOK and not onefichier.ebook_files(unit):
            raise HTTPException(400, f"Aucun ebook dans ce dossier : {unit}")
        if kind == onefichier.KIND_SERIE and not onefichier.season_show(unit):
            name = os.path.basename(unit)
            if name in series_names:
                raise HTTPException(400, f"La série « {name} » existe à plusieurs emplacements : "
                                         f"envoie-la depuis un seul dossier source.")
            series_names.add(name)
        if unit not in seen:  # un livre et son dossier ne donnent qu'un seul envoi
            seen.add(unit)
            units.append(unit)

    kinds = {onefichier.classify(u)[0] for u in units}
    if kinds & {onefichier.KIND_FILM, onefichier.KIND_SERIE} and not config.settings.tmdb_enabled:
        raise HTTPException(400, "TMDB doit être activé : il sert à nommer les dossiers films et séries.")

    after = req.after
    dry = config.settings.dry_run
    if after != "keep":
        if kinds - {onefichier.KIND_FILM, onefichier.KIND_SERIE}:
            raise HTTPException(400, "Libérer le serveur n'est possible que pour les films et les séries.")
        if after == "delete" and req.confirm_delete_text != config.settings.delete_confirm_text:
            raise HTTPException(403, f"Confirmation invalide : tape exactement '{config.settings.delete_confirm_text}'.")
        if not dry:
            blocked = [u for u in units if not security.source_is_removable(u)]
            if blocked:
                raise HTTPException(
                    409,
                    f"Impossible de retirer cet élément de sa source : {blocked[0]}. Le dossier est "
                    f"probablement monté en lecture seule (:ro). Rien n'a été envoyé ni supprimé.",
                )
    if after == "move":
        if not req.destination:
            raise HTTPException(400, "Choisis le disque de destination.")
        for kind in kinds:
            try:
                onefichier._disk_dest(kind, req.destination)  # disque détecté et monté, sinon erreur
            except onefichier.OneFichierError as exc:
                raise _onefichier_error(exc, 400)

    job = jobs.create_job(operation="archive_1fichier", media_type="films", items=units,
                          destination=req.destination if after == "move" else None,
                          priority=req.priority, adult=adult_media.any_adult(units))
    job.after = after
    _launch(job)
    return {"job_id": job.id}


@app.post("/api/1fichier/restore")
async def onefichier_restore(req: OneFichierRestoreRequest, unlocked: bool = Depends(adult_unlocked)):
    if not req.keys:
        raise HTTPException(400, "Aucun élément sélectionné.")
    if not onefichier.is_configured():
        raise HTTPException(400, "Clé API 1fichier non renseignée (Réglages › 1fichier).")

    try:
        entries = {e["key"]: e for e in onefichier.list_entries()}
    except onefichier.OneFichierError as exc:
        raise _onefichier_error(exc, 500)
    marks = adult_media.marked_paths()  # lues une fois pour toute la sélection
    for key in req.keys:
        if not onefichier.valid_key(key) or key not in entries:
            raise HTTPException(404, f"Élément absent de l'index 1fichier : {key}")
        if not adult_media.visible(entries[key], unlocked, marks):
            raise _hidden_404()
        if entries[key].get("status") != "done":
            raise HTTPException(409, f"Envoi incomplet, restauration impossible : {key}")

    disk = None
    if req.target == "disk":
        if not req.disk:
            raise HTTPException(400, "Choisis le disque externe de restauration.")
        known = {os.path.normpath(d["path"]) for d in disks.list_destination_disks()}
        disk = os.path.normpath(req.disk)
        if disk not in known:
            raise HTTPException(400, f"Disque non détecté ou non monté : {disk}")

    job = jobs.create_job(operation="restore_1fichier", media_type="films",
                          items=list(req.keys), destination=onefichier.encode_target(req.target, disk),
                          priority=req.priority,
                          adult=any(adult_media.is_adult_item(entries[k], marks) for k in req.keys))
    _launch(job)
    return {"job_id": job.id}


@app.get("/api/1fichier/covers/{name}")
def onefichier_cover(name: str):
    """Jaquette d'un ebook (tirée de son premier livre)."""
    path = onefichier.cover_path(name)
    if not path:
        raise HTTPException(404, "Jaquette introuvable.")
    return FileResponse(path, headers={"Cache-Control": "max-age=86400"})


@app.delete("/api/1fichier/entries/{key}")
def onefichier_forget(key: str, unlocked: bool = Depends(adult_unlocked)):
    """Retire de l'index un envoi resté incomplet (jamais un envoi terminé)."""
    if not onefichier.valid_key(key):
        raise HTTPException(400, "Identifiant invalide.")
    _visible_entry_or_404(key, unlocked)
    try:
        onefichier.forget_pending(key)
    except onefichier.OneFichierError as exc:
        raise _onefichier_error(exc, 409)
    return {"ok": True}


@app.post("/api/1fichier/entries/{key}/recover")
async def onefichier_recover(key: str, unlocked: bool = Depends(adult_unlocked)):
    """Retrouve sur 1fichier l'archive d'un envoi resté incomplet dans l'index."""
    if not onefichier.valid_key(key):
        raise HTTPException(400, "Identifiant invalide.")
    _visible_entry_or_404(key, unlocked)
    try:
        result = await onefichier.recover_pending(key)
    except onefichier.OneFichierError as exc:
        raise _onefichier_error(exc, 502)
    return {"ok": True, **result}


@app.get("/api/1fichier/entries/{key}/access")
def onefichier_access(key: str, unlocked: bool = Depends(adult_unlocked)):
    """Lien et mot de passe d'une archive, demandés explicitement par l'interface."""
    if not onefichier.valid_key(key):
        raise HTTPException(400, "Identifiant invalide.")
    _visible_entry_or_404(key, unlocked)
    try:
        return {"ok": True, **onefichier.reveal_entry(key)}
    except onefichier.OneFichierError as exc:
        raise _onefichier_error(exc, 409)


@app.post("/api/1fichier/entries/{key}/check")
async def onefichier_check(key: str, unlocked: bool = Depends(adult_unlocked)):
    """Vérifie que l'archive existe toujours sur 1fichier et lit sa date d'envoi."""
    if not onefichier.valid_key(key):
        raise HTTPException(400, "Identifiant invalide.")
    _visible_entry_or_404(key, unlocked)
    try:
        result = await onefichier.check_entry(key)
    except onefichier.OneFichierError as exc:
        raise _onefichier_error(exc, 502)
    return {"ok": True, **result}


@app.get("/api/series/seasons")
def series_seasons(path: str):
    """Saisons d'une série (dossier de la série), avec leur état sur 1fichier."""
    show = os.path.normpath(path)
    if not os.path.isdir(show):
        raise HTTPException(404, f"Dossier introuvable : {show}")
    try:
        kind, unit = onefichier.classify(show)
    except onefichier.OneFichierError as exc:
        raise _onefichier_error(exc, 403)
    if kind != onefichier.KIND_SERIE or unit != show or onefichier.season_show(show):
        raise HTTPException(400, "Sélectionne le dossier d'une série (pas une saison).")
    info = series_mod.seasons_of(show)
    tmdb_id = identities.tmdb_id_for("series", show) or onefichier.archived_tmdb_id(show)
    sent = onefichier.seasons_sent(tmdb_id)
    whole = "*" in sent  # série envoyée en entier : toutes ses saisons sont sur 1fichier
    seasons = [{**s, "sent": whole or (s["season"] is not None and s["season"] in sent)} for s in info["seasons"]]
    return {"name": os.path.basename(show), "show": show, "seasons": seasons,
            "root_episodes": info["root_episodes"], "root_size": info["root_size"],
            "whole_sent": whole}


async def _film_sources(unlocked: bool) -> list[dict]:
    """Chaque film possédé, où qu'il soit (serveur, disque connecté ou non, 1fichier),
    avec son identifiant TMDB quand on le connaît. Sert aux sagas."""
    out: list[dict] = []
    search_items, _meta = await scan_cache.search("films", "")
    for it in adult_media.filter_visible(search_items, unlocked):
        tid = it.get("tmdb_id") or identities.tmdb_id_for("films", it["path"])
        out.append({"tmdb_id": tid, "where": "serveur", "label": "Serveur",
                    "title": it.get("title"), "year": it.get("year"), "path": it["path"]})
    disks_list = await asyncio.to_thread(disk_library.refresh, False, frozenset())
    for disk in disks_list:
        for it in adult_media.filter_visible((disk.get("items") or {}).get("films", []), unlocked):
            meta = it.get("meta") or {}
            tid = meta.get("tmdb_id") or identities.tmdb_id_for("films", it["path"])
            out.append({"tmdb_id": tid, "where": "disque", "label": f"Disque {disk['label']}",
                        "title": meta.get("title") or it["name"], "year": meta.get("year"), "path": it["path"]})
    for entry in await asyncio.to_thread(onefichier.list_entries):
        if entry.get("status") != "done" or entry.get("kind") != onefichier.KIND_FILM:
            continue
        if not adult_media.visible(entry, unlocked):
            continue
        key = str(entry.get("key", ""))
        tid = int(key) if key.isdigit() else entry.get("tmdb_id")
        meta = entry.get("meta") or {}
        out.append({"tmdb_id": tid, "where": "1fichier", "label": "1fichier",
                    "title": meta.get("title") or entry.get("title"), "year": entry.get("year"), "path": None})
    return out


async def _owned_by_tmdb(unlocked: bool) -> dict[int, list[dict]]:
    """Identifiant TMDB -> lieux où le film est possédé : {where, term}, où `term` est ce qu'il faut
    chercher dans la bibliothèque (nom du fichier sur le serveur et le disque, titre pour 1fichier).
    Le serveur passe en premier. Sans session déverrouillée, les films adultes n'y figurent pas."""
    found: dict[int, dict] = {}
    for film in await _film_sources(unlocked):
        try:
            tid = int(film["tmdb_id"])
        except (TypeError, ValueError):
            continue
        places = found.setdefault(tid, {})
        if film["where"] in places:
            continue
        places[film["where"]] = os.path.basename(film["path"]) if film.get("path") else (film.get("title") or "")
    order = {"serveur": 0, "disque": 1, "1fichier": 2}
    return {tid: [{"where": w, "term": t} for w, t in sorted(places.items(), key=lambda kv: order.get(kv[0], 9))]
            for tid, places in found.items()}


@app.get("/api/sagas")
async def get_sagas(unlocked: bool = Depends(adult_unlocked)):
    """Sagas de films (collections TMDB) : affiche, films possédés et films manquants.
    Les fiches TMDB manquantes se récupèrent en arrière-plan (running)."""
    films = await _film_sources(unlocked)
    ids = sorted({int(f["tmdb_id"]) for f in films if f.get("tmdb_id") is not None})
    sagas_mod.start(ids)
    return {
        "sagas": sagas_mod.groups(films),
        "running": sagas_mod.running(),
        "tmdb_enabled": config.settings.tmdb_enabled,
        "films_total": len(films),
        "films_without_tmdb": sum(1 for f in films if f.get("tmdb_id") is None),
    }


# ---------------------------------------------------------------------
# Disques de sauvegarde (bibliothèque sur disque externe)
# ---------------------------------------------------------------------

def _disk_items(paths: list[str]) -> tuple[list[str], set[str]]:
    """Vérifie que chaque chemin est un élément d'un disque CONNECTÉ (enfant direct
    d'un sous-dossier MEDIA). Renvoie les chemins normalisés et les types touchés."""
    if not paths:
        raise HTTPException(400, "Aucun élément sélectionné.")
    items: list[str] = []
    kinds: set[str] = set()
    for raw in paths:
        path = os.path.normpath(raw)
        loc = disk_library.locate(path)
        if not loc:
            raise HTTPException(
                403,
                f"Élément absent d'un disque connecté, ou hors des dossiers MEDIA : {path}. "
                f"Rebranche le disque puis rafraîchis la liste.",
            )
        if not os.path.lexists(path):
            raise HTTPException(400, f"Introuvable : {path}")
        items.append(path)
        kinds.add(loc[0])
    return items, kinds


def _disk_names_on_1fichier() -> frozenset:
    """Noms (dossier 1fichier et nom d'origine) des envois terminés."""
    names: set[str] = set()
    for entry in onefichier.list_entries():
        if entry.get("status") == "done" and entry.get("season") is None:  # une saison ne désigne pas un dossier de disque
            for field in ("original_name", "folder"):
                if entry.get(field):
                    names.add(str(entry[field]))
    return frozenset(names)


@app.get("/api/disk-library")
async def get_disk_library(force: bool = False, unlocked: bool = Depends(adult_unlocked)):
    """Disques connectés (scannés) et déjà connus (hors ligne, lecture seule)."""
    names = await asyncio.to_thread(_disk_names_on_1fichier)
    disks_list = await asyncio.to_thread(disk_library.refresh, force, names)
    # Scan d'un disque en cours (version 43) : l'interface relit la liste tant qu'il tourne.
    scanning = disk_library.scans_en_cours()
    # Les fiches TMDB et jaquettes se complètent en arrière-plan : la liste ne les attend pas.
    meta_running = (await disk_meta.kick()) or scanning
    for disk in disks_list:  # copies de films adultes masquées tant que l'affichage est désactivé
        if (disk.get("items") or {}).get("films"):
            disk["items"]["films"] = adult_media.filter_visible(disk["items"]["films"], unlocked)
    return _json_rapide({
        "disks": disks_list,
        "meta_running": meta_running,
        "scanning": scanning,
        "server_dirs": {
            "films": config.settings.source_dirs_films,
            "series": config.settings.source_dirs_series,
            "ebooks": config.settings.source_dirs_ebooks,
        },
        "delete_confirm_text": config.settings.delete_confirm_text,
        "dry_run": config.settings.dry_run,
        "onefichier_configured": onefichier.is_configured(),
    })


@app.post("/api/disk-library/transfer")
async def disk_transfer(req: DiskTransferRequest, unlocked: bool = Depends(adult_unlocked)):
    """Copie ou déplace des éléments d'un disque connecté vers un dossier source du serveur."""
    items, kinds = _disk_items(req.items)
    adult = _disk_items_for_session(items, unlocked)
    if len(kinds) != 1:
        raise HTTPException(400, "Sélectionne des éléments d'un seul type (films, séries ou ebooks).")
    kind = kinds.pop()
    dry = config.settings.dry_run

    allowed = getattr(config.settings, f"source_dirs_{kind}")
    dest = os.path.normpath(req.server_dir or "")
    if dest not in [os.path.normpath(d) for d in allowed]:
        raise HTTPException(400, f"Dossier serveur non configuré pour ce type : {dest or '(aucun)'}")
    if not os.path.isdir(dest):
        raise HTTPException(400, f"Dossier serveur introuvable : {dest}")
    if not dry and not os.access(dest, os.W_OK):
        raise HTTPException(400, f"Dossier serveur non accessible en écriture : {dest}")

    if req.mode == "move" and not dry:
        blocked = [i for i in items if not security.source_is_removable(i)]
        if blocked:
            raise HTTPException(
                409,
                f"Impossible de retirer cet élément du disque : {blocked[0]}. Le disque est probablement "
                f"monté en lecture seule. Rien n'a été copié ni déplacé.",
            )

    total = sum(_size_of(i) for i in items)
    try:
        free = shutil.disk_usage(dest).free
    except OSError as exc:
        raise HTTPException(400, f"Impossible de lire l'espace libre du serveur : {exc}")
    usable = max(0, free - config.settings.safety_margin_bytes)
    if total > usable and not dry:
        raise HTTPException(
            409,
            f"Espace insuffisant sur le serveur : {total} octets requis, {usable} octets utilisables.",
        )

    job = jobs.create_job(operation=req.mode, media_type=kind, items=items, destination=dest,
                          priority=req.priority, adult=adult)
    _launch(job)
    return {"job_id": job.id}


@app.post("/api/disk-library/delete")
async def disk_delete(req: DiskItemsRequest, unlocked: bool = Depends(adult_unlocked)):
    """Supprime des éléments d'un disque connecté (confirmation tapée obligatoire)."""
    items, kinds = _disk_items(req.items)
    adult = _disk_items_for_session(items, unlocked)
    if req.confirm_delete_text != config.settings.delete_confirm_text:
        raise HTTPException(
            403,
            f"Confirmation invalide : tape exactement '{config.settings.delete_confirm_text}'.",
        )
    if not config.settings.dry_run:
        blocked = [i for i in items if not security.source_is_removable(i)]
        if blocked:
            raise HTTPException(
                409,
                f"Impossible de supprimer cet élément du disque : {blocked[0]}. Le disque est probablement "
                f"monté en lecture seule. Rien n'a été supprimé.",
            )
    job = jobs.create_job(operation="delete", media_type=sorted(kinds)[0], items=items, destination=None,
                          adult=adult)
    _launch(job)
    return {"job_id": job.id}


# Caractères refusés par exFAT et NTFS (les disques de sauvegarde le sont souvent).
_FORBIDDEN_NAME_CHARS = set('<>:"/\\|?*')


def _clean_disk_name(raw: str, old_path: str) -> str:
    """Nom final d'un élément renommé. Pour un fichier, l'extension d'origine est
    toujours conservée (un .mkv reste un .mkv, que l'utilisateur la tape ou non)."""
    is_dir = os.path.isdir(old_path) and not os.path.islink(old_path)
    base = (raw or "").strip()
    if not is_dir:
        ext = os.path.splitext(os.path.basename(old_path))[1]
        # L'extension tapée (même répétée, « x.mkv.mkv ») est retirée avant d'être remise une seule fois.
        while ext and base.lower().endswith(ext.lower()):
            base = base[: -len(ext)].rstrip(" .")
        base = base.rstrip(" .") + ext
    else:
        base = base.rstrip(" .")
    if not base or base.startswith(".") or base in (".", ".."):
        raise HTTPException(400, "Nom invalide : il ne peut pas être vide ni commencer par un point.")
    bad = sorted({c for c in base if c in _FORBIDDEN_NAME_CHARS or ord(c) < 32})
    if bad:
        raise HTTPException(400, f"Caractères interdits sur les disques : {' '.join(bad)}")
    if len(base.encode("utf-8")) > 240:
        raise HTTPException(400, "Nom trop long (240 octets au maximum).")
    return base


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)  # ex. changement de casse seul sur un disque insensible à la casse
    except OSError:
        return False


@app.post("/api/disk-library/rename")
def disk_rename(req: DiskRenameRequest, unlocked: bool = Depends(adult_unlocked)):
    """Renomme un élément d'un disque connecté, par exemple pour qu'il soit reconnu par TMDB."""
    items, _kinds = _disk_items([req.path])
    _disk_items_for_session(items, unlocked)
    old = items[0]
    loc = disk_library.locate(old)
    if not loc:
        raise HTTPException(403, "Le disque n'est plus connecté : rebranche-le puis rafraîchis la liste.")
    root = loc[1]
    name = _clean_disk_name(req.new_name, old)
    new = os.path.join(os.path.dirname(old), name)
    if new == old:
        return {"ok": True, "path": old, "name": name, "unchanged": True}
    if os.path.lexists(new) and not _same_file(old, new):
        raise HTTPException(409, f"Un élément porte déjà ce nom dans ce dossier : {name}")
    if config.settings.dry_run:
        log_action("SIMULE", "RENOMMER", f"{old} -> {new}")
        return {"ok": True, "dry_run": True, "path": new, "name": name}
    if not security.source_is_removable(old):
        raise HTTPException(
            409,
            f"Impossible de renommer cet élément : {old}. Le disque est probablement monté en lecture seule.",
        )
    try:
        os.rename(old, new)
    except OSError as exc:
        log_action("ECHEC", "RENOMMER", f"{old} -> {new} ({exc})")
        raise HTTPException(409, f"Renommage impossible : {exc}")
    disk_library.renamed(root, old, new)
    log_action("OK", "RENOMMER", f"{old} -> {new}")
    return {"ok": True, "path": new, "name": name}


class DiskCategoryRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=200)
    adult: bool


@app.post("/api/disk-library/category")
def disk_category(req: DiskCategoryRequest, unlocked: bool = Depends(adult_unlocked)):
    """Déplace des films d'un disque connecté vers son sous-dossier adulte (adult=true), ou vers le
    dossier films du disque (adult=false). Le déplacement reste sur le même disque : rien n'est copié."""
    if not unlocked:
        raise _hidden_404()
    items, kinds = _disk_items(req.paths)
    if kinds != {"films"}:
        raise HTTPException(400, "Seuls des films se rangent dans la catégorie Adultes.")
    _disk_items_for_session(items, unlocked)
    sub = adult_media.adult_subdir() if req.adult else (config.settings.dest_subdir_films or "").strip().strip("/")
    if not sub:
        raise HTTPException(400, "Sous-dossier non configuré pour cette catégorie (Réglages › Destination).")
    plan = []  # (ancien chemin, nouveau chemin, racine du disque)
    for old in items:
        loc = disk_library.locate(old)
        if not loc:
            raise HTTPException(403, "Le disque n'est plus connecté : rebranche-le puis rafraîchis la liste.")
        root = loc[1]
        if adult_media.disk_path_is_adult(old, root) == req.adult:
            continue  # déjà dans la bonne catégorie
        new = os.path.join(root, sub, os.path.basename(old))
        if os.path.lexists(new):
            raise HTTPException(409, f"Un élément porte déjà ce nom dans {sub} : {os.path.basename(old)}. Rien n'a été déplacé.")
        if not security.source_is_removable(old):
            raise HTTPException(409, f"Impossible de déplacer cet élément : {old}. Le disque est probablement monté en lecture seule. Rien n'a été déplacé.")
        plan.append((old, new, root))
    if config.settings.dry_run:
        for old, new, _root in plan:
            log_action("SIMULE", "CATEGORIE", f"{old} -> {new}")
        return {"ok": True, "dry_run": True, "moved": [new for _o, new, _r in plan], "errors": [], "unchanged": len(items) - len(plan)}
    moved, errors = [], []
    for old, new, root in plan:
        try:
            os.makedirs(os.path.dirname(new), exist_ok=True)
            os.rename(old, new)
        except OSError as exc:
            errors.append({"path": old, "error": str(exc)})
            log_action("ECHEC", "CATEGORIE", f"{old} -> {new} ({exc})")
            continue
        disk_library.moved(root, old, new)
        log_action("OK", "CATEGORIE", f"{old} -> {new}")
        moved.append(new)
    return {"ok": not errors, "moved": moved, "errors": errors, "unchanged": len(items) - len(plan)}


# ---------------------------------------------------------------------
# Renommage dans la bibliothèque serveur (films et séries)
# ---------------------------------------------------------------------

class LibraryIdentifyRequest(BaseModel):
    rename_file: bool = False          # films : renommer aussi le fichier sur le serveur (un seul à la fois)
    kind: Literal["films", "series"]
    paths: list[str]                   # films : le fichier vidéo ; séries : chaque dossier de la série
    new_name: str                      # « Titre (année) » choisi ; l'extension éventuelle est ignorée
    tmdb_id: Optional[int] = None      # résultat TMDB choisi, s'il y en a un
    adult: Optional[bool] = None       # films : « contenu adulte » coché (None = inchangé)


def _library_item_ok(p: str, kind: str) -> None:
    """Un film (fichier ou dossier) strictement dans un dossier films, ou une série
    (dossier directement dans un dossier séries). Rien d'autre."""
    sources = [os.path.normpath(s) for s in (config.settings.source_dirs_films + adult_media.adult_roots()
                                             if kind == "films" else config.settings.source_dirs_series)]
    if kind == "films":
        ok = any(p.startswith(s + os.sep) for s in sources)
    else:
        ok = os.path.normpath(os.path.dirname(p)) in sources and os.path.isdir(p)
    if not ok:
        raise HTTPException(403, f"Élément hors de la bibliothèque {('films' if kind == 'films' else 'séries')} : {p}")
    if not os.path.lexists(p):
        raise HTTPException(400, f"Introuvable : {p}")


class LibraryCategoryRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=200)
    adult: bool


@app.post("/api/library/identify")
def library_identify(req: LibraryIdentifyRequest, unlocked: bool = Depends(adult_unlocked)):
    """Identifie des films ou une série de la bibliothèque. Par défaut RIEN n'est renommé sur le
    serveur : l'identification reste dans l'application (recherche TMDB, nom des copies envoyées vers
    un disque). `rename_file` (un seul film à la fois) renomme aussi le fichier ou le dossier sur le
    serveur ; Plex doit alors être actualisé."""
    if not req.paths:
        raise HTTPException(400, "Aucun élément sélectionné.")
    olds = [os.path.normpath(p) for p in req.paths]
    for p in olds:
        _library_item_ok(p, req.kind)
    if req.kind == "films" and adult_media.hidden_paths(olds, unlocked):
        raise _hidden_404()
    if req.rename_file and (req.kind != "films" or len(olds) != 1):
        raise HTTPException(400, "Le renommage du fichier ne porte que sur un film à la fois.")
    name = _clean_disk_name(req.new_name, olds[0])
    base = os.path.splitext(name)[0] if not os.path.isdir(olds[0]) else name
    title, year = guess_title_year(base)
    record = {"name": base, "title": title or base, "year": year, "tmdb_id": req.tmdb_id}
    new_path = None
    if req.rename_file:
        target = os.path.join(os.path.dirname(olds[0]), name)
        if target != olds[0]:
            if os.path.lexists(target):
                raise HTTPException(409, f"Un élément porte déjà ce nom dans ce dossier : {name}")
            if config.settings.dry_run:
                log_action("SIMULE", "RENOMMER", f"{olds[0]} -> {target}")
            else:
                if not os.access(os.path.dirname(olds[0]), os.W_OK):
                    raise HTTPException(409, "Le dossier du film n'est pas accessible en écriture.")
                try:
                    os.rename(olds[0], target)
                except OSError as exc:
                    log_action("ECHEC", "RENOMMER", f"{olds[0]} -> {target} ({exc})")
                    raise HTTPException(409, f"Renommage impossible : {exc}")
                identities.move(req.kind, olds[0], target)
                adult_media.move_mark(olds[0], target)
                log_action("OK", "RENOMMER", f"{olds[0]} -> {target}")
                new_path = target
                olds = [target]
    keys = olds if req.kind == "films" else [olds[0]]
    identities.set_many(req.kind, keys, record)
    if req.kind == "films" and req.adult is not None:
        # Une case décochée retire la marque, mais ne retire pas un film qui est dans un dossier adulte.
        adult_media.set_marked(keys, req.adult)
    for p in olds:
        log_action("OK", "IDENTIFIER", f"{p} -> {base}")
    scan_cache.invalidate(("films", "series"))
    adult_flag = req.kind == "films" and adult_media.is_adult_path(olds[0])
    return {"ok": True, "name": base, "title": record["title"], "year": year, "tmdb_id": req.tmdb_id,
            "adult": adult_flag, "renamed_path": new_path, "plex_refresh": new_path is not None}


@app.post("/api/library/category")
def library_category(req: LibraryCategoryRequest, unlocked: bool = Depends(adult_unlocked)):
    """Range des films du serveur dans la catégorie Adultes (adult=true) ou les en sort (adult=false).
    Le fichier ne bouge pas : la catégorie est une marque gardée par l'application, Plex n'est pas
    touché. Sans session déverrouillée, la route répond comme si rien n'existait."""
    if not unlocked:
        raise _hidden_404()
    olds = [os.path.normpath(p) for p in req.paths]
    for p in olds:
        _library_item_ok(p, "films")
    if not req.adult and any(adult_media.in_adult_roots(p) for p in olds):
        raise HTTPException(409, "Un film rangé dans le dossier adulte du serveur ne peut pas sortir de la catégorie Adultes.")
    adult_media.set_marked(olds, req.adult)
    scan_cache.invalidate(("films",))
    for p in olds:
        log_action("OK", "CATEGORIE", f"{p} -> {'Adultes' if req.adult else 'Films'}")
    return {"ok": True, "adult": req.adult, "count": len(olds)}


# ---------------------------------------------------------------------
# Recherche TMDB pour nommer un élément (renommage depuis la bibliothèque disque)
# ---------------------------------------------------------------------

@app.get("/api/tmdb/cast")
async def tmdb_cast(tmdb_id: int = Query(gt=0), unlocked: bool = Depends(adult_unlocked)):
    """Distribution d'un film (nom, rôle, petite photo des premiers rôles). Sans session déverrouillée,
    un film pour adultes donne une distribution vide, comme un identifiant inconnu."""
    if not unlocked and (await tmdb.credits(tmdb_id)).get("adult"):
        return {"cast": []}
    return {"cast": await tmdb.cast(tmdb_id)}


@app.get("/api/tmdb/credits")
async def tmdb_credits(tmdb_id: int = Query(gt=0), unlocked: bool = Depends(adult_unlocked)):
    """Fiche technique d'un film : réalisateurs, scénaristes, studios et distribution (voir tmdb.credits).
    Sans session déverrouillée, un film pour adultes répond comme un identifiant inconnu : une fiche
    vide, qui ne dit ni qu'il existe ni ce qu'il est. Le drapeau TMDB n'est jamais renvoyé."""
    data = await tmdb.credits(tmdb_id)
    if data.get("adult") and not unlocked:
        return tmdb.empty_credits()
    return {key: value for key, value in data.items() if key != "adult"}  # copie : le cache n'est pas modifié


@app.get("/api/tmdb/person")
async def tmdb_person(person_id: int = Query(gt=0), unlocked: bool = Depends(adult_unlocked)):
    """Fiche d'une personne et sa filmographie. Chaque film indique où il se trouve dans la
    bibliothèque (`owned` : serveur, disque, 1fichier). Sans session, les titres pour adultes
    sont retirés et ne comptent dans aucun décompte, ni dans la présence."""
    if not config.settings.tmdb_enabled:
        raise HTTPException(409, "TMDB est désactivé : renseigne la clé API TMDB dans Réglages.")
    try:
        data = await tmdb.person(person_id, unlocked)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"TMDB n'a pas répondu correctement ({_sans_secret(str(exc))}).")
    if data is None:
        raise HTTPException(404, "Personne introuvable sur TMDB.")
    owned = await _owned_by_tmdb(unlocked)
    for film in data["films"]:
        film["owned"] = owned.get(film["tmdb_id"], [])
    data["owned_count"] = sum(1 for f in data["films"] if f["owned"])
    return data


@app.get("/api/tmdb/guess")
def tmdb_guess(name: str):
    """Titre et année lus dans un nom de fichier ou de dossier, pour préremplir la recherche."""
    title, year = guess_title_year(name)
    return {"title": title, "year": year}


@app.get("/api/tmdb/search")
async def tmdb_search(kind: Literal["films", "series"], q: str, year: Optional[int] = None,
                      adult: bool = False, unlocked: bool = Depends(adult_unlocked)):
    """Plusieurs résultats TMDB (affiche, année, résumé) pour choisir le bon titre.
    `adult` : inclut les titres pour adultes (case « Contenu adulte » de l'identification)."""
    query = (q or "").strip()
    if not query:
        raise HTTPException(400, "Saisis un titre à rechercher.")
    if not config.settings.tmdb_enabled:
        raise HTTPException(409, "TMDB est désactivé : renseigne la clé API TMDB dans Réglages.")
    if adult and kind == "films" and not unlocked:
        raise HTTPException(403, "Déverrouille les médias adultes pour chercher des titres adultes.")
    try:
        results = await tmdb.search("movie" if kind == "films" else "tv", query, year,
                                    adult=adult and kind == "films")
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"TMDB n'a pas répondu correctement ({_sans_secret(str(exc))}). Vérifie la clé API.")
    return {"results": results}


@app.post("/api/disk-library/forget")
def disk_forget(req: DiskForgetRequest):
    """Retire de la liste un disque hors ligne. Un disque connecté ne peut pas être oublié."""
    try:
        removed = disk_library.forget(req.path)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    if not removed:
        raise HTTPException(404, "Disque inconnu de la liste.")
    return {"ok": True}


# ---------------------------------------------------------------------
# Jaquettes des ebooks (dossier = une collection)
# ---------------------------------------------------------------------

COVER_UPLOAD_MAX_BYTES = 5 * 1024 * 1024
_NO_LOCAL_COVER: dict[str, float] = {}  # clé -> instant où aucun livre n'avait de jaquette
NO_LOCAL_COVER_RETRY = 3600


class EbookCoverUpload(BaseModel):
    path: str                          # dossier d'ebooks (ou livre isolé) de la bibliothèque ou d'un disque
    data: str                          # image encodée en base64 (une URL data: est acceptée)


def _ebook_item_path(raw: str) -> str:
    """Un ebook de la bibliothèque serveur ou d'un disque connecté, et rien d'autre."""
    p = os.path.normpath(raw or "")
    if p in ("", ".", "/"):
        raise HTTPException(400, "Chemin manquant.")
    in_source = any(p.startswith(os.path.normpath(s) + os.sep) for s in config.settings.source_dirs_ebooks)
    loc = disk_library.locate(p)
    if not (in_source or (loc and loc[0] == "ebooks")):
        raise HTTPException(403, "Ce chemin n'est pas un ebook de la bibliothèque ni d'un disque connecté.")
    if not os.path.lexists(p):
        raise HTTPException(404, "Introuvable.")
    return p


def _local_cover_name(path: str) -> str | None:
    """Jaquette tirée des livres du dossier (mise en cache sur disque)."""
    key = onefichier.ebook_key(path)
    name = onefichier.find_cover(key)
    if name:
        return name
    last = _NO_LOCAL_COVER.get(key)
    if last is not None and time.time() - last < NO_LOCAL_COVER_RETRY:
        return None
    files = onefichier.ebook_files(path) if os.path.isdir(path) else [path]
    found = onefichier.cover_from_books(path, key, files) if files else None
    if found:
        return found[0]
    _NO_LOCAL_COVER[key] = time.time()
    return None


@app.get("/api/ebooks/cover")
async def ebook_cover(path: str):
    """Jaquette d'une collection d'ebooks : importée, tirée d'un livre, ou trouvée en ligne."""
    p = _ebook_item_path(path)
    name = await asyncio.to_thread(_local_cover_name, p)
    if not name:
        title, author = ebooks_mod.title_author(p)
        data = await ebook_online.find_cover(onefichier.ebook_key(p), title, author)
        if data:
            name = await asyncio.to_thread(onefichier.store_cover_bytes, onefichier.ebook_key(p), data)
    if not name or not onefichier.cover_path(name):
        raise HTTPException(404, "Pas de jaquette pour cet ebook.")
    return FileResponse(onefichier.cover_path(name), headers={"Cache-Control": "no-cache"})


@app.post("/api/ebooks/cover")
def ebook_cover_upload(req: EbookCoverUpload):
    """Importe une image comme jaquette d'une collection (remplace celle qui existait)."""
    p = _ebook_item_path(req.path)
    raw = (req.data or "").split(",", 1)[-1]
    if len(raw) > COVER_UPLOAD_MAX_BYTES * 4 // 3 + 4:
        raise HTTPException(413, "Image trop volumineuse (5 Mo au maximum).")
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception:
        raise HTTPException(400, "Image illisible.")
    if len(data) > COVER_UPLOAD_MAX_BYTES:
        raise HTTPException(413, "Image trop volumineuse (5 Mo au maximum).")
    key = onefichier.ebook_key(p)
    try:
        name = onefichier.store_cover_bytes(key, data)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    _NO_LOCAL_COVER.pop(key, None)
    poster = f"/api/1fichier/covers/{name}?v={int(time.time())}"
    loc = disk_library.locate(p)
    if loc:
        disk_library.update_meta(loc[1], p, {"poster_url": poster, "cover_source": "import manuel"})
    log_action("OK", "JAQUETTE", f"{p} (import manuel)")
    server_url = f"/api/ebooks/cover?path={quote(p, safe='')}&v={int(time.time())}"
    return {"ok": True, "poster_url": poster if loc else server_url}


# ---------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------

def _adult_fragments() -> list[str]:
    """Textes qui, dans le journal, désignent un film adulte : nom, titre, dossier."""
    found: set[str] = set()

    def add(text) -> None:
        text = str(text or "").strip()
        stem = os.path.splitext(text)[0]
        for candidate in (text, stem):
            if len(candidate) >= 6:
                found.add(candidate)

    marks = adult_media.marked_paths()  # une lecture pour les deux boucles
    for it in scan_cache.cached_items("films"):
        if adult_media.is_adult_item(it, marks):
            add(it.get("name"))
            add(os.path.basename(it.get("path") or ""))
    for entry in onefichier.list_entries():
        if adult_media.is_adult_item(entry, marks):
            add(entry.get("title"))
            add(os.path.basename(entry.get("original_path") or ""))
    for root in adult_media.adult_roots():
        add(root)
    if adult_media.adult_subdir():
        add(adult_media.adult_subdir())
    return sorted(found)


@app.get("/api/logs")
def get_logs(limit: int = Query(200, ge=1, le=1000), unlocked: bool = Depends(adult_unlocked)):
    entries = read_log(limit)
    if not unlocked:
        # Les lignes écrites depuis la version 42 sont déjà masquées à l'écriture. Pour les plus
        # anciennes, on retire celles qui citent un dossier adulte, une marque ou un nom connu.
        fragments = _adult_fragments()
        entries = [e for e in entries
                   if not adult_media.mentions_adult(e["detail"])
                   and not any(f in e["detail"] for f in fragments)]
    return {"entries": entries}


# ---------------------------------------------------------------------
# Frontend statique (monté en dernier pour laisser /api et /ws prioritaires)
# ---------------------------------------------------------------------

FRONTEND_DIR = os.environ.get("FRONTEND_DIR", "/app/frontend")
if os.path.isdir(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
