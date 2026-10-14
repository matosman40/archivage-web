"""Radarr et Sonarr : retirer un film ou une série de l'application (version 49).

Radarr gère les films, Sonarr les séries. Quand on supprime un élément du serveur ou d'un disque
avec l'option « retirer de Radarr / Sonarr », sa fiche est retirée de l'application : elle n'est
plus cherchée, ni téléchargée. Les fichiers ne sont pas touchés par Radarr/Sonarr
(deleteFiles=false) et la fiche ne revient pas par une liste d'import (addImportListExclusion=true).

Le retrait se fait en deux temps, pour qu'une erreur ne laisse rien à moitié fait :
  1. preparer() : interroge l'application et dit quelle fiche correspond à chaque élément.
     Aucune modification. Une erreur de connexion, une clé refusée ou une correspondance
     ambiguë arrête tout ici : la suppression n'a alors pas lieu.
  2. retirer() : supprime les fiches trouvées.

Correspondance : par identifiant TMDB quand l'application le fournit, sinon par titre normalisé
et année. Plusieurs fiches possibles = refus (on ne devine jamais).
"""
import os
import re
import unicodedata
from typing import Optional

import httpx

from . import config, identities
from .naming import guess_title_year

APPS = {
    "radarr": {"label": "Radarr", "media": "films", "path": "movie"},
    "sonarr": {"label": "Sonarr", "media": "series", "path": "series"},
}
TIMEOUT = 20.0


class ArrError(Exception):
    """Erreur présentable à l'utilisateur, sans clé API."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def _cfg(kind: str) -> tuple[str, str]:
    return (getattr(config.settings, f"{kind}_url", "") or "", getattr(config.settings, f"{kind}_api_key", "") or "")


def configured(kind: str) -> bool:
    url, key = _cfg(kind)
    return bool(url and key)


def kind_for_media(media_type: str) -> Optional[str]:
    for kind, app in APPS.items():
        if app["media"] == media_type:
            return kind
    return None


def status() -> dict:
    """Présence de la configuration (sans aucun secret) et action choisie (version 50)."""
    action = getattr(config.settings, "arr_action", "retirer") or "retirer"
    return {kind: {"label": app["label"], "configured": configured(kind), "action": action}
            for kind, app in APPS.items()}


async def _request(kind: str, method: str, path: str, params: Optional[dict] = None,
                   body: Optional[dict] = None) -> httpx.Response:
    label = APPS[kind]["label"]
    url, key = _cfg(kind)
    if not url or not key:
        raise ArrError(f"{label} n'est pas configuré (Réglages › Radarr et Sonarr).", 400)
    if not url.startswith(("http://", "https://")):
        raise ArrError(f"L'adresse de {label} doit commencer par http:// ou https://.", 400)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            return await client.request(method, f"{url}/api/v3/{path}", params=params, json=body,
                                        headers={"X-Api-Key": key})
    except httpx.HTTPError:
        # Le message httpx peut contenir l'adresse : on n'en renvoie qu'un texte générique.
        raise ArrError(f"{label} est injoignable. Vérifie son adresse dans Réglages.")


def _check(kind: str, resp: httpx.Response) -> None:
    label = APPS[kind]["label"]
    if resp.status_code in (401, 403):
        raise ArrError(f"La clé API de {label} est refusée. Vérifie-la dans Réglages.")
    if resp.status_code >= 400:
        raise ArrError(f"{label} a répondu {resp.status_code}.")


async def test_connection(kind: str) -> dict:
    resp = await _request(kind, "GET", "system/status")
    _check(kind, resp)
    data = resp.json()
    return {"ok": True, "app": data.get("appName") or APPS[kind]["label"], "version": data.get("version") or "?"}


def _norm(text) -> str:
    texte = unicodedata.normalize("NFKD", str(text or ""))
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", texte.lower())


def identite(media_type: str, item: str, meta: Optional[dict] = None) -> dict:
    """Titre, année et identifiant TMDB d'un élément : identification faite dans l'application,
    sinon fiche de la bibliothèque disque, sinon le nom du fichier ou du dossier."""
    ident = identities.get(media_type, item) or {}
    meta = meta or {}
    title, year = ident.get("title") or meta.get("title"), ident.get("year") or meta.get("year")
    if not title:
        title, guessed = guess_title_year(os.path.basename(item.rstrip("/")))
        year = year or guessed
    tmdb = ident.get("tmdb_id") or meta.get("tmdb_id")
    return {"title": title, "year": int(year) if year else None, "tmdb_id": int(tmdb) if tmdb else None}


def _correspondances(entries: list, ident: dict) -> list:
    if ident["tmdb_id"] and any(e.get("tmdbId") for e in entries):
        return [e for e in entries if e.get("tmdbId") == ident["tmdb_id"]]
    titre = _norm(ident["title"])
    if not titre:
        return []
    candidats = [e for e in entries if _norm(e.get("title")) == titre]
    # Sans année connue, le titre seul ne suffit pas : « Dune » (1984) ne doit pas retirer « Dune » (2021).
    if candidats and not ident["year"]:
        raise ArrError(
            f"Année inconnue pour « {ident['title']} » : impossible de savoir quelle fiche retirer. "
            f"Rien n'a été supprimé. Identifie l'élément (bouton « Identifier ») pour fixer l'année.",
            409,
        )
    hits = []
    for e in candidats:
        annee = e.get("year")
        if annee and int(annee) != ident["year"]:
            continue
        hits.append(e)
    return hits


async def preparer(media_type: str, items: list[str], metas: Optional[dict] = None) -> list[dict]:
    """Fiches à retirer, sans rien modifier. Liste vide si l'application n'est pas concernée
    (pas de Radarr/Sonarr configuré, ou type sans application : ebooks)."""
    kind = kind_for_media(media_type)
    if not kind or not configured(kind):
        return []
    resp = await _request(kind, "GET", APPS[kind]["path"])
    _check(kind, resp)
    entries = resp.json()
    plan = []
    for item in items:
        ident = identite(media_type, item, (metas or {}).get(item))
        hits = _correspondances(entries, ident)
        label = APPS[kind]["label"]
        if len(hits) > 1:
            raise ArrError(
                f"Plusieurs fiches de {label} correspondent à « {ident['title']} ». Supprime-la dans {label} "
                f"ou identifie l'élément (bouton « Identifier »). Rien n'a été supprimé."
            )
        plan.append({
            "item": item,
            "kind": kind,
            "app": label,
            "id": hits[0]["id"] if hits else None,
            "title": (hits[0].get("title") if hits else ident["title"]) or os.path.basename(item),
            "status": "a_retirer" if hits else "absent",
        })
    return plan


async def _desactiver(kind: str, entry: dict) -> str:
    """Désactive la fiche (ne plus la chercher) sans la retirer de l'application (version 50)."""
    chemin = f"{APPS[kind]['path']}/{entry['id']}"
    resp = await _request(kind, "GET", chemin)
    if resp.status_code == 404:
        return "absent"
    _check(kind, resp)
    fiche = resp.json()
    if fiche.get("monitored") is False:
        return "désactivé"  # déjà désactivée : rien à faire
    fiche["monitored"] = False
    resp = await _request(kind, "PUT", chemin, body=fiche)
    if resp.status_code == 404:
        return "absent"
    _check(kind, resp)
    return "désactivé"


async def retirer(plan: list[dict]) -> list[dict]:
    """Retire (ou désactive, selon le réglage arr_action) les fiches du plan. Une erreur arrête la suite."""
    results = []
    desactiver = (getattr(config.settings, "arr_action", "retirer") or "retirer") == "desactiver"
    for entry in plan:
        if entry["status"] != "a_retirer":
            results.append({**entry, "result": "absent"})
            continue
        kind = entry["kind"]
        if desactiver:
            try:
                resultat = await _desactiver(kind, entry)
            except ArrError as exc:
                done = [r["title"] for r in results if r["result"] in ("retiré", "désactivé")]
                suite = f" Déjà traité(s) : {', '.join(done)}." if done else ""
                raise ArrError(f"{exc}{suite} Les fichiers n'ont pas été touchés.")
            results.append({**entry, "result": resultat})
            continue
        resp = await _request(kind, "DELETE", f"{APPS[kind]['path']}/{entry['id']}",
                              params={"deleteFiles": "false", "addImportListExclusion": "true"})
        if resp.status_code == 404:
            results.append({**entry, "result": "absent"})
            continue
        try:
            _check(kind, resp)
        except ArrError as exc:
            done = [r["title"] for r in results if r["result"] == "retiré"]
            suite = f" Déjà retiré(s) : {', '.join(done)}." if done else ""
            raise ArrError(f"{exc}{suite} La suppression des fichiers n'a pas été lancée.")
        results.append({**entry, "result": "retiré"})
    return results
