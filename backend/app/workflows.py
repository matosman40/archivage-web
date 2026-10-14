"""Automatisations : des règles « quand … alors … » exécutées sans intervention.

Une règle a :
- un DÉCLENCHEUR : « plex_vu » (un film vient d'être marqué vu dans Plex) ou « nouveau_film »
  (un film apparaît dans les dossiers sources) ;
- des FILTRES facultatifs : note minimale (note Plex) et genre ;
- un MOMENT : « immediat » (dès la détection) ou « nuit » (à l'heure choisie, 01:00 par défaut) ;
- des ACTIONS exécutées dans l'ordre : copier sur un disque, déplacer sur un disque, envoyer sur 1fichier,
  envoyer une notification.

Garanties :
- Les films adultes sont toujours exclus.
- À la création, les films déjà vus ou présents ne sont pas traités (sauf « inclure les films déjà vus »).
- Chaque couple (action, film) n'est fait qu'une fois : relancer une règle ne refait pas une copie existante.
- Un élément qui échoue est retenté au plus ESSAIS_MAX fois, puis abandonné et signalé dans l'historique.
- Les actions qui retirent un fichier de sa source (déplacer, libérer le serveur) passent toujours en
  dernier, et une seule par règle. Elles exigent ALLOW_DELETE et une confirmation explicite.
- En simulation (DRY_RUN), rien n'est mémorisé : la règle se rejoue pour de vrai ensuite.

Les transferts restent soumis aux plages de transfert (Réglages) : « la nuit » fixe le moment où
la tâche est créée, la copie elle-même suit les plages activées.
"""
import asyncio
import json
import os
import re
import time
import uuid
from datetime import datetime
from typing import Awaitable, Callable, Optional

from . import config, notify, security
from .logs import log_action

DECLENCHEURS = {
    "plex_vu": "Un film est marqué « vu » dans Plex",
    "nouveau_film": "Un nouveau film apparaît dans mes dossiers sources",
}
ACTIONS = {
    "copier_disque": "Copier sur un disque",
    "deplacer_disque": "Déplacer sur un disque (retire le fichier de sa source)",
    "envoyer_1fichier": "Envoyer sur 1fichier",
    "notifier": "Envoyer une notification",
}
MOMENTS = {
    "immediat": "Dès la détection",
    "nuit": "La nuit, à l'heure choisie",
}
ESSAIS_MAX = 3
INTERVALLE_DETECTION = 1800.0   # secondes entre deux relevés des films
PAS_BOUCLE = 60.0               # secondes entre deux passages de la boucle
HISTORIQUE_MAX = 60
_HEURE_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")

# Fonctions fournies par le serveur (main.py) : listing des films, création et attente des tâches.
_fonctions: dict = {"lister": None, "lancer": None, "attendre": None}
_donnees: Optional[dict] = None
_verrous: dict[str, asyncio.Lock] = {}
_dernier_passage: Optional[float] = None  # None : aucun relevé encore fait
_maintenant: Callable[[], datetime] = datetime.now


def configurer(*, lister: Callable[[], Awaitable[list]],
               lancer: Callable[[dict, list], Awaitable[object]],
               attendre: Callable[[object], Awaitable[int]]) -> None:
    """lister() -> films ; lancer(action, chemins) -> tâche ; attendre(tâche) -> nombre d'erreurs."""
    _fonctions.update(lister=lister, lancer=lancer, attendre=attendre)


# ---------------------------------------------------------------------
# Stockage
# ---------------------------------------------------------------------

def _fichier() -> str:
    return os.path.join(os.path.dirname(config.SETTINGS_FILE) or ".", "workflows.json")


def recharger() -> None:
    """Relit le fichier au prochain accès (utile après un changement de chemin, en test)."""
    global _donnees
    _donnees = None


def _lire() -> dict:
    global _donnees
    if _donnees is None:
        vide = {"regles": [], "historique": []}
        try:
            with open(_fichier(), "r", encoding="utf-8") as fh:
                contenu = json.load(fh)
            _donnees = {"regles": list(contenu.get("regles", [])),
                        "historique": list(contenu.get("historique", []))}
        except FileNotFoundError:
            _donnees = vide
        except (OSError, ValueError) as exc:
            # Fichier illisible : il est mis de côté (jamais écrasé sans copie) et on repart de zéro.
            try:
                os.replace(_fichier(), _fichier() + ".corrompu")
            except OSError:
                pass
            log_action("ECHEC", "AUTOMATISATION",
                       f"fichier des automatisations illisible ({type(exc).__name__}) : mis de côté")
            _donnees = vide
    return _donnees


def _ecrire() -> None:
    chemin = _fichier()
    os.makedirs(os.path.dirname(chemin) or ".", exist_ok=True)
    tmp = chemin + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(_lire(), fh, ensure_ascii=False, indent=1)
    os.replace(tmp, chemin)


def _etat_vide() -> dict:
    return {"connus": None,          # None = première lecture pas encore faite (rien n'est déclenché)
            "file_attente": [],      # chemins détectés et pas encore traités entièrement
            "faits": {},             # clé d'action -> chemins déjà traités
            "essais": {},            # « clé|chemin » -> nombre d'échecs
            "derniere_nuit": None,   # date (AAAA-MM-JJ) du dernier passage « nuit »
            "derniere_execution": None,
            "dernier_resultat": None}


def trouver(regle_id: str) -> Optional[dict]:
    for regle in _lire()["regles"]:
        if regle["id"] == regle_id:
            return regle
    return None


def _iso() -> str:
    return _maintenant().isoformat(timespec="minutes")


# ---------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------

def capacites() -> dict:
    """Ce que ce serveur peut faire : l'interface n'affiche que les actions possibles."""
    return {
        "allow_delete": bool(config.ALLOW_DELETE),
        "onefichier": bool(config.settings.onefichier_enabled) and bool(config.settings.tmdb_enabled),
        "notify": bool((config.settings.notify_url or "").strip()),
    }


def _retire_source(action: dict) -> bool:
    return action["type"] == "deplacer_disque" or (
        action["type"] == "envoyer_1fichier" and action.get("apres") == "liberer")


def _normaliser_action(action: dict) -> dict:
    kind = action.get("type")
    out = {"type": kind}
    if kind in ("copier_disque", "deplacer_disque"):
        brut = (action.get("destination") or "").strip()
        out["destination"] = os.path.normpath(brut) if brut else ""
    if kind == "envoyer_1fichier":
        out["apres"] = action.get("apres") or "garder"
        if out["apres"] == "liberer":
            out["retirer_arr"] = bool(action.get("retirer_arr"))  # version 49 : choix explicite, décoché par défaut
    if _retire_source(out):
        out["confirme"] = bool(action.get("confirme"))
    return out


def _normaliser(entree: dict) -> dict:
    return {
        "nom": (entree.get("nom") or "").strip(),
        "actif": bool(entree.get("actif", True)),
        "declencheur": entree.get("declencheur"),
        "note_min": entree.get("note_min"),
        "genre": (entree.get("genre") or "").strip(),
        "inclure_deja_vus": bool(entree.get("inclure_deja_vus")),
        "quand": entree.get("quand") or "immediat",
        "heure": entree.get("heure") or "01:00",
        "actions": [_normaliser_action(a) for a in entree.get("actions") or []],
    }


def verifier(regle: dict) -> None:
    """Lève ValueError (message en français) si la règle ne peut pas fonctionner."""
    if not regle["nom"]:
        raise ValueError("Donne un nom à l'automatisation.")
    if regle["declencheur"] not in DECLENCHEURS:
        raise ValueError("Déclencheur inconnu.")
    if regle["quand"] not in MOMENTS:
        raise ValueError("Moment inconnu.")
    if regle["quand"] == "nuit" and not _HEURE_RE.fullmatch(regle["heure"] or ""):
        raise ValueError("L'heure doit être au format HH:MM.")
    if regle["note_min"] is not None and not (0 <= float(regle["note_min"]) <= 10):
        raise ValueError("La note minimale doit être entre 0 et 10.")
    if not regle["actions"]:
        raise ValueError("Ajoute au moins une action.")
    if len(regle["actions"]) > 6:
        raise ValueError("Pas plus de 6 actions par automatisation.")

    for action in regle["actions"]:
        kind = action["type"]
        if kind not in ACTIONS:
            raise ValueError("Action inconnue.")
        if kind in ("copier_disque", "deplacer_disque"):
            dest = action.get("destination") or ""
            if not dest.startswith("/") or dest == "/":
                raise ValueError("Choisis le disque de destination.")
            if security.is_under_source_dirs(dest):
                raise ValueError("La destination chevauche un dossier source : refusé par sécurité.")
        if kind == "envoyer_1fichier":
            if not config.settings.onefichier_enabled:
                raise ValueError("1fichier n'est pas configuré (Réglages › 1fichier).")
            if not config.settings.tmdb_enabled:
                raise ValueError("TMDB doit être activé : il sert à nommer les dossiers 1fichier.")
        if kind == "notifier" and not (config.settings.notify_url or "").strip():
            raise ValueError("Aucune URL de notification configurée (Réglages).")
        if _retire_source(action):
            if not config.ALLOW_DELETE:
                raise ValueError("Cette action retire le fichier de sa source : elle est désactivée "
                                 "(ALLOW_DELETE=false). Seules les copies sont possibles.")
            if not action.get("confirme"):
                raise ValueError("Coche la confirmation : cette action retire le fichier de sa source.")

    retirent = [i for i, a in enumerate(regle["actions"]) if _retire_source(a)]
    if len(retirent) > 1:
        raise ValueError("Une seule action peut retirer le fichier de sa source.")
    if retirent and retirent[0] != len(regle["actions"]) - 1:
        raise ValueError("L'action qui retire le fichier de sa source doit être la dernière.")


# ---------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------

def _marquer_nuit_passee(regle: dict) -> None:
    """Une règle « nuit » créée après son heure attend la nuit suivante (pas de passage immédiat)."""
    if regle["quand"] != "nuit":
        return
    maintenant = _maintenant()
    h, m = (int(x) for x in regle["heure"].split(":"))
    if maintenant.hour * 60 + maintenant.minute >= h * 60 + m:
        regle["etat"]["derniere_nuit"] = maintenant.date().isoformat()


def creer(entree: dict) -> dict:
    regle = _normaliser(entree)
    verifier(regle)
    regle["id"] = uuid.uuid4().hex[:8]
    regle["cree_le"] = _iso()
    regle["etat"] = _etat_vide()
    regle["etat"]["connus"] = [] if regle["inclure_deja_vus"] else None
    _marquer_nuit_passee(regle)
    _lire()["regles"].append(regle)
    _ecrire()
    return regle


def modifier(regle_id: str, entree: dict) -> dict:
    regle = trouver(regle_id)
    if regle is None:
        raise KeyError(regle_id)
    nouvelle = _normaliser(entree)
    verifier(nouvelle)
    etat = regle["etat"]
    if (nouvelle["declencheur"] != regle["declencheur"]
            or nouvelle["inclure_deja_vus"] != regle["inclure_deja_vus"]):
        etat["connus"] = [] if nouvelle["inclure_deja_vus"] else None  # nouvelle première lecture
    cree_le = regle["cree_le"]
    regle.update(nouvelle)
    regle["id"] = regle_id
    regle["cree_le"] = cree_le
    regle["etat"] = etat
    _marquer_nuit_passee(regle)
    _ecrire()
    return regle


def supprimer(regle_id: str) -> None:
    donnees = _lire()
    avant = len(donnees["regles"])
    donnees["regles"] = [r for r in donnees["regles"] if r["id"] != regle_id]
    if len(donnees["regles"]) == avant:
        raise KeyError(regle_id)
    _verrous.pop(regle_id, None)
    _ecrire()


def publique(regle: dict) -> dict:
    etat = regle["etat"]
    out = {k: v for k, v in regle.items() if k != "etat"}
    out["etat"] = {
        "en_attente": len(etat["file_attente"]),
        "fichiers_en_attente": [os.path.basename(p) for p in etat["file_attente"][:30]],
        "premiere_lecture_faite": etat["connus"] is not None,
        "derniere_execution": etat["derniere_execution"],
        "dernier_resultat": etat["dernier_resultat"],
    }
    return out


def liste_publique() -> dict:
    donnees = _lire()
    return {
        "regles": [publique(r) for r in donnees["regles"]],
        "historique": donnees["historique"][:HISTORIQUE_MAX],
        "catalogue": {"declencheurs": DECLENCHEURS, "actions": ACTIONS, "moments": MOMENTS},
        "capacites": capacites(),
    }


# ---------------------------------------------------------------------
# Détection
# ---------------------------------------------------------------------

def _condition(declencheur: str, film: dict) -> bool:
    if declencheur == "plex_vu":
        return film.get("watched") is True
    return True  # nouveau_film : tout film présent dans les dossiers sources


def _filtres_ok(regle: dict, film: dict) -> bool:
    note_min = regle.get("note_min")
    if note_min is not None:
        note = film.get("rating")
        if note is None or float(note) < float(note_min):
            return False  # sans note connue, le film n'est pas retenu
    genre = (regle.get("genre") or "").strip().lower()
    if genre and genre not in [g.lower() for g in (film.get("genres") or [])]:
        return False
    return True


def _detecter(regle: dict, films: list) -> int:
    """Ajoute à la file d'attente les films nouvellement concernés. Renvoie leur nombre."""
    etat = regle["etat"]
    cible = {f["path"]: f for f in films if not f.get("adult") and _condition(regle["declencheur"], f)}
    if etat["connus"] is None:
        # Première lecture : on mémorise l'existant sans rien déclencher.
        etat["connus"] = sorted(cible)
        return 0
    deja = set(etat["connus"])
    nouveaux = sorted(p for p, f in cible.items() if p not in deja and _filtres_ok(regle, f))
    etat["connus"] = sorted(cible)
    en_file = set(etat["file_attente"])
    for chemin in nouveaux:
        if chemin not in en_file:
            etat["file_attente"].append(chemin)
    return len(nouveaux)


# ---------------------------------------------------------------------
# Exécution
# ---------------------------------------------------------------------

def _cle(action: dict) -> str:
    if action["type"] == "envoyer_1fichier":
        return f"envoyer_1fichier:{action.get('apres', 'garder')}"
    if action["type"] in ("copier_disque", "deplacer_disque"):
        return f"{action['type']}:{action.get('destination', '')}"
    return action["type"]


def _message(exc: BaseException) -> str:
    return str(getattr(exc, "detail", None) or exc)


async def _executer_action(regle: dict, action: dict, chemins: list[str]) -> tuple[bool, str]:
    if action["type"] == "notifier":
        noms = ", ".join(os.path.basename(p) for p in chemins[:10])
        await notify.send(f"Automatisation : {regle['nom']}", f"{len(chemins)} film(s) : {noms}")
        return True, "notification envoyée (si une URL est configurée)"
    try:
        tache = await _fonctions["lancer"](action, chemins)
    except Exception as exc:  # validation refusée (espace, destination, configuration…)
        return False, _message(exc)
    erreurs = await _fonctions["attendre"](tache)
    if erreurs:
        return False, f"{erreurs} erreur(s) sur {len(chemins)} élément(s)"
    return True, f"{len(chemins)} élément(s)"


def _historiser(regle: dict, action: dict, nombre: int, ok: bool, detail: str) -> None:
    libelle = ACTIONS.get(action["type"], action["type"])
    historique = _lire()["historique"]
    historique.insert(0, {"at": _iso(), "regle": regle["nom"], "action": libelle,
                          "elements": nombre, "ok": ok, "detail": detail})
    del historique[HISTORIQUE_MAX:]
    log_action("OK" if ok else "ECHEC", "AUTOMATISATION", f"{regle['nom']} · {libelle} : {detail}")


async def _executer(regle: dict) -> None:
    """Exécute les actions dans l'ordre pour les films en attente. Appelée sous verrou."""
    etat = regle["etat"]
    simulation = config.settings.dry_run
    for action in regle["actions"]:
        if not etat["file_attente"]:
            break
        cle = _cle(action)
        faits = set(etat["faits"].get(cle, []))
        a_faire = [p for p in etat["file_attente"]
                   if p not in faits and etat["essais"].get(f"{cle}|{p}", 0) < ESSAIS_MAX]
        if not a_faire:
            continue
        ok, detail = await _executer_action(regle, action, a_faire)
        if simulation:
            _historiser(regle, action, len(a_faire), ok, f"simulation · {detail}")
            continue
        if ok:
            etat["faits"].setdefault(cle, []).extend(a_faire)
        else:
            for p in a_faire:
                etat["essais"][f"{cle}|{p}"] = etat["essais"].get(f"{cle}|{p}", 0) + 1
        _historiser(regle, action, len(a_faire), ok, detail)
        _ecrire()

    if not simulation:
        # Un film sort de la file quand chaque action est faite ou abandonnée.
        cles = [_cle(a) for a in regle["actions"]]
        faits = {c: set(etat["faits"].get(c, [])) for c in cles}
        etat["file_attente"] = [
            p for p in etat["file_attente"]
            if any(p not in faits[c] and etat["essais"].get(f"{c}|{p}", 0) < ESSAIS_MAX for c in cles)
        ]
    etat["derniere_execution"] = _iso()
    echecs = [k for k, v in etat["essais"].items() if v >= ESSAIS_MAX]
    etat["dernier_resultat"] = (f"{len(echecs)} élément(s) abandonné(s) après {ESSAIS_MAX} essais"
                                if echecs else "ok")
    _ecrire()


def _verrou(regle_id: str) -> asyncio.Lock:
    return _verrous.setdefault(regle_id, asyncio.Lock())


# Exécutions en cours. Une copie peut attendre des heures sa plage de transfert : elle tourne donc
# en tâche de fond, jamais dans la boucle de surveillance, et une seule à la fois par règle.
_executions: set[str] = set()
_taches: set = set()


def _lancer_execution(regle: dict) -> None:
    rid = regle["id"]
    if rid in _executions:
        return  # une exécution tourne déjà : les nouveaux films seront pris au passage suivant
    _executions.add(rid)
    task = asyncio.ensure_future(_executer_regle(rid))
    _taches.add(task)
    task.add_done_callback(_taches.discard)


async def _executer_regle(rid: str) -> None:
    regle = trouver(rid)
    if regle is None:
        _executions.discard(rid)
        return
    try:
        if regle["etat"]["file_attente"]:
            await _executer(regle)
            _ecrire()
    except Exception as exc:
        log_action("ECHEC", "AUTOMATISATION", f"{regle['nom']} : {_message(exc)}")
    finally:
        _executions.discard(rid)


async def attendre_taches() -> None:
    """Attend la fin des exécutions lancées dans la boucle courante (utile aux tests)."""
    boucle_courante = asyncio.get_running_loop()
    attente = [t for t in _taches if not t.done() and t.get_loop() is boucle_courante]
    if attente:
        await asyncio.gather(*attente, return_exceptions=True)


async def traiter(regle: dict, films: list, *, forcer: bool) -> None:
    """Détection, puis exécution si la règle est immédiate (ou si forcer). L'exécution tourne en fond."""
    async with _verrou(regle["id"]):
        if trouver(regle["id"]) is None:
            return  # supprimée entre-temps
        _detecter(regle, films)
        _ecrire()
    if (forcer or regle["quand"] == "immediat") and regle["etat"]["file_attente"]:
        _lancer_execution(regle)


async def executer_nuit(regle: dict) -> None:
    """Passage « nuit » : une fois par jour, à l'heure de la règle, sur les films en attente."""
    async with _verrou(regle["id"]):
        regle["etat"]["derniere_nuit"] = _maintenant().date().isoformat()
        _ecrire()
    if regle["etat"]["file_attente"]:
        _lancer_execution(regle)


async def lancer_maintenant(regle_id: str) -> None:
    """« Lancer maintenant » : relève les films puis exécute, sans attendre la nuit."""
    regle = trouver(regle_id)
    if regle is None:
        return
    try:
        films = await _fonctions["lister"]()
        await traiter(regle, films, forcer=True)
    except Exception as exc:
        log_action("ECHEC", "AUTOMATISATION", f"{regle['nom']} : {_message(exc)}")


async def premier_releve(regle_id: str) -> None:
    """Relevé juste après la création : mémorise l'existant (ou traite tout si demandé)."""
    regle = trouver(regle_id)
    if regle is None:
        return
    try:
        films = await _fonctions["lister"]()
        await traiter(regle, films, forcer=False)
    except Exception as exc:
        log_action("ECHEC", "AUTOMATISATION", f"{regle['nom']} : relevé impossible ({_message(exc)})")


def _heure_atteinte(regle: dict, maintenant: datetime) -> bool:
    h, m = (int(x) for x in regle["heure"].split(":"))
    return (maintenant.hour * 60 + maintenant.minute >= h * 60 + m
            and regle["etat"].get("derniere_nuit") != maintenant.date().isoformat())


async def _tick() -> None:
    global _dernier_passage
    actives = [r for r in _lire()["regles"] if r["actif"]]
    if not actives:
        return
    maintenant = _maintenant()
    releve_du = _dernier_passage is None or time.monotonic() - _dernier_passage >= INTERVALLE_DETECTION
    films = None
    if releve_du:
        _dernier_passage = time.monotonic()
        try:
            films = await _fonctions["lister"]()
        except Exception as exc:  # Plex injoignable, disque absent… : on réessaiera au prochain relevé
            log_action("ECHEC", "AUTOMATISATION", f"relevé des films impossible ({_message(exc)})")
    for regle in actives:
        try:
            if films is not None:
                await traiter(regle, films, forcer=False)
            if regle["quand"] == "nuit" and _heure_atteinte(regle, maintenant):
                await executer_nuit(regle)
        except Exception as exc:
            log_action("ECHEC", "AUTOMATISATION", f"{regle['nom']} : {_message(exc)}")


async def boucle() -> None:
    """Boucle de fond : démarrée au lancement du serveur."""
    while True:
        await asyncio.sleep(PAS_BOUCLE)
        try:
            await _tick()
        except Exception as exc:
            log_action("ECHEC", "AUTOMATISATION", f"cycle interrompu : {_message(exc)}")
