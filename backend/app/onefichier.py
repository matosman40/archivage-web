"""Sauvegarde des films, séries et ebooks sur 1fichier, via l'API d'un compte premium.

Trois types d'éléments, une archive par élément :
- FILM : un fichier ou un dossier de film. Identifiant TMDB (film), dossier
  ``tmdb-<id>/`` sur 1fichier ;
- SÉRIE : un dossier de série (la « pack » de la médiathèque). Identifiant TMDB
  (série), dossier ``tmdb-tv-<id>/`` ; une seule archive pour toute la série ;
- EBOOK : un dossier qui contient des livres. Pas d'identifiant TMDB : le
  dossier 1fichier est ``ebook-<empreinte>/``, et la jaquette est celle du
  premier livre du dossier (EPUB, CBZ ou PDF).

Pour chaque élément :
1. le dossier 1fichier ne contient que l'identifiant (les titres n'apparaissent
   pas sur 1fichier) ;
2. le dossier est mis dans une archive 7z chiffrée en AES-256, avec en-têtes
   chiffrés : sans le mot de passe, ni le nom ni le contenu ne sont lisibles ;
3. un mot de passe aléatoire est tiré POUR CHAQUE ÉLÉMENT et enregistré dans un
   index local (ONEFICHIER_INDEX_FILE, par défaut 1fichier-index.json à côté de
   settings.json, donc sur le volume /data). SANS CET INDEX, LES ARCHIVES NE
   S'OUVRENT PLUS : il faut sauvegarder le volume /data ;
4. l'archive est envoyée dans son dossier, sa taille est vérifiée côté
   1fichier, puis le lien est noté dans l'index. Les fichiers locaux ne sont
   jamais supprimés par cette opération.

Restauration : le lien est retéléchargé, l'archive est déchiffrée (7z vérifie
le CRC de chaque fichier) et son contenu est replacé à son emplacement
d'origine, ou dans le dossier choisi. Le fichier reste sur 1fichier.

Les chemins et noms de champs JSON suivent l'implémentation de référence de
rclone (backend « fichier ») ; à valider sur un premier envoi réel.
"""
import asyncio
import hashlib
import io
import json
import os
import posixpath
import re
import secrets
import shutil
import string
import subprocess
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from typing import Optional
from urllib.parse import unquote

import httpx

from . import arr, config, disk_library, disks, ebooks as ebooks_mod, identities, tmdb
from . import adult_media, notify, scheduler, urlguard
from .logs import log_action
from .series import scan_pack, season_number

# Hôtes autorisés à recevoir le jeton 1fichier. Lu aussi par hosts/onefichier.py.
ALLOWED_DOMAINS = ("1fichier.com",)

API_BASE = "https://api.1fichier.com/v1"
MAX_FILE_BYTES = 300 * 10**9          # plafond d'un fichier côté 1fichier
PASSWORD_CHARS = string.ascii_letters + string.digits
SAFETY_SLACK_BYTES = 64 * 1024 * 1024  # marge fixe pour les fichiers temporaires
_SECRET_FIELDS = {"password", "url"}   # jamais renvoyés à l'interface

KIND_FILM = "film"
KIND_SERIE = "serie"
KIND_EBOOK = "ebook"
KIND_NAMES = {KIND_FILM: "film", KIND_SERIE: "série", KIND_EBOOK: "ebook"}
# Radarr (films) et Sonarr (séries) : fiche retirée quand la copie locale est libérée (version 49).
ARR_MEDIA_BY_KIND = {KIND_FILM: "films", KIND_SERIE: "series"}
# Clés d'index : identifiant TMDB brut (films), « tv:<id> » (série entière),
# « tv:<id>:s<n> » (une saison d'une série), « book:<empreinte> » (ebooks)
KEY_RE = re.compile(r"^(\d+|tv:\d+|tv:\d+:s\d{1,2}|book:[0-9a-f]{12})$")
SEASON_KEY_RE = re.compile(r"^tv:(\d+):s(\d{1,2})$")
COVER_RE = re.compile(r"^book-[0-9a-f]{12}\.(jpg|png|gif|webp)$")
COVER_MAX_BYTES = 30 * 1024 * 1024    # une jaquette plus grosse est ignorée
COVER_WIDTH = 400                      # largeur des jaquettes enregistrées
COVER_TRY_LIMIT = 10                   # nombre de livres examinés pour trouver une jaquette
_IMAGE_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}

_lock = threading.RLock()
# Clés des éléments dont l'envoi est en cours dans CE processus. Permet de
# refuser de retirer une entrée pendant qu'un envoi la remplit.
_ACTIVE: set[str] = set()
# Avancement des envois en cours, en mémoire seulement (jamais écrit dans l'index) :
# étape, pourcentage, octets envoyés, vitesse. Affiché dans la bibliothèque 1fichier.
_PROGRESS: dict[str, dict] = {}


def _progress(key: str, **fields) -> None:
    current = _PROGRESS.setdefault(key, {"started_at": time.time()})
    current.update(fields)
    current["updated_at"] = time.time()


class OneFichierError(Exception):
    """Erreur présentable telle quelle à l'utilisateur (ne contient aucun secret).
    `status` (optionnel) indique le code HTTP à renvoyer à l'interface."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class ArchiveCorrompue(OneFichierError):
    """Archive présente chez 1fichier mais abîmée : taille, empreinte SHA-256 ou CRC 7z fausse
    (version 50). Sous-classe d'OneFichierError : les appels existants la traitent pareil."""


def folder_name(tmdb_id) -> str:
    return f"tmdb-{tmdb_id}"


# ---------------------------------------------------------------------
# Index local (mots de passe, liens, identifiants de dossiers)
# ---------------------------------------------------------------------

def index_path() -> str:
    override = os.environ.get("ONEFICHIER_INDEX_FILE")
    if override:
        return override
    return os.path.join(os.path.dirname(config.SETTINGS_FILE) or ".", "1fichier-index.json")


def _read() -> dict:
    path = index_path()
    if not os.path.exists(path):
        return {"version": 1, "folders": {}, "entries": {}}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        # Ne JAMAIS repartir d'un index vide ici : il serait réécrit et les
        # mots de passe des archives déjà envoyées seraient perdus.
        raise OneFichierError(
            f"index 1fichier illisible ({path}) : {exc}. Rien n'a été modifié ; "
            f"restaure le fichier depuis une sauvegarde avant de continuer."
        )
    if not isinstance(data, dict):
        raise OneFichierError(f"index 1fichier invalide ({path}). Rien n'a été modifié.")
    data.setdefault("folders", {})
    data.setdefault("entries", {})
    return data


INDEX_BACKUP_EVERY = 60      # au plus une copie par minute : pas de copie à chaque petite écriture
INDEX_BACKUP_KEEP = 100      # nombre de copies conservées (les plus anciennes sont supprimées)
EXPORT_MIN_PASSPHRASE = 12   # phrase de passe de l'export chiffré


def index_backup_dir() -> str:
    return index_path() + ".backups"


def _backup_index_if_due(path: str) -> None:
    """Garde une copie de l'index tel qu'il était AVANT l'écriture en cours, au plus
    une fois par minute. Une copie qui échoue ne bloque jamais l'écriture de l'index."""
    if not os.path.exists(path):
        return
    bdir = index_backup_dir()
    try:
        os.makedirs(bdir, mode=0o700, exist_ok=True)
        copies = sorted(f for f in os.listdir(bdir) if f.endswith(".json"))
        if copies and time.time() - os.path.getmtime(os.path.join(bdir, copies[-1])) < INDEX_BACKUP_EVERY:
            return
        name = time.strftime("%Y%m%d-%H%M%S") + ".json"
        target = os.path.join(bdir, name)
        if os.path.exists(target):
            return
        shutil.copy2(path, target)  # copie les droits 0600 de l'index
        copies.append(name)
        for old in copies[:-INDEX_BACKUP_KEEP]:
            os.remove(os.path.join(bdir, old))
    except OSError as exc:
        log_action("ALERTE", "1FICHIER", f"copie de sauvegarde de l'index impossible : {exc}")


def index_status() -> dict:
    """Ce que l'interface affiche : nombre de copies locales, dernière copie, dernier export."""
    bdir = index_backup_dir()
    copies = sorted(f for f in os.listdir(bdir) if f.endswith(".json")) if os.path.isdir(bdir) else []
    last_backup = None
    if copies:
        last_backup = os.path.getmtime(os.path.join(bdir, copies[-1]))
    return {
        "backups": len(copies),
        "last_backup_at": last_backup,
        "last_export_at": _snapshot().get("last_export_at"),
    }


def _write(data: dict) -> None:
    path = index_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    _backup_index_if_due(path)
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # lisible par le seul propriétaire
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    with _SNAP_LOCK:  # le cache de lecture (voir _snapshot) est périmé dès maintenant
        _SNAP_WRITES["n"] += 1


# Cache de lecture de l'index. Relire et parser un index de plusieurs milliers d'entrées à chaque
# interrogation coûtait cher (la page interroge toutes les 2 à 3 s pendant un envoi). Le cache est
# valable tant que le fichier garde le même inode, la même date et la même taille, et que
# l'application n'a rien écrit depuis (compteur _SNAP_WRITES, incrémenté par _write).
_SNAP: dict = {"key": None, "data": None}
_SNAP_WRITES = {"n": 0}
_SNAP_LOCK = threading.Lock()


def _public_view(data: dict) -> dict:
    """Index sans mot de passe ni lien : ce que la lecture partagée peut garder en mémoire."""
    entries = {
        key: ({f: v for f, v in e.items() if f not in _SECRET_FIELDS} if isinstance(e, dict) else e)
        for key, e in data["entries"].items()
    }
    return {**data, "entries": entries}


def _snapshot() -> dict:
    """Lecture partagée de l'index, SANS mot de passe ni lien. Relue depuis le disque seulement si le
    fichier a changé ou si l'application vient d'écrire l'index. Le résultat est partagé entre les
    lectures : ne jamais le modifier. Une mise à jour passe par _update, qui relit toujours le fichier.
    Les écritures sont atomiques (fichier temporaire puis os.replace) : un lecteur voit un index complet."""
    path = index_path()
    try:
        st = os.stat(path)
        file_key = (path, st.st_ino, st.st_mtime_ns, st.st_size)
    except OSError:
        file_key = None  # index absent : rien à garder, _read répond un index vide
    with _SNAP_LOCK:
        writes = _SNAP_WRITES["n"]  # lu AVANT la lecture : une écriture pendant la lecture force une relecture
        if file_key is not None and _SNAP["key"] == (file_key, writes):
            return _SNAP["data"]
    data = _public_view(_read())
    if file_key is not None:
        with _SNAP_LOCK:
            _SNAP["key"] = (file_key, writes)
            _SNAP["data"] = data
    return data


def _update(fn):
    """Lecture-modification-écriture atomique de l'index. Si `fn` lève une
    exception, rien n'est écrit."""
    with _lock:
        data = _read()
        result = fn(data)
        _write(data)
        return result


def get_entry(key: str) -> Optional[dict]:
    """Entrée complète de l'index (y compris liens et mots de passe) : à n'utiliser que côté serveur,
    pour contrôler un droit d'accès. Ne jamais la renvoyer telle quelle à l'interface."""
    return _get_entry(key)


def _get_entry(key: str) -> Optional[dict]:
    with _lock:
        entry = _read()["entries"].get(key)
    if not entry:
        return None
    return {**entry, "key": key, "kind": entry_kind(entry)}


def etat_fichier(entry: dict) -> Optional[str]:
    """État d'un envoi terminé (version 50) : present, absent (lien introuvable chez 1fichier),
    corrompu (dernier test de restauration en échec sur l'archive), ou non_verifie."""
    if entry.get("status") != "done":
        return None
    if entry.get("link_ok") is False:
        return "absent"
    if entry.get("corrompu") or entry.get("taille_ok") is False:
        return "corrompu"
    if entry.get("link_ok") is True:
        return "present"
    return "non_verifie"


def list_entries() -> list[dict]:
    """Entrées de l'index, sans mot de passe ni lien (ce que l'interface affiche)."""
    data = _snapshot()
    public = []
    for key, entry in data["entries"].items():
        item = {k: v for k, v in entry.items() if k not in _SECRET_FIELDS}
        item["key"] = key
        item["kind"] = entry_kind(entry)
        item["etat"] = etat_fichier(entry)
        item["active"] = key in _ACTIVE
        if item["active"]:
            item["progress"] = dict(_PROGRESS.get(key, {}))
        public.append(item)
    public.sort(key=lambda e: e.get("archived_at") or e.get("created_at") or 0, reverse=True)
    return public


def export_index(passphrase: str) -> tuple[str, str]:
    """Archive 7z chiffrée (AES-256) contenant l'index : seule copie hors serveur des mots
    de passe. La phrase de passe n'est jamais enregistrée. Renvoie (chemin, nom du fichier)."""
    if len(passphrase or "") < EXPORT_MIN_PASSPHRASE:
        raise OneFichierError(f"phrase de passe trop courte : {EXPORT_MIN_PASSPHRASE} caractères minimum.", 400)
    src = index_path()
    if not os.path.exists(src):
        raise OneFichierError("aucun index à exporter (aucun envoi enregistré).", 404)
    staging = config.settings.onefichier_staging_dir or None
    if staging:
        os.makedirs(staging, exist_ok=True)
    work = tempfile.mkdtemp(prefix="index-export-", dir=staging)
    name = f"1fichier-index-{time.strftime('%Y%m%d-%H%M%S')}.7z"
    archive = os.path.join(work, name)
    try:
        # 7z lit l'index à son emplacement : aucune copie en clair n'est créée.
        _7z_create(os.path.dirname(os.path.abspath(src)), os.path.basename(src), archive, passphrase, 5)
        _7z_test(archive, passphrase)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    _update(lambda d: d.__setitem__("last_export_at", time.time()))
    log_action("OK", "1FICHIER", "index exporté (archive 7z chiffrée)")
    return archive, name


def forget_pending(key: str) -> None:
    """Retire de l'index un envoi resté incomplet. Un envoi terminé n'est
    jamais retirable ici : son mot de passe est la seule clé de son archive."""
    if key in _ACTIVE:
        raise OneFichierError("envoi en cours : attends la fin de l'opération avant de le retirer.", 409)

    def controle(entry):
        if not entry:
            raise OneFichierError("entrée introuvable", 404)
        if entry.get("status") != "pending":
            raise OneFichierError("seul un envoi incomplet peut être retiré de l'index", 409)
        return entry_kind(entry)

    if config.settings.dry_run:
        # Simulation : mêmes contrôles, mais l'index réel (et son mot de passe) n'est pas modifié.
        controle(_snapshot()["entries"].get(key))
        return

    def fn(d):
        kind = controle(d["entries"].get(key))
        del d["entries"][key]
        return kind
    kind = _update(fn)
    if kind == KIND_EBOOK:
        _remove_cover(key)


# ---------------------------------------------------------------------
# Chemins : seuls les dossiers films configurés sont concernés
# ---------------------------------------------------------------------

def _film_dirs() -> list[str]:
    # Les films adultes sont des films : même classement, mêmes contrôles de chemin.
    return [os.path.normpath(d) for d in config.settings.source_dirs_films + config.settings.source_dirs_adultes]


def is_inside_film_dirs(path: str) -> bool:
    """Strictement à l'intérieur d'un dossier films : la racine d'une
    bibliothèque entière ne peut pas être envoyée d'un coup."""
    p = os.path.normpath(path)
    return any(p.startswith(src + os.sep) for src in _film_dirs())


def under_film_dirs(path: str) -> bool:
    """Égal à un dossier films, ou à l'intérieur (destination de restauration)."""
    p = os.path.normpath(path)
    return any(p == src or p.startswith(src + os.sep) for src in _film_dirs())


def _series_dirs() -> list[str]:
    return [os.path.normpath(d) for d in config.settings.source_dirs_series]


def _ebook_dirs() -> list[str]:
    return [os.path.normpath(d) for d in config.settings.source_dirs_ebooks]


def _roots(kind: str) -> list[str]:
    return {KIND_FILM: _film_dirs, KIND_SERIE: _series_dirs, KIND_EBOOK: _ebook_dirs}[kind]()


def under_any_dirs(path: str) -> bool:
    """Destination de restauration : dans un dossier source (films, séries ou ebooks)."""
    p = os.path.normpath(path)
    roots = _film_dirs() + _series_dirs() + _ebook_dirs()
    return any(p == src or p.startswith(src + os.sep) for src in roots)


def under_kind_dirs(kind: str, path: str) -> bool:
    p = os.path.normpath(path)
    return any(p == src or p.startswith(src + os.sep) for src in _roots(kind))


def entry_kind(entry: dict) -> str:
    """Les entrées créées avant la version 6 ne portent pas de type : ce sont des films."""
    return entry.get("kind") or KIND_FILM


def valid_key(key: str) -> bool:
    return bool(KEY_RE.match(key or ""))


def folder_for(key: str) -> str:
    """Nom du dossier sur 1fichier, dérivé de la clé de l'index. Une saison est
    rangée dans le dossier de sa série : elle partage ainsi le dossier de la série."""
    m = SEASON_KEY_RE.match(key)
    if m:
        return f"tmdb-tv-{m.group(1)}"
    if key.startswith("tv:"):
        return f"tmdb-tv-{key[3:]}"
    if key.startswith("book:"):
        return f"ebook-{key[5:]}"
    return f"tmdb-{key}"


def season_key(tmdb_id, season: int) -> str:
    return f"tv:{int(tmdb_id)}:s{int(season)}"


def folder_key_for(key: str) -> str:
    """Clé du dossier 1fichier : celle de la série pour une saison, sinon la clé elle-même."""
    m = SEASON_KEY_RE.match(key)
    return f"tv:{m.group(1)}" if m else key


ADULT_ROOT_KEY = "adult-root"      # dossier racine des films adultes sur 1fichier
ADULT_FOLDER_NAME = "ADULTES"


def adult_folder_key(key: str) -> str:
    """Clé d'index d'un dossier de film adulte : séparée de celle du dossier ordinaire."""
    return f"adult:{key}"


def archive_name_for(key: str, folder: str) -> str:
    m = SEASON_KEY_RE.match(key)
    if m:
        return f"{folder}-S{int(m.group(2)):02d}.7z"
    return f"{folder}.7z"


def season_show(path: str) -> Optional[str]:
    """Si `path` est le dossier d'une saison, renvoie le dossier de la série qui la
    contient (serveur ou disque détecté). Sinon None."""
    p = os.path.normpath(path)
    if not os.path.isdir(p) or os.path.islink(p):
        return None
    show = os.path.dirname(p)
    if os.path.dirname(show) in _series_dirs():
        return show
    loc = disk_library.locate_season(p)
    return loc[1] if loc else None


def unit_label(path: str) -> str:
    """Nom affiché pour un élément envoyé : « Série · Saison 1 » pour une saison."""
    show = season_show(path)
    base = os.path.basename(os.path.normpath(path))
    return f"{os.path.basename(show)} · {base}" if show else base


def archived_tmdb_id(show: str) -> Optional[int]:
    """Identifiant TMDB d'une série déjà envoyée depuis ce dossier (sinon None)."""
    show = os.path.normpath(show)
    for e in list_entries():
        if e.get("kind") == KIND_SERIE and e.get("tmdb_id") is not None:
            origin = e.get("original_path") or ""
            if origin == show or origin.startswith(show + os.sep):
                return int(e["tmdb_id"])
    return None


def seasons_sent(tmdb_id) -> dict:
    """Saisons déjà envoyées sur 1fichier pour une série : {n° de saison: clé}.
    Une série envoyée EN ENTIER compte toutes ses saisons : la clé est alors « tv:<id> »."""
    if tmdb_id is None:
        return {}
    out = {}
    whole = f"tv:{int(tmdb_id)}"
    for entry in list_entries():
        if entry.get("status") != "done":
            continue
        if entry["key"] == whole:
            return {"*": whole}
        m = SEASON_KEY_RE.match(entry["key"])
        if m and int(m.group(1)) == int(tmdb_id):
            out[int(m.group(2))] = entry["key"]
    return out


def ebook_key(unit: str) -> str:
    """Empreinte stable d'un dossier d'ebooks, calculée depuis son chemin relatif
    à la source : deux dossiers de même nom ne peuvent pas se confondre."""
    root = _strictly_inside(unit, _ebook_dirs())
    rel = os.path.relpath(unit, root) if root else unit
    return "book:" + hashlib.sha256(rel.encode("utf-8")).hexdigest()[:12]


def _strictly_inside(path: str, roots: list[str]) -> Optional[str]:
    return next((src for src in roots if path.startswith(src + os.sep)), None)


def classify(path: str) -> tuple[str, str]:
    """Renvoie (type, unité d'envoi) pour un élément sélectionné.

    - film : un fichier ou dossier strictement à l'intérieur d'un dossier films ;
    - série : un dossier directement dans un dossier séries, ou une saison (sous-dossier
      d'une série) ;
    - ebook : le dossier qui contient le livre (si le livre est un fichier) ;
      jamais la racine d'une source, qui regrouperait toute la bibliothèque.
    Lève OneFichierError (403 hors des dossiers configurés, 400 sinon)."""
    p = os.path.normpath(path)
    if is_inside_film_dirs(p):
        return KIND_FILM, p
    if os.path.isdir(p) and os.path.dirname(p) in _series_dirs():
        return KIND_SERIE, p
    if season_show(p):
        return KIND_SERIE, p  # une saison d'une série (serveur ou disque)
    if p in _ebook_dirs():
        raise OneFichierError(
            "racine d'une source ebooks : sélectionne le dossier qui contient les livres.", 400)
    root = _strictly_inside(p, _ebook_dirs())
    if root:
        unit = p if os.path.isdir(p) else os.path.dirname(p)
        if not unit.startswith(root + os.sep):  # un livre posé directement à la racine
            raise OneFichierError(
                "un ebook doit être dans un dossier : sélectionne le dossier qui contient "
                "les livres.", 400)
        return KIND_EBOOK, unit
    # Élément d'un disque de sauvegarde CONNECTÉ (enfant direct d'un sous-dossier MEDIA).
    loc = disk_library.locate(p)
    if loc:
        kind = {"films": KIND_FILM, "series": KIND_SERIE, "ebooks": KIND_EBOOK}[loc[0]]
        if kind == KIND_FILM:
            return kind, p
        if not os.path.isdir(p):
            raise OneFichierError(
                "une série ou un ebook doit être un dossier : sélectionne le dossier, pas un fichier isolé.", 400)
        return kind, p
    raise OneFichierError(
        "élément hors des dossiers films, séries et ebooks configurés : refusé.", 403)


def ebook_files(unit: str) -> list[str]:
    """Livres d'un dossier (sous-dossiers compris), dans l'ordre alphabétique :
    le premier sert de jaquette."""
    found = []
    for dirpath, dirs, files in os.walk(unit):
        dirs.sort()
        for f in files:
            if f.lower().endswith(config.settings.ebook_extensions_tuple):
                found.append(os.path.join(dirpath, f))
    return sorted(found, key=lambda p: os.path.relpath(p, unit).lower())


# ---------------------------------------------------------------------
# API 1fichier
# ---------------------------------------------------------------------

def is_configured() -> bool:
    return config.settings.onefichier_enabled


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {config.settings.onefichier_api_key}"}


def _check_host(url: str) -> None:
    """Le jeton API n'est envoyé qu'à des serveurs 1fichier.com : une URL
    renvoyée par une réponse ne doit jamais pouvoir le détourner."""
    try:
        urlguard.check_host(url, ALLOWED_DOMAINS)
    except urlguard.UrlRejected as exc:
        raise OneFichierError(f"adresse inattendue renvoyée par 1fichier : {exc.host or 'vide'}")


def _node_base(host: str) -> str:
    base = host if host.startswith("http") else f"https://{host}"
    _check_host(base)
    return base.rstrip("/")


async def _call(client: httpx.AsyncClient, method: str, path: str, payload: Optional[dict] = None) -> dict:
    url = f"{API_BASE}{path}"
    # 1fichier refuse toute requête API sans Content-Type JSON (« Content-Type not JSON #24 »),
    # GET compris : l'en-tête est donc explicite sur chaque appel.
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    try:
        if method == "GET":
            resp = await client.get(url, headers=headers, timeout=30)
        else:
            resp = await client.post(url, json=payload or {}, headers=headers, timeout=30)
    except httpx.HTTPError as exc:
        raise OneFichierError(f"connexion à 1fichier impossible ({type(exc).__name__})")
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if isinstance(data, list):  # listes renvoyées telles quelles (fichiers d'un dossier)
        data = {"status": "OK", "items": data}
    if not isinstance(data, dict):
        data = {}
    status = str(data.get("status", "OK")).upper()
    if resp.status_code >= 400 or status != "OK":
        message = data.get("message") or f"HTTP {resp.status_code}"
        raise OneFichierError(f"1fichier ({path}) : {message}")
    return data


async def test_connection() -> dict:
    if not is_configured():
        raise OneFichierError("clé API 1fichier non renseignée (Réglages).")
    async with httpx.AsyncClient() as client:
        data = await _call(client, "POST", "/user/info.cgi")
    return {k: v for k, v in data.items() if k != "status"}


async def _ensure_folder(client: httpx.AsyncClient, key: str, name: str, parent_key: Optional[str] = None) -> int:
    known = _snapshot()["folders"].get(key)
    if known is not None:
        return int(known)

    parent_id = 0  # 0 : racine de 1fichier
    if parent_key is not None:
        parent_id = await _ensure_folder(client, parent_key, ADULT_FOLDER_NAME)
    try:
        data = await _call(client, "POST", "/folder/mkdir.cgi", {"name": name, "folder_id": parent_id})
    except OneFichierError as exc:
        raise OneFichierError(
            f"création du dossier {name} : {exc}. S'il existe déjà sur 1fichier sans être "
            f"dans l'index local, supprime-le ou renomme-le sur 1fichier."
        )
    folder_id = data.get("folder_id")
    if folder_id is None:
        raise OneFichierError("1fichier n'a pas renvoyé l'identifiant du dossier")
    await asyncio.to_thread(_update, lambda d: d["folders"].__setitem__(key, int(folder_id)))
    return int(folder_id)


async def _upload_server(client: httpx.AsyncClient) -> tuple[str, str]:
    data = await _call(client, "GET", "/upload/get_upload_server.cgi")
    upload_id, host = data.get("id"), data.get("url")
    if not upload_id or not host:
        raise OneFichierError("1fichier n'a pas fourni de serveur d'envoi")
    return _node_base(str(host)), str(upload_id)


class _CountingReader:
    """Enveloppe le fichier pour suivre la progression de l'envoi, et pour limiter
    son débit (vitesse de la plage active). fileno() est exposé pour que httpx
    connaisse la taille exacte (sinon il enverrait en chunked, que 1fichier
    pourrait refuser)."""

    def __init__(self, fh, throttle: Optional[scheduler.Throttle] = None):
        self._fh = fh
        self._throttle = throttle
        self.done = 0

    def read(self, size=-1):
        chunk = self._fh.read(size)
        self.done += len(chunk)
        if self._throttle is not None:
            self._throttle.wait_for(len(chunk))
        return chunk

    def seek(self, offset, whence=0):
        pos = self._fh.seek(offset, whence)
        self.done = pos
        return pos

    def tell(self):
        return self._fh.tell()

    def fileno(self):
        return self._fh.fileno()


def _upload_blocking(node: str, upload_id: str, folder_id: int, path: str, holder: dict) -> None:
    timeout = httpx.Timeout(30.0, read=600.0, write=600.0)
    with open(path, "rb") as fh, httpx.Client(timeout=timeout) as client:
        reader = _CountingReader(fh, scheduler.Throttle("onefichier"))
        holder["reader"] = reader
        resp = client.post(
            f"{node}/upload.cgi",
            params={"id": upload_id},
            data={"did": str(folder_id)},
            files={"file[]": (os.path.basename(path), reader, "application/octet-stream")},
            headers=_auth_headers(),
        )
    if resp.status_code >= 400:
        raise OneFichierError(f"envoi refusé par 1fichier (HTTP {resp.status_code})")


async def _end_upload(node: str, upload_id: str) -> dict:
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.get(
                f"{node}/end.pl", params={"xid": upload_id}, headers={"JSON": "1", **_auth_headers()},
            )
        except httpx.HTTPError as exc:
            raise OneFichierError(f"fin d'envoi impossible ({type(exc).__name__})")
    try:
        data = resp.json()
    except ValueError:
        raise OneFichierError("réponse de fin d'envoi illisible")
    links = data.get("links") if isinstance(data, dict) else None
    if not links:
        raise OneFichierError("envoi terminé mais 1fichier n'a renvoyé aucun lien")
    return links[0]


async def _download_url(client: httpx.AsyncClient, url: str) -> str:
    data = await _call(client, "POST", "/download/get_token.cgi", {"url": url})
    link = data.get("url")
    if not link:
        raise OneFichierError("1fichier n'a pas renvoyé de lien de téléchargement")
    _check_host(str(link))
    return str(link)


def _download_blocking(url: str, dest: str, holder: dict) -> None:
    # Pas d'en-tête d'authentification : le lien de téléchargement est déjà signé.
    throttle = scheduler.Throttle("onefichier")
    with httpx.Client(timeout=httpx.Timeout(30.0, read=600.0), follow_redirects=True) as client:
        with client.stream("GET", url) as resp:
            if resp.status_code >= 400:
                raise OneFichierError(f"téléchargement refusé par 1fichier (HTTP {resp.status_code})")
            with open(dest, "wb") as fh:
                for chunk in resp.iter_bytes(1024 * 1024):
                    fh.write(chunk)
                    holder["done"] = holder.get("done", 0) + len(chunk)
                    throttle.wait_for(len(chunk))


# ---------------------------------------------------------------------
# 7z (chiffrement, test, extraction)
# ---------------------------------------------------------------------

def _low_priority_prefix() -> list[str]:
    """Préfixe `ionice -c3 nice -n 19` (classe « inactive » disque, priorité CPU
    minimale) si le système le permet ; sinon rien. Testé une seule fois."""
    global _PRIO_PREFIX
    if _PRIO_PREFIX is None:
        prefix: list[str] = []
        candidates = [["ionice", "-c3"], ["nice", "-n", "19"]]
        for cmd in candidates:
            if shutil.which(cmd[0]):
                try:
                    if subprocess.run([*cmd, "true"], capture_output=True, timeout=5).returncode == 0:
                        prefix += cmd
                except (OSError, subprocess.SubprocessError):
                    pass
        _PRIO_PREFIX = prefix
    return _PRIO_PREFIX


_PRIO_PREFIX: Optional[list[str]] = None


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_7z(args: list[str], password: str, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([*_low_priority_prefix(), "7z", *args], cwd=cwd, capture_output=True, text=True, errors="replace")
    except FileNotFoundError:
        raise OneFichierError("7z est absent du conteneur : reconstruis l'image (paquet p7zip-full).")


def _7z_error(proc: subprocess.CompletedProcess, password: str) -> str:
    text = ((proc.stderr or "") + (proc.stdout or "")).replace(password, "***").strip()
    return text[-300:] or f"code de sortie {proc.returncode}"


def _7z_create(parent: str, base: str, archive: str, password: str, level: int) -> None:
    # -mhe=on : chiffre aussi les en-têtes, donc la liste des fichiers.
    args = ["a", "-t7z", "-y", "-bd", "-mhe=on", f"-mx={level}", f"-p{password}", archive, base]
    proc = _run_7z(args, password, cwd=parent)
    if proc.returncode != 0:
        raise OneFichierError(f"compression 7z échouée : {_7z_error(proc, password)}")


def _7z_test(archive: str, password: str) -> None:
    proc = _run_7z(["t", "-y", "-bd", f"-p{password}", archive], password)
    if proc.returncode != 0:
        raise ArchiveCorrompue(f"vérification de l'archive échouée : {_7z_error(proc, password)}")


def _7z_extract(archive: str, password: str, out_dir: str) -> None:
    proc = _run_7z(["x", "-y", "-bd", f"-p{password}", f"-o{out_dir}", archive], password)
    if proc.returncode != 0:
        hint = " (mot de passe incorrect : l'index a-t-il été modifié ?)" if "password" in (proc.stderr or "").lower() else ""
        raise OneFichierError(f"extraction échouée{hint} : {_7z_error(proc, password)}")


# ---------------------------------------------------------------------
# Outils fichiers
# ---------------------------------------------------------------------

PASSWORD_LENGTH = 16  # ~95 bits d'entropie : solide, et tapable à la main si besoin


def _new_password() -> str:
    return "".join(secrets.choice(PASSWORD_CHARS) for _ in range(PASSWORD_LENGTH))


def _tree_size(path: str) -> int:
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _ensure_space(path: str, needed: int) -> None:
    # Le dossier doit EXISTER : il est monté dans le conteneur (docker-compose.yml).
    # Le créer ici écrirait l'archive dans le système du conteneur, sur le disque
    # de l'hôte, si le montage manque.
    if not os.path.isdir(path):
        raise OneFichierError(
            f"dossier de travail introuvable : {path}. Il doit être monté dans le conteneur "
            f"(docker-compose.yml) puis indiqué dans Réglages › 1fichier › Dossier de travail.")
    try:
        free = shutil.disk_usage(path).free
    except OSError as exc:
        raise OneFichierError(f"dossier de travail inaccessible ({path}) : {exc}")
    usable = free - config.settings.safety_margin_bytes
    if usable < needed:
        raise OneFichierError(
            f"espace insuffisant dans le dossier de travail {path} : {needed // (1024 * 1024)} Mo "
            f"nécessaires, {max(0, usable) // (1024 * 1024)} Mo utilisables. Libère de la place sur ce "
            f"disque ou choisis un autre dossier dans Réglages › 1fichier › Dossier de travail.")


async def _with_progress(job, name: str, total: int, read_done, blocking, key: Optional[str] = None) -> None:
    """Exécute `blocking` dans un thread et publie sa progression (lue via
    `read_done`) une fois par seconde. Avec `key`, l'avancement est aussi
    gardé pour la bibliothèque 1fichier (octets, vitesse)."""
    task = asyncio.ensure_future(asyncio.to_thread(blocking))
    started = time.time()
    job.emit({"type": "item_progress", "item": name, "percent": 0})
    last = 0
    while True:
        done, _pending = await asyncio.wait({task}, timeout=1.0)
        if task in done:
            break
        sent = read_done()
        pct = min(99, int(sent * 100 / total)) if total else 0
        if key:
            elapsed = max(time.time() - started, 0.001)
            _progress(key, stage="Envoi vers 1fichier", percent=pct, bytes_done=int(sent),
                      bytes_total=int(total), speed=int(sent / elapsed))
        if pct != last:
            last = pct
            job.emit({"type": "item_progress", "item": name, "percent": pct})
    task.result()  # relève l'exception éventuelle


META_RETRY_SECONDS = 3600   # délai avant de retenter une fiche TMDB introuvable
META_CONCURRENCY = 4


def _has_tmdb(entry: dict) -> bool:
    return entry_kind(entry) in (KIND_FILM, KIND_SERIE) and entry.get("tmdb_id") is not None


def _needs_meta(entry: dict) -> bool:
    if not _has_tmdb(entry):
        return False
    meta = entry.get("meta")
    if not meta:
        return True
    return bool(meta.get("unavailable")) and time.time() - meta.get("checked_at", 0) > META_RETRY_SECONDS


def _store_meta(key: str, meta: dict) -> None:
    def fn(d):
        entry = d["entries"].get(key)
        if entry is not None:
            entry["meta"] = meta
    _update(fn)


async def refresh_meta(keys=None, force: bool = False) -> int:
    """Récupère la fiche TMDB des entrées qui n'en ont pas (ou toutes si
    `force`). Best-effort : une panne TMDB ne bloque jamais la bibliothèque.
    Les ebooks n'ont pas de fiche TMDB : leur fiche est créée à l'envoi."""
    if not config.settings.tmdb_enabled:
        return 0
    entries = _snapshot()["entries"]
    targets = [k for k, e in entries.items()
               if (keys is None or k in keys) and _has_tmdb(e) and (force or _needs_meta(e))]
    semaphore = asyncio.Semaphore(META_CONCURRENCY)

    async def one(key: str) -> None:
        entry = entries[key]
        fetch = tmdb.tv_details if entry_kind(entry) == KIND_SERIE else tmdb.movie_details
        async with semaphore:
            try:
                details = await fetch(entry["tmdb_id"])
            except Exception:
                return
            now = time.time()
            meta = {**details, "checked_at": now} if details else {"unavailable": True, "checked_at": now}
            await asyncio.to_thread(_store_meta, key, meta)

    await asyncio.gather(*(one(k) for k in targets))
    return len(targets)


_BG_TASKS: set = set()
_META_STATE = {"running": False}


def _meta_pending() -> int:
    entries = _snapshot()["entries"]
    return sum(1 for e in entries.values() if _has_tmdb(e) and _needs_meta(e))


def _start_meta_task(force: bool) -> bool:
    """Lance la récupération des fiches TMDB en arrière-plan (une seule à la fois)."""
    if not config.settings.tmdb_enabled or _META_STATE["running"]:
        return False
    _META_STATE["running"] = True

    async def run() -> None:
        try:
            await refresh_meta(force=force)
        except Exception:
            pass
        finally:
            _META_STATE["running"] = False

    task = asyncio.ensure_future(run())
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return True


def start_meta_refresh_all() -> bool:
    return _start_meta_task(force=True)


async def library() -> dict:
    """Bibliothèque affichée. Ne fait AUCUN appel réseau : les fiches manquantes
    sont récupérées en arrière-plan, l'interface relit la liste en attendant."""
    pending = await asyncio.to_thread(_meta_pending)
    if pending:
        _start_meta_task(force=False)
    entries = await asyncio.to_thread(list_entries)
    return {"entries": entries, "meta_running": _META_STATE["running"], "meta_pending": pending}


def _fail(job, name: str, source: str, reason: str) -> None:
    log_action("ECHEC", "1FICHIER", f"{source} ({reason})")
    job.emit({"type": "item_error", "item": name, "error": reason})


# ---------------------------------------------------------------------
# Jaquettes et fiches des ebooks (sans TMDB)
# ---------------------------------------------------------------------

def covers_dir() -> str:
    return os.path.join(os.path.dirname(index_path()) or ".", "1fichier-covers")


def cover_path(name: str) -> Optional[str]:
    if not COVER_RE.match(name or ""):
        return None
    path = os.path.join(covers_dir(), name)
    return path if os.path.isfile(path) else None


def _cover_prefix(key: str) -> str:
    return key.replace(":", "-")


def _remove_cover(key: str) -> None:
    directory = covers_dir()
    if not os.path.isdir(directory):
        return
    for f in os.listdir(directory):
        if f.startswith(_cover_prefix(key) + "."):
            try:
                os.remove(os.path.join(directory, f))
            except OSError:
                pass


def find_cover(key: str) -> Optional[str]:
    """Nom du fichier de jaquette enregistré pour cette clé, ou None."""
    directory = covers_dir()
    if not os.path.isdir(directory):
        return None
    prefix = _cover_prefix(key) + "."
    for f in sorted(os.listdir(directory)):
        if f.startswith(prefix) and COVER_RE.match(f):
            return f
    return None


def store_cover_bytes(key: str, data: bytes) -> str:
    """Enregistre une image (jaquette importée ou trouvée en ligne) sous la clé,
    ramenée à COVER_WIDTH px en JPEG. Lève ValueError si ce n'est pas une image."""
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:
        raise ValueError("ce fichier n'est pas une image lisible") from exc
    shrunk, ext = _shrink(data, img.format.lower() if img.format else "")
    return _write_cover(key, ext, shrunk)


def cover_from_books(unit: str, key: str, files: list[str]) -> Optional[tuple[str, str]]:
    """Cherche la jaquette dans les premiers livres (EPUB, CBZ, PDF) et l'enregistre.
    Renvoie (nom du fichier, livre d'origine relatif au dossier), ou None."""
    for book in files[:COVER_TRY_LIMIT]:
        got = extract_cover(book)
        if got:
            data, ext = got
            return _write_cover(key, ext, data), os.path.relpath(book, unit)
    return None


def _write_cover(key: str, ext: str, data: bytes) -> str:
    _remove_cover(key)
    os.makedirs(covers_dir(), exist_ok=True)
    name = f"{_cover_prefix(key)}.{ext}"
    path = os.path.join(covers_dir(), name)
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    return name


def _natural(text: str) -> list:
    """Tri « naturel » : page2 avant page10."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", text.lower())]


def _epub_cover(path: str) -> Optional[tuple[bytes, str]]:
    with zipfile.ZipFile(path) as z:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        opf_path = next((el.get("full-path") for el in container.iter()
                         if el.tag.split("}")[-1] == "rootfile" and el.get("full-path")), None)
        if not opf_path:
            return None
        base = posixpath.dirname(opf_path)
        opf = ET.fromstring(z.read(opf_path))
        manifest: dict[str, tuple[str, str, str]] = {}
        cover_id = None
        for el in opf.iter():
            tag = el.tag.split("}")[-1]
            if tag == "item" and el.get("id"):
                manifest[el.get("id")] = (el.get("href") or "", el.get("media-type") or "", el.get("properties") or "")
            elif tag == "meta" and el.get("name") == "cover":
                cover_id = el.get("content")
        candidates = [v for v in manifest.values() if "cover-image" in v[2].split()]
        if cover_id in manifest:
            candidates.insert(0, manifest[cover_id])
        for href, mtype, _props in candidates:
            ext = _IMAGE_EXT.get(mtype)
            name = posixpath.normpath(posixpath.join(base, unquote(href)))
            if not ext or not href:
                continue
            try:
                info = z.getinfo(name)
            except KeyError:
                continue
            if info.file_size > COVER_MAX_BYTES:
                continue
            return z.read(name), ext
    return None


def _cbz_cover(path: str) -> Optional[tuple[bytes, str]]:
    with zipfile.ZipFile(path) as z:
        pages = [i for i in z.infolist()
                 if not i.is_dir() and not i.filename.startswith("__MACOSX")
                 and i.filename.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))
                 and i.file_size <= COVER_MAX_BYTES]
        if not pages:
            return None
        first = min(pages, key=lambda i: _natural(i.filename))
        ext = "jpg" if first.filename.lower().endswith((".jpg", ".jpeg")) else first.filename.lower().rsplit(".", 1)[-1]
        return z.read(first), ext


def _pdf_cover(path: str) -> Optional[tuple[bytes, str]]:
    try:
        import pymupdf  # PyMuPDF : facultatif, sans lui les PDF n'ont pas de jaquette
    except ImportError:
        return None
    doc = pymupdf.open(path)
    try:
        if doc.page_count == 0:
            return None
        pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False)
        return pix.tobytes("png"), "png"
    finally:
        doc.close()


def _shrink(data: bytes, ext: str) -> tuple[bytes, str]:
    """Ramène la jaquette à COVER_WIDTH px en JPEG (Pillow si disponible)."""
    try:
        from PIL import Image
    except ImportError:
        return data, ext
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        if img.width > COVER_WIDTH:
            img = img.resize((COVER_WIDTH, round(img.height * COVER_WIDTH / img.width)))
        img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, "JPEG", quality=85, optimize=True)
        return out.getvalue(), "jpg"
    except Exception:
        return data, ext


def extract_cover(path: str) -> Optional[tuple[bytes, str]]:
    """Jaquette d'un livre (EPUB, CBZ, PDF). None si le format ne s'y prête pas
    ou si le fichier est illisible : on n'échoue jamais pour une jaquette."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".epub":
            raw = _epub_cover(path)
        elif ext == ".cbz":
            raw = _cbz_cover(path)
        elif ext == ".pdf":
            raw = _pdf_cover(path)
        else:
            raw = None
    except Exception:
        raw = None
    return _shrink(*raw) if raw else None


def build_ebook_meta(unit: str, key: str, files: list[str]) -> dict:
    """Fiche d'un dossier d'ebooks : titre et auteur d'après le nom du dossier,
    formats, nombre de livres, et jaquette tirée du premier livre qui en a une."""
    author, title = ebooks_mod.split_author_title(os.path.basename(unit))
    formats = sorted({os.path.splitext(f)[1].lstrip(".").upper() for f in files})
    found = cover_from_books(unit, key, files)
    cover_name, cover_source = found if found else (None, None)
    return {
        "title": title, "original_title": None, "author": author, "year": None,
        "release_date": None, "runtime": None, "genres": [], "rating": None, "votes": None,
        "summary": None, "tagline": None, "backdrop_url": None, "tmdb_url": None,
        "poster_url": f"/api/1fichier/covers/{cover_name}" if cover_name else None,
        "books": len(files), "formats": formats,
        "first_book": os.path.relpath(files[0], unit), "cover_source": cover_source,
        "checked_at": time.time(),
    }


# ---------------------------------------------------------------------
# Envoi (archive)
# ---------------------------------------------------------------------

def _claim(key: str, fields: dict) -> tuple[Optional[dict], Optional[dict]]:
    """Réserve la clé dans l'index AVANT tout envoi, de façon atomique : deux
    envois simultanés du même élément ne peuvent pas passer.
    Renvoie (entrée créée, None) ou (None, entrée existante)."""

    def fn(d):
        existing = d["entries"].get(key)
        if existing:
            return None, existing
        entry = {
            **fields,
            "key": key,
            "status": "pending",
            "password": _new_password(),
            "created_at": time.time(),
            "archived_at": None,
            "restored_at": None,
            "error": None,
            "url": None,
            "remote_size": None,
            "folder_id": None,
            "sha256": None,
            "local_copy": None,
            "local_copy_at": None,
            # Version 46 : l'entrée suit le début de l'envoi, ce qui permet de la reprendre après un arrêt.
            "reprise_suivie": True,
            "archive_size": None,
            "upload_started_at": None,
        }
        d["entries"][key] = entry
        return entry, None

    return _update(fn)


def _release(key: str, password: str) -> None:
    def fn(d):
        entry = d["entries"].get(key)
        if entry and entry.get("status") == "pending" and entry.get("password") == password:
            del d["entries"][key]
    _update(fn)


def _mark_error(key: str, message: str) -> None:
    def fn(d):
        entry = d["entries"].get(key)
        if entry and entry.get("status") == "pending":
            entry["error"] = message
    _update(fn)


def _mark_done(key: str, url: str, remote_size: int) -> None:
    def fn(d):
        entry = d["entries"][key]
        entry.update(status="done", url=url, remote_size=remote_size, size=remote_size,
                     archived_at=time.time(), error=None)
    _update(fn)


COPY_BLOCK_BYTES = 1024 * 1024


def _copy_file_throttled(src: str, dst: str, throttle: scheduler.Throttle) -> None:
    """Copie par blocs, en respectant la vitesse de la plage active pour les copies disque."""
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            block = fi.read(COPY_BLOCK_BYTES)
            if not block:
                break
            fo.write(block)
            throttle.wait_for(len(block))


def _copy_archive_to_disk(archive_path: str, digest: str, name: str) -> str:
    """Copie l'archive chiffrée sur le disque de copie (Réglages). La copie est relue et son
    empreinte comparée à celle de l'envoi : sans concordance, elle est supprimée."""
    root = config.settings.archive_copy_dir
    if not os.path.isdir(root) or not os.access(root, os.W_OK):
        raise OneFichierError(f"dossier de copie introuvable ou non inscriptible : {root} (disque monté ?)")
    dest = os.path.join(root, name)
    if os.path.lexists(dest):
        raise OneFichierError(f"une archive du même nom existe déjà sur le disque de copie : {name}")
    needed = os.path.getsize(archive_path)
    if shutil.disk_usage(root).free < needed + SAFETY_SLACK_BYTES:
        raise OneFichierError(f"place insuffisante sur le disque de copie ({needed} o nécessaires)")
    tmp = dest + ".part"
    try:
        _copy_file_throttled(archive_path, tmp, scheduler.Throttle("disk"))
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
        if _sha256_file(tmp) != digest:
            raise OneFichierError("copie sur disque : empreinte différente de celle de l'envoi (copie supprimée)")
        os.replace(tmp, dest)
    except OSError as exc:
        if os.path.lexists(tmp):
            os.remove(tmp)
        raise OneFichierError(f"copie sur disque impossible : {exc}")
    except OneFichierError:
        if os.path.lexists(tmp):
            os.remove(tmp)
        raise
    return dest


def _set_local_copy(key: str, path: str) -> None:
    _update(lambda d: d["entries"][key].update(local_copy=path, local_copy_at=time.time()))


def _set_sha256(key: str, digest: str) -> None:
    _update(lambda d: d["entries"][key].__setitem__("sha256", digest))


def _set_folder_id(key: str, folder_id: int) -> None:
    _update(lambda d: d["entries"][key].__setitem__("folder_id", folder_id))


def _set_archive_size(key: str, size: int) -> None:
    _update(lambda d: d["entries"][key].__setitem__("archive_size", size))


def _set_upload_started(key: str) -> None:
    """Noté AVANT le premier octet envoyé : au redémarrage, un envoi noté ici peut avoir laissé un fichier."""
    _update(lambda d: d["entries"][key].__setitem__("upload_started_at", time.time()))


def _marquer_retrouvee(key: str, url: str, size: int) -> None:
    def fn(d):
        current = d["entries"].get(key)
        if not current or current.get("status") != "pending":
            raise OneFichierError("entrée modifiée entre-temps : rien n'a été changé.", 409)
        current.update(status="done", url=url, remote_size=size, size=size,
                       archived_at=time.time(), error=None, recovered=True)
    _update(fn)


async def _identify(job, kind: str, unit: str, name: str) -> dict:
    """Détermine la clé, le dossier 1fichier et le titre d'un élément."""
    if kind == KIND_FILM:
        known = identities.tmdb_id_for("films", unit)  # identifié dans l'application
        if known:
            return {"key": str(known), "folder": folder_for(str(known)), "tmdb_id": known,
                    "title": name, "year": None, "meta": None}
        match = await tmdb.find_movie(name, adult=adult_media.is_adult_path(unit))
        if not match:
            raise OneFichierError(
                "identifiant TMDB introuvable pour ce titre : le dossier 1fichier ne peut pas être "
                "nommé. Vérifie le nom du fichier ou la clé TMDB.")
        tmdb_id = int(match["id"])
        return {"key": str(tmdb_id), "folder": folder_for(str(tmdb_id)), "tmdb_id": tmdb_id,
                "title": match.get("title") or name, "year": match.get("year"), "meta": None}
    if kind == KIND_SERIE:
        known = identities.tmdb_id_for("series", unit)
        if known:
            key = f"tv:{known}"
            return {"key": key, "folder": folder_for(key), "tmdb_id": known,
                    "title": name, "year": None, "meta": None}
        match = await tmdb.find_tv(name)
        if not match:
            raise OneFichierError(
                "série introuvable sur TMDB : le dossier 1fichier ne peut pas être nommé. "
                "Vérifie le nom du dossier de la série ou la clé TMDB.")
        tmdb_id = int(match["id"])
        key = f"tv:{tmdb_id}"
        return {"key": key, "folder": folder_for(key), "tmdb_id": tmdb_id,
                "title": match.get("title") or name, "year": match.get("year"), "meta": None}
    files = await asyncio.to_thread(ebook_files, unit)
    if not files:
        raise OneFichierError("aucun ebook dans ce dossier : rien à envoyer.")
    key = ebook_key(unit)
    meta = await asyncio.to_thread(build_ebook_meta, unit, key, files)
    return {"key": key, "folder": folder_for(key), "tmdb_id": None,
            "title": meta["title"] or name, "year": None, "meta": meta}


def _lien_distant(match: dict) -> str:
    url = str(match.get("url") or "")
    if url and not url.startswith("http"):
        url = f"https://1fichier.com/?{url}"
    if not url:
        raise OneFichierError("1fichier n'a pas renvoyé le lien du fichier.")
    _check_host(url)
    return url


async def _reprendre_envoi(job, name: str, unit: str, kind: str, show: Optional[str], key: str, folder: str) -> tuple[bool, Optional[str]]:
    """Tâche reprise après un redémarrage (version 46) : traite l'entrée « en cours » laissée par l'arrêt.
    - Rien n'était parti vers 1fichier (arrêt pendant la compression ou la connexion) : l'entrée est retirée
      et l'envoi repart de zéro, avec un nouveau mot de passe.
    - Le téléversement avait commencé : 1fichier est interrogé. Si le fichier attendu est là, à la taille
      exacte de l'archive, l'envoi a abouti : il est enregistré comme retrouvé, sans nouvel envoi. Sinon, un
      fichier partiel peut subsister chez 1fichier : l'entrée est retirée, l'envoi repart de zéro, et un
      avertissement demande de vérifier le dossier. Si 1fichier ne répond pas, rien ne change.
    Renvoie (traité, avertissement) : traité = True si l'élément est déjà traité ; avertissement = message à garder
    sur la nouvelle entrée quand l'envoi repart de zéro, pour qu'il reste visible dans l'interface."""
    entry = await asyncio.to_thread(_get_entry, key)
    if not entry or entry.get("status") != "pending":
        return False, None
    if not entry.get("reprise_suivie"):
        raise OneFichierError(
            "envoi interrompu avant la version 46 : son état n'est pas suivi. Vérifie 1fichier, puis retire "
            "l'entrée incomplète (Réglages › 1fichier) avant de relancer.")
    if not entry.get("upload_started_at"):
        await asyncio.to_thread(_release, key, entry["password"])
        job.emit({"type": "item_log", "item": name,
                  "line": "Reprise : rien n'avait été envoyé avant l'arrêt, l'envoi repart de zéro."})
        log_action("REPRISE", "1FICHIER", f"{folder} : envoi relancé (rien n'avait été envoyé)")
        return False, None

    archive_size = entry.get("archive_size")
    folder_id = entry.get("folder_id")
    if not archive_size or folder_id is None:
        raise OneFichierError(
            "envoi interrompu sans taille ni dossier enregistrés : vérifie 1fichier avant de relancer.")
    # Si 1fichier ne répond pas, l'exception fait échouer l'élément ; l'entrée reste en attente.
    async with httpx.AsyncClient() as client:
        data = await _call(client, "POST", "/file/ls.cgi", {"folder_id": int(folder_id)})
    listing = data.get("files") or data.get("items") or []
    match = next((f for f in listing if isinstance(f, dict) and f.get("filename") == entry["archive_name"]), None)
    if match and int(match.get("size") or 0) == int(archive_size):
        url = _lien_distant(match)
        await asyncio.to_thread(_marquer_retrouvee, key, url, int(archive_size))
        job.emit({"type": "item_log", "item": name,
                  "line": "Envoi interrompu, mais 1fichier a reçu l'archive complète : enregistrée sans nouvel envoi."})
        log_action("OK", "1FICHIER", f"{folder} : envoi interrompu retrouvé complet (reprise)")
        if _has_tmdb(entry):
            try:
                await refresh_meta({key}, force=True)
            except Exception:
                pass
        await _after_backup(job, name, unit, kind, show)
        return True, None

    await asyncio.to_thread(_release, key, entry["password"])
    message = (f"Envoi interrompu : le fichier {entry['archive_name']} n'est pas complet sur 1fichier. "
               f"L'envoi repart de zéro. Vérifie le dossier {folder} et supprime un éventuel fichier partiel de même nom.")
    job.emit({"type": "item_log", "item": name, "line": message})
    log_action("ALERTE", "1FICHIER", f"{folder} : {message}")
    return False, message


async def _archive_one(job, item: str, name: str) -> None:
    if not os.path.lexists(item):
        if getattr(job, "resumed", False):
            # Tâche reprise : la source a pu être retirée après un envoi réussi, avant l'arrêt.
            job.emit({"type": "item_skipped", "item": name, "reason": "already_done",
                      "message": "Source absente au redémarrage : sans doute déjà traitée avant l'arrêt (voir 1fichier)."})
            return
        raise OneFichierError("source introuvable (déjà déplacée ou supprimée ?)")
    kind, unit = classify(item)
    show = season_show(unit) if kind == KIND_SERIE else None
    season = None
    if show:
        season = season_number(os.path.basename(unit))
        if season is None:
            raise OneFichierError(
                "dossier de saison sans numéro : renomme-le « Season 1 » ou « S01 » pour l'envoyer par saison.")
    name = unit_label(unit)
    # Une notification sort du serveur : pour un film adulte, ni son nom ni son détail d'erreur (version 42).
    adulte = kind == KIND_FILM and adult_media.is_adult_path(unit)
    libelle_sortant = "élément pour adultes" if adulte else name
    size = await asyncio.to_thread(_tree_size, unit)
    if size > MAX_FILE_BYTES:
        raise OneFichierError("trop volumineux pour 1fichier (plafond 300 Go par fichier)")
    if kind in (KIND_FILM, KIND_SERIE) and not config.settings.tmdb_enabled:
        raise OneFichierError("TMDB doit être activé : il sert à nommer le dossier 1fichier.")

    # Une saison est identifiée par sa série (même TMDB, même dossier 1fichier).
    ident = await _identify(job, kind, show or unit, os.path.basename(show or unit))
    if season is not None:
        ident["key"] = season_key(ident["tmdb_id"], season)
        ident["folder"] = folder_for(f"tv:{ident['tmdb_id']}")
    key, folder = ident["key"], ident["folder"]
    year = f" ({ident['year']})" if ident.get("year") else ""
    if kind == KIND_EBOOK:
        line = f"{ident['meta']['books']} livre(s) · jaquette : {ident['meta']['cover_source'] or 'aucune'} → {folder}/"
    else:
        suffix = f" · saison {season}" if season is not None else ""
        line = f"TMDB {ident['tmdb_id']} : {ident['title']}{year}{suffix} → {folder}/"
    job.emit({"type": "item_log", "item": name, "line": line})

    if config.settings.dry_run:
        if await asyncio.to_thread(_get_entry, key):
            if _after_mode(job) == "keep":
                job.emit({"type": "item_skipped", "item": name, "reason": "already_archived",
                          "message": f"Déjà sur 1fichier ({folder})."})
                return
            job.emit({"type": "item_log", "item": name, "line": f"Déjà sur 1fichier ({folder})."})
            await _after_backup(job, name, unit, kind, show)
            return
        log_action("SIMULE", "1FICHIER", f"{unit} -> {folder}/")
        job.emit({"type": "item_log", "item": name, "line": "[dry-run] envoi simulé"})
        await _after_backup(job, name, unit, kind, show)
        return

    base = os.path.basename(unit)
    parent = os.path.dirname(unit)
    level = config.settings.onefichier_compression
    episodes = (await asyncio.to_thread(scan_pack, unit))["episodes"] if season is not None else None
    avertissement = None
    if getattr(job, "resumed", False):
        traite, avertissement = await _reprendre_envoi(job, name, unit, kind, show, key, folder)
        if traite:
            return
    fields = {
        "kind": kind, "tmdb_id": ident["tmdb_id"], "title": ident["title"], "year": ident["year"],
        "original_name": base, "original_path": unit, "archive_name": archive_name_for(key, folder),
        "folder": folder, "source_size": size, "compression": level, "meta": ident["meta"],
        "adult": kind == KIND_FILM and adult_media.is_adult_path(unit),
        "show_name": os.path.basename(show) if show else None, "season": season, "episodes": episodes,
        "notice": avertissement,
    }
    if not await asyncio.to_thread(_get_entry, key):
        # Un envoi commence : il attend une plage activée. Avant _claim, car une entrée « en cours »
        # ne doit pas exister pendant l'attente. Un élément déjà connu de l'index passe sans attendre.
        await scheduler.wait_for_window(job)
    entry, existing = await asyncio.to_thread(_claim, key, fields)
    if existing:
        if existing.get("status") == "done" and _after_mode(job) != "keep":
            job.emit({"type": "item_log", "item": name, "line": f"Déjà sur 1fichier ({folder})."})
            await _after_backup(job, name, unit, kind, show)
            return
        if existing.get("status") == "done":
            reason, message = "already_archived", f"Déjà sur 1fichier ({folder})."
        else:
            reason = "pending"
            message = ("Un envoi précédent est resté incomplet. Retire-le de la liste 1fichier "
                       "puis relance (le fichier partiel éventuel reste à supprimer sur 1fichier).")
        job.emit({"type": "item_skipped", "item": name, "reason": reason, "message": message})
        return

    _ACTIVE.add(key)
    _progress(key, stage="Préparation", percent=None, bytes_done=None, bytes_total=size,
              speed=None, source=unit, archive=entry["archive_name"], folder=folder)
    password = entry["password"]
    work = os.path.join(config.settings.onefichier_staging_dir, uuid.uuid4().hex)
    upload_started = False
    backed_up = False
    try:
        await asyncio.to_thread(_ensure_space, config.settings.onefichier_staging_dir,
                                int(size * 1.02) + SAFETY_SLACK_BYTES)
        os.makedirs(work)
        archive_path = os.path.join(work, entry["archive_name"])

        job.emit({"type": "item_log", "item": name, "line": f"Compression 7z chiffrée (AES-256, niveau {level})…"})
        _progress(key, stage="Compression 7z chiffrée (AES-256)", percent=None)
        await asyncio.to_thread(_7z_create, parent, base, archive_path, password, level)
        _progress(key, stage="Vérification de l'archive", percent=None)
        await asyncio.to_thread(_7z_test, archive_path, password)
        _progress(key, stage="Empreinte SHA-256 de l'archive", percent=None)
        digest = await asyncio.to_thread(_sha256_file, archive_path)
        await asyncio.to_thread(_set_sha256, key, digest)
        job.emit({"type": "item_log", "item": name, "line": f"Empreinte SHA-256 : {digest[:16]}…"})
        local_size = os.path.getsize(archive_path)
        if local_size > MAX_FILE_BYTES:
            raise OneFichierError("archive trop volumineuse pour 1fichier (plafond 300 Go)")
        await asyncio.to_thread(_set_archive_size, key, local_size)

        async with httpx.AsyncClient() as client:
            if adult_media.is_adult_item(entry):  # film adulte : ADULTES/tmdb-<id>, pas la racine
                folder_id = await _ensure_folder(client, adult_folder_key(folder_key_for(key)), folder,
                                                 parent_key=ADULT_ROOT_KEY)
            else:
                folder_id = await _ensure_folder(client, folder_key_for(key), folder)
        await asyncio.to_thread(_set_folder_id, key, folder_id)

        job.emit({"type": "item_log", "item": name, "line": "Envoi vers 1fichier…"})
        _progress(key, stage="Connexion à 1fichier", percent=None, bytes_total=local_size)
        async with httpx.AsyncClient() as client:
            node, upload_id = await _upload_server(client)  # aucun fichier transféré à cette étape
        upload_started = True  # à partir d'ici, des données peuvent être parties : on garde l'entrée en cas d'échec
        await asyncio.to_thread(_set_upload_started, key)
        holder: dict = {}
        await _with_progress(
            job, name, local_size,
            lambda: holder["reader"].done if "reader" in holder else 0,
            lambda: _upload_blocking(node, upload_id, folder_id, archive_path, holder),
            key=key,
        )

        _progress(key, stage="Finalisation de l'envoi", percent=100)
        link = await _end_upload(node, upload_id)
        remote_size = int(link.get("size", -1))
        if remote_size != local_size:
            raise OneFichierError(
                f"taille différente côté 1fichier ({remote_size} o au lieu de {local_size} o) : "
                f"envoi à refaire, le fichier distant est à vérifier"
            )
        url = link.get("download")
        if not url:
            raise OneFichierError("envoi terminé mais aucun lien de téléchargement n'a été renvoyé")
        _check_host(str(url))
        await asyncio.to_thread(_mark_done, key, str(url), remote_size)
        if config.settings.archive_copy_dir:
            # Deuxième copie, indépendante de 1fichier. Un échec ici ne remet pas en cause l'envoi.
            try:
                _progress(key, stage="Copie de l'archive sur le disque", percent=None)
                copy_path = await asyncio.to_thread(_copy_archive_to_disk, archive_path, digest, entry["archive_name"])
                await asyncio.to_thread(_set_local_copy, key, copy_path)
                job.emit({"type": "item_log", "item": name, "line": f"Copie sur disque : {copy_path}"})
            except OneFichierError as exc:
                log_action("ALERTE", "1FICHIER", f"{unit} : copie sur disque impossible ({exc})")
                await notify.send("Copie de l'archive sur disque impossible",
                                  f"{libelle_sortant} : {'détail masqué' if adulte else exc}")
                job.emit({"type": "item_log", "item": name, "line": f"Copie sur disque impossible : {exc}"})
        if _has_tmdb(entry):
            try:
                await refresh_meta({key}, force=True)
            except Exception:
                pass

        log_action("OK", "1FICHIER", f"{unit} -> {folder}/ ({remote_size} o)")
        backed_up = True
    except Exception as exc:
        message = str(exc) if isinstance(exc, OneFichierError) else f"erreur inattendue ({exc})"
        await notify.send("Sauvegarde 1fichier échouée",
                          f"{libelle_sortant} : {'détail masqué' if adulte else message}")
        if upload_started:
            # Un envoi a pu commencer : on garde l'entrée (et son mot de passe) pour ne rien perdre.
            await asyncio.to_thread(_mark_error, key, message)
        else:
            # Rien n'est parti vers 1fichier : l'entrée est retirée, l'élément reste réessayable.
            await asyncio.to_thread(_release, key, password)
        raise
    finally:
        _ACTIVE.discard(key)
        _PROGRESS.pop(key, None)
        shutil.rmtree(work, ignore_errors=True)
    if backed_up:
        await _after_backup(job, name, unit, kind, show)


def _after_mode(job) -> str:
    return getattr(job, "after", "keep") or "keep"


def _disk_dest(kind: str, root: Optional[str], adult: bool = False) -> str:
    """Dossier de destination sur un disque externe détecté (même règle que les
    restaurations : le disque doit être monté, le sous-dossier MEDIA/… est créé)."""
    known = {os.path.normpath(d["path"]) for d in disks.list_destination_disks()}
    root = os.path.normpath(root or "")
    if root not in known:
        raise OneFichierError(f"disque non détecté ou non monté : {root or '(aucun)'}")
    return os.path.join(root, _subdir_for(kind, adult))


async def _retirer_fiche_arr(job, unit: str, kind: str, show: Optional[str]) -> Optional[str]:
    """Retire la fiche Radarr/Sonarr de l'élément dont la copie locale est libérée, si la tâche le
    demande (version 49). Renvoie un message d'erreur (la copie locale est alors gardée), ou None.
    Une saison seule ne retire rien : la série reste suivie par Sonarr."""
    if not getattr(job, "retirer_arr", False):
        return None
    media = ARR_MEDIA_BY_KIND.get(kind)
    if not media or show is not None:
        return None
    try:
        plan = await arr.preparer(media, [unit])
        if not plan:
            return None  # application non configurée pour ce type
        if config.settings.dry_run:
            for entry in plan:
                log_action("SIMULE", "ARR", f"{entry['app']} : {entry['title']} ({entry['status']})")
            return None
        resultats = await arr.retirer(plan)
    except arr.ArrError as exc:
        return str(exc)
    for r in resultats:
        log_action("OK" if r["result"] == "retiré" else "INFO", "ARR",
                   f"{r['app']} : {r['title']} ({r['result']}, fichiers conservés)")
    return None


async def _after_backup(job, name: str, unit: str, kind: str, show: Optional[str] = None) -> None:
    """Étape après une sauvegarde 1fichier vérifiée :
    - keep   : rien de plus (copie locale conservée) ;
    - delete : la copie locale est supprimée (libère le serveur) ;
    - move   : la copie locale est copiée sur le disque choisi, vérifiée, puis retirée.
    Une erreur ici est signalée à part : la sauvegarde 1fichier, elle, reste valable."""
    mode = _after_mode(job)
    if mode == "keep":
        job.emit({"type": "item_done", "item": name})
        return
    dry = config.settings.dry_run
    try:
        if mode == "delete":
            if not dry and not config.ALLOW_DELETE:  # garde à l'exécution (version 45)
                log_action("PARTIEL", "LIBERER", f"{unit} (sauvegardé sur 1fichier, suppression désactivée : ALLOW_DELETE=false)")
                job.emit({"type": "item_partial", "item": name,
                          "error": "Sauvegardé sur 1fichier, mais la copie locale n'a pas été supprimée (ALLOW_DELETE=false)."})
                return
            # Fiche Radarr / Sonarr retirée avant la copie locale : si cela échoue, la copie reste (version 49).
            erreur_arr = await _retirer_fiche_arr(job, unit, kind, show)
            if erreur_arr:
                log_action("PARTIEL", "LIBERER", f"{unit} (sauvegardé sur 1fichier, fiche non retirée : {erreur_arr})")
                job.emit({"type": "item_partial", "item": name,
                          "error": f"Sauvegardé sur 1fichier, mais la fiche n'a pas pu être retirée de Radarr / Sonarr : {erreur_arr} La copie locale est conservée."})
                return
            if dry:
                log_action("SIMULE", "LIBERER", unit)
                job.emit({"type": "item_log", "item": name, "line": "[dry-run] copie locale supprimée (simulé)"})
                job.emit({"type": "item_done", "item": name})
                return
            from . import jobs as jobs_mod  # import tardif : jobs importe déjà onefichier
            try:
                await asyncio.to_thread(jobs_mod._remove_path, unit)
            except OSError as exc:
                log_action("PARTIEL", "LIBERER", f"{unit} (sauvegardé sur 1fichier, suppression impossible : {exc})")
                job.emit({"type": "item_partial", "item": name,
                          "error": f"Sauvegardé sur 1fichier, mais la copie locale n'a pas pu être supprimée : {exc}"})
                return
            if os.path.lexists(unit):
                job.emit({"type": "item_partial", "item": name,
                          "error": "Sauvegardé sur 1fichier, mais la copie locale est encore présente."})
                return
            log_action("OK", "LIBERER", unit)
            job.emit({"type": "item_log", "item": name, "line": "Copie locale supprimée : place libérée."})
            job.emit({"type": "item_done", "item": name})
            return

        # move : copie vérifiée sur le disque, puis retrait de la source (fait par jobs)
        from . import jobs as jobs_mod
        dest_dir = _disk_dest(kind, job.destination, adult=kind == KIND_FILM and adult_media.is_adult_path(unit))
        if show:  # une saison va dans le dossier de sa série sur le disque
            dest_dir = os.path.join(dest_dir, identities.dest_name("series", show) or os.path.basename(show))
        else:  # un film ou une série entière prend son nom identifié (comme une copie)
            ident_kind = {KIND_FILM: "films", KIND_SERIE: "series"}.get(kind)
            named = identities.dest_name(ident_kind, unit) if ident_kind else None
            if named:
                job.dest_names[unit] = named
        if not dry:
            os.makedirs(dest_dir, exist_ok=True)
        # Une copie par disque, comme les autres copies : l'emplacement du disque est pris par _copy_or_move_one
        # (version 45), après l'attente d'une plage horaire.
        await jobs_mod._copy_or_move_one(job, unit, dest_dir, move=True)
    except Exception as exc:
        job.emit({"type": "item_partial", "item": name,
                  "error": f"Sauvegardé sur 1fichier, mais l'étape suivante a échoué : {exc}"})


async def recover_pending(tmdb_id) -> dict:
    """Retrouve sur 1fichier l'archive d'un envoi resté « incomplet » dans
    l'index (cas : l'envoi a abouti, mais l'enregistrement a été interrompu).
    Recherche le fichier tmdb-<id>.7z dans son dossier et rétablit le lien.
    La restauration vérifie ensuite le contenu avec le mot de passe enregistré."""
    key = str(tmdb_id)
    if key in _ACTIVE:
        raise OneFichierError("envoi en cours : attends la fin de l'opération.", 409)
    entry = await asyncio.to_thread(_get_entry, key)
    if not entry:
        raise OneFichierError("entrée introuvable", 404)
    if entry.get("status") != "pending":
        raise OneFichierError("cette entrée est déjà terminée : rien à retrouver.", 409)
    if entry.get("folder_id") is None:
        raise OneFichierError("aucun dossier 1fichier enregistré pour cet envoi : rien à vérifier.", 409)

    async with httpx.AsyncClient() as client:
        data = await _call(client, "POST", "/file/ls.cgi", {"folder_id": int(entry["folder_id"])})
    listing = data.get("files") or data.get("items") or []
    match = next((f for f in listing if isinstance(f, dict) and f.get("filename") == entry["archive_name"]), None)
    if not match:
        raise OneFichierError(
            f"aucun fichier {entry['archive_name']} dans le dossier {entry['folder']} sur 1fichier : "
            f"l'envoi n'a pas abouti. Tu peux retirer l'entrée.", 404)

    size = int(match.get("size") or 0)
    if size <= 0:
        raise OneFichierError("fichier trouvé mais vide : envoi incomplet, à refaire.", 409)
    url = str(match.get("url") or "")
    if url and not url.startswith("http"):
        url = f"https://1fichier.com/?{url}"
    if not url:
        raise OneFichierError("1fichier n'a pas renvoyé le lien du fichier.")
    _check_host(url)

    def fn(d):
        current = d["entries"].get(key)
        if not current or current.get("status") != "pending":
            raise OneFichierError("entrée modifiée entre-temps : rien n'a été changé.", 409)
        current.update(status="done", url=url, remote_size=size, size=size,
                       archived_at=time.time(), error=None, recovered=True)
    await asyncio.to_thread(_update, fn)
    try:
        await refresh_meta({key}, force=True)
    except Exception:
        pass

    log_action("OK", "1FICHIER", f"{folder_for(key)} : envoi retrouvé sur 1fichier ({size} o)")
    tmdb_value = int(key) if key.isdigit() else entry.get("tmdb_id")  # films : la clé est l'identifiant
    return {"key": key, "tmdb_id": tmdb_value, "size": size}


def reveal_entry(key: str) -> dict:
    """Lien et mot de passe d'une archive terminée, pour les copier hors de l'application.
    Ces champs ne figurent JAMAIS dans la liste (list_entries) : ils ne sont renvoyés
    que lorsque l'interface les demande explicitement (bouton « Afficher »)."""
    entry = _get_entry(key)
    if not entry:
        raise OneFichierError("entrée introuvable", 404)
    if entry.get("status") != "done" or not entry.get("url"):
        raise OneFichierError("seul un envoi terminé a un lien à copier.", 409)
    return {
        "url": entry["url"],
        "password": entry["password"],
        "archive_name": entry.get("archive_name"),
        "folder": entry.get("folder"),
    }


async def check_entry(key: str) -> dict:
    """Vérifie auprès de 1fichier que l'archive existe toujours dans son dossier
    (donc que le lien fonctionne) et lit sa date d'envoi telle que 1fichier la connaît.
    Le résultat est enregistré dans l'index : link_ok, link_checked_at, uploaded_at.
    Une erreur réseau ou d'API ne modifie rien : seul un constat est enregistré."""
    if key in _ACTIVE:
        raise OneFichierError("envoi en cours : attends la fin de l'opération.", 409)
    entry = await asyncio.to_thread(_get_entry, key)
    if not entry:
        raise OneFichierError("entrée introuvable", 404)
    if entry.get("status") != "done":
        raise OneFichierError("seul un envoi terminé peut être vérifié.", 409)
    folder_id = entry.get("folder_id")
    if folder_id is None:
        fkey = folder_key_for(key)
        folder_id = _snapshot()["folders"].get(adult_folder_key(fkey) if adult_media.is_adult_item(entry) else fkey)
    if folder_id is None:
        raise OneFichierError("dossier 1fichier inconnu : impossible de vérifier cet envoi.", 409)

    async with httpx.AsyncClient() as client:
        data = await _call(client, "POST", "/file/ls.cgi", {"folder_id": int(folder_id)})
    listing = data.get("files") or data.get("items") or []
    match = next((f for f in listing if isinstance(f, dict) and f.get("filename") == entry["archive_name"]), None)
    link_ok = match is not None
    remote_date = str(match.get("date")) if match and match.get("date") not in (None, "") else None
    # Version 50 : taille distante comparée à la taille enregistrée à l'envoi. Une différence signale
    # une archive tronquée ou modifiée chez 1fichier. Sans taille (liste incomplète), rien n'est conclu.
    taille_distante = int(match.get("size") or 0) if match else 0
    taille_attendue = int(entry.get("remote_size") or entry.get("size") or 0)
    taille_ok = (taille_distante == taille_attendue) if (link_ok and taille_distante and taille_attendue) else None
    checked_at = time.time()

    def fn(d):
        current = d["entries"].get(key)
        if not current or current.get("status") != "done":
            raise OneFichierError("entrée modifiée entre-temps : rien n'a été changé.", 409)
        current.update(link_ok=link_ok, link_checked_at=checked_at,
                       uploaded_at=remote_date or current.get("uploaded_at"))
        if taille_ok is not None:
            current["taille_ok"] = taille_ok
        return {"link_ok": link_ok, "link_checked_at": checked_at,
                "uploaded_at": current.get("uploaded_at"), "taille_ok": taille_ok}
    result = await asyncio.to_thread(_update, fn)
    log_action("OK" if link_ok else "ALERTE", "1FICHIER",
               f"{folder_for(key)} : lien {'actif' if link_ok else 'introuvable'} (vérification)")
    return {"key": key, **result}


async def run_archive(job) -> None:
    if not is_configured():
        job.emit({"type": "job_error", "error": "Clé API 1fichier non renseignée (Réglages)."})
        return
    needs_tmdb = False
    for item in job.items:
        try:
            needs_tmdb = needs_tmdb or classify(item)[0] in (KIND_FILM, KIND_SERIE)
        except OneFichierError:
            pass  # l'erreur sera signalée élément par élément
    if needs_tmdb and not config.settings.tmdb_enabled:
        job.emit({"type": "job_error", "error": "TMDB doit être activé : il sert à nommer les dossiers films et séries."})
        return
    for item in job.items:
        name = unit_label(item.rstrip("/"))
        try:
            # Un seul envoi (ou restauration) 1fichier à la fois, et seulement pendant une plage
            # activée. L'emplacement est rendu entre deux archives : une tâche plus prioritaire passe.
            async with scheduler.slot(job, "onefichier"):
                job.emit({"type": "item_start", "item": name})
                await _archive_one(job, item, name)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, OneFichierError) else f"erreur inattendue ({exc})"
            _fail(job, name, item, reason)


# ---------------------------------------------------------------------
# Restauration
# ---------------------------------------------------------------------

def encode_target(target: str, disk: Optional[str] = None) -> str:
    """Destination d'un job de restauration, sous forme de chaîne :
    origin | default | disk:<chemin du disque>."""
    return f"disk:{os.path.normpath(disk)}" if target == "disk" and disk else target


def decode_target(value: Optional[str]) -> tuple[str, Optional[str]]:
    value = value or "default"
    if value.startswith("disk:"):
        return "disk", value[5:]
    return value, None


def restore_dir_for(kind: str) -> str:
    return {KIND_FILM: config.settings.restore_dir_films,
            KIND_SERIE: config.settings.restore_dir_series,
            KIND_EBOOK: config.settings.restore_dir_ebooks}[kind]


def _subdir_for(kind: str, adult: bool = False) -> str:
    if adult and kind == KIND_FILM:
        return adult_media.adult_subdir() or config.settings.dest_subdir_films
    return {KIND_FILM: config.settings.dest_subdir_films,
            KIND_SERIE: config.settings.dest_subdir_series,
            KIND_EBOOK: config.settings.dest_subdir_ebooks}[kind]


def entry_label(entry: dict) -> str:
    """Nom affiché pour une entrée : « Série · Saison 1 » pour une saison."""
    if entry.get("season") is not None and entry.get("show_name"):
        return f"{entry['show_name']} · {entry.get('original_name') or ''}".rstrip(" ·")
    return entry.get("original_name") or folder_for(entry.get("key", ""))


def restore_parent(entry: dict, target: str, disk: Optional[str] = None) -> str:
    """Dossier dans lequel replacer l'élément restauré.
    - origin  : son emplacement d'origine (doit exister) ;
    - default : le dossier de restauration des réglages (doit exister : volume monté) ;
    - disk    : un disque externe détecté, dans le sous-dossier du type (créé au besoin).
    Une saison revient dans le dossier de sa série (créé au besoin)."""
    kind = entry_kind(entry)
    show_dir = entry.get("show_name") if entry.get("season") is not None else None
    if target == "origin":
        parent = os.path.dirname(entry["original_path"])
        if not os.path.isdir(parent):
            raise OneFichierError(f"emplacement d'origine introuvable : {parent} (disque monté ?)")
    elif target == "default":
        base = restore_dir_for(kind)
        if not base:
            raise OneFichierError(
                f"aucun dossier de restauration défini pour les {KIND_NAMES[kind]}s "
                f"(Réglages › 1fichier, section Restauration).")
        if not os.path.isdir(base):
            raise OneFichierError(
                f"dossier de restauration introuvable : {base} (ce dossier doit être monté dans le conteneur).")
        parent = base
    elif target == "disk":
        known = {os.path.normpath(d["path"]) for d in disks.list_destination_disks()}
        root = os.path.normpath(disk or "")
        if root not in known:
            raise OneFichierError(f"disque non détecté ou non monté : {root or '(aucun)'}")
        parent = os.path.join(root, _subdir_for(kind, bool(entry.get("adult"))))
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            raise OneFichierError(f"création de {parent} impossible : {exc}")
    else:
        raise OneFichierError(f"destination de restauration inconnue : {target}")
    if show_dir and target != "origin":
        parent = os.path.join(parent, show_dir)
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            raise OneFichierError(f"création de {parent} impossible : {exc}")
    if not os.access(parent, os.W_OK | os.X_OK):
        raise OneFichierError(f"dossier non accessible en écriture : {parent} (montage en lecture seule ?)")
    return parent


async def _fetch_archive(job, entry: dict, archive: str, size: int, name: str) -> str:
    """Récupère l'archive chiffrée depuis 1fichier. Si 1fichier ne répond pas et qu'une copie
    existe sur disque, celle-ci est utilisée. L'empreinte est vérifiée ensuite, quelle que soit la source."""
    holder: dict = {"done": 0}
    try:
        async with httpx.AsyncClient() as client:
            direct = await _download_url(client, entry["url"])
        job.emit({"type": "item_log", "item": name, "line": "Téléchargement depuis 1fichier…"})
        await _with_progress(job, name, size, lambda: holder["done"],
                             lambda: _download_blocking(direct, archive, holder))
        if size and os.path.getsize(archive) != size:
            raise OneFichierError("téléchargement incomplet (taille différente de celle enregistrée)")
        return "1fichier"
    except (OneFichierError, httpx.HTTPError, OSError) as exc:
        local = entry.get("local_copy")
        if not local or not os.path.isfile(local):
            raise
        job.emit({"type": "item_log", "item": name,
                  "line": f"1fichier indisponible ({exc}) : utilisation de la copie sur disque."})
    await asyncio.to_thread(shutil.copyfile, local, archive)
    return "disque"


async def _restore_one(job, entry: dict, target: str, disk: Optional[str]) -> None:
    base = entry["original_name"]       # nom réel (chemins, archive)
    name = entry_label(entry)           # nom affiché (événements)
    parent = restore_parent(entry, target, disk)
    dest = os.path.join(parent, base)
    if os.path.lexists(dest):
        job.emit({"type": "item_skipped", "item": name, "reason": "already_exists",
                  "message": "Existe déjà à l'emplacement de restauration : rien n'a été écrasé."})
        return

    size = int(entry.get("remote_size") or entry.get("size") or 0)
    staging = config.settings.onefichier_staging_dir
    if config.settings.dry_run:
        log_action("SIMULE", "RESTORE", f"{entry['folder']} -> {dest}")
        job.emit({"type": "item_log", "item": name, "line": "[dry-run] restauration simulée"})
        job.emit({"type": "item_done", "item": name})
        return

    await asyncio.to_thread(_ensure_space, staging, size * 2 + SAFETY_SLACK_BYTES)
    work = os.path.join(staging, uuid.uuid4().hex)
    os.makedirs(work)
    try:
        archive = os.path.join(work, entry["archive_name"])
        await _fetch_archive(job, entry, archive, size, name)
        expected = entry.get("sha256")
        if expected:
            job.emit({"type": "item_log", "item": name, "line": "Vérification de l'empreinte SHA-256…"})
            actual = await asyncio.to_thread(_sha256_file, archive)
            if actual != expected:
                raise OneFichierError(
                    "empreinte SHA-256 différente de celle de l'envoi : archive altérée, rien n'a été extrait. "
                    "Vérifie l'envoi sur 1fichier et refais-le si besoin.")
        else:
            job.emit({"type": "item_log", "item": name,
                      "line": "Pas d'empreinte enregistrée pour cet envoi : contrôle CRC 7z seulement."})

        out = os.path.join(work, "extract")
        os.makedirs(out)
        job.emit({"type": "item_log", "item": name, "line": "Déchiffrement et extraction (7z)…"})
        await asyncio.to_thread(_7z_extract, archive, entry["password"], out)

        extracted = os.path.join(out, base)
        if not os.path.lexists(extracted):
            raise OneFichierError("l'archive ne contient pas le fichier attendu")

        job.emit({"type": "item_log", "item": name, "line": "Remise dans la bibliothèque…"})
        try:
            await asyncio.to_thread(shutil.move, extracted, dest)
        except Exception:
            if os.path.lexists(dest):  # copie partielle : on la retire, l'original est intact sur 1fichier
                await asyncio.to_thread(_remove_partial, dest)
            raise

        key = entry["key"]
        await asyncio.to_thread(_update, lambda d: d["entries"][key].__setitem__("restored_at", time.time()))
        log_action("OK", "RESTORE", f"{entry['folder']} -> {dest}")
        job.emit({"type": "item_done", "item": name})
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _remove_partial(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.remove(path)
        except OSError:
            pass


def restore_labels(keys: list[str]) -> list[str]:
    """Libellés des entrées à restaurer (même nom que dans run_restore). Une seule lecture de
    l'index : relire le fichier pour chaque clé coûtait plusieurs secondes pour une centaine d'entrées."""
    with _lock:
        entries = _read()["entries"]
    out = []
    for key in keys:
        entry = entries.get(key)
        if entry:
            entry = {**entry, "key": key, "kind": entry_kind(entry)}
        out.append(entry_label(entry) if entry else folder_for(key))
    return out


async def run_restore(job) -> None:
    if not is_configured():
        job.emit({"type": "job_error", "error": "Clé API 1fichier non renseignée (Réglages)."})
        return
    for key in job.items:
        entry = await asyncio.to_thread(_get_entry, key)
        label = entry_label(entry) if entry else folder_for(key)
        try:
            async with scheduler.slot(job, "onefichier"):
                job.emit({"type": "item_start", "item": label})
                if not entry or entry.get("status") != "done":
                    raise OneFichierError("entrée absente de l'index ou envoi incomplet")
                await scheduler.wait_for_window(job)
                target, disk = decode_target(job.destination)
                await _restore_one(job, entry, target, disk)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, OneFichierError) else f"erreur inattendue ({exc})"
            _fail(job, label, folder_for(key), reason)


# ---------------------------------------------------------------------
# Cache de travail : archives temporaires des envois et restaurations
# ---------------------------------------------------------------------
# Chaque envoi ou restauration travaille dans un sous-dossier au nom aléatoire
# (uuid4 en hexadécimal) du dossier de travail, supprimé à la fin. Un arrêt en
# pleine opération peut en laisser un. Le vidage ne supprime QUE ces dossiers :
# les autres fichiers du dossier de travail ne sont jamais touchés.

WORK_DIR_RE = re.compile(r"^[0-9a-f]{32}$")


def transfer_active() -> bool:
    """Un envoi 1fichier est en cours dans ce processus."""
    return bool(_ACTIVE)


def staging_status() -> dict:
    """Utilisation du dossier de travail : dossiers temporaires reconnus (taille),
    place libre, et nombre d'autres éléments (conservés, jamais supprimés)."""
    path = config.settings.onefichier_staging_dir
    out = {"path": path, "exists": os.path.isdir(path), "free_bytes": None,
           "work_dirs": 0, "work_bytes": 0, "work_items": [], "other_count": 0}
    if not out["exists"]:
        return out
    try:
        out["free_bytes"] = shutil.disk_usage(path).free
        names = sorted(os.listdir(path))
    except OSError:
        return out
    for name in names:
        full = os.path.join(path, name)
        if WORK_DIR_RE.match(name) and os.path.isdir(full) and not os.path.islink(full):
            try:
                mtime = int(os.path.getmtime(full))
            except OSError:
                mtime = 0
            size = _tree_size(full)
            out["work_items"].append({"name": name, "size": size, "mtime": mtime})
            out["work_bytes"] += size
        else:
            out["other_count"] += 1
    out["work_dirs"] = len(out["work_items"])
    return out


def clear_staging() -> dict:
    """Supprime les dossiers temporaires reconnus. L'appelant vérifie qu'aucun
    transfert n'est en cours ou en attente. En simulation, rien n'est supprimé."""
    status = staging_status()
    removed = 0
    freed = 0
    for item in status["work_items"]:
        full = os.path.join(status["path"], item["name"])
        if config.settings.dry_run:
            log_action("SIMULE", "CACHE", f"dossier de travail {item['name']} (non supprimé en simulation)")
        else:
            shutil.rmtree(full, ignore_errors=True)
            if os.path.exists(full):
                log_action("ECHEC", "CACHE", f"dossier de travail {item['name']} non supprimé")
                continue
            log_action("OK", "CACHE", f"dossier de travail {item['name']} supprimé")
        removed += 1
        freed += item["size"]
    return {"removed": removed, "freed_bytes": freed, "dry_run": bool(config.settings.dry_run)}


# ---------------------------------------------------------------------
# Contrôles planifiés (version 30) : liens, test de restauration, alertes
# ---------------------------------------------------------------------

CHECK_HOUR = int(os.environ.get("ONEFICHIER_CHECK_HOUR", "3"))  # heure locale du contrôle hebdomadaire
CHECK_WEEKDAY = int(os.environ.get("ONEFICHIER_CHECK_WEEKDAY", "6"))  # jour : 0 = lundi … 6 = dimanche (version 50)
CHECK_PAUSE = 1.0                      # secondes entre deux appels à 1fichier
RESTORE_TEST_EVERY = 30 * 86400        # chaque élément est testé au plus une fois par mois
# Nombre de tests de restauration à chaque contrôle (version 50). 1 par défaut : une archive par semaine.
RESTORE_TESTS_PER_RUN = max(1, int(os.environ.get("ONEFICHIER_RESTORE_TESTS_PER_RUN", "1")))
RESTORE_TEST_MAX_BYTES = 20 * 10**9    # test limité aux archives de 20 Go au plus (bande passante)
_checks = {"running": False}


def _next_run_at(now: Optional[float] = None) -> float:
    """Prochain contrôle hebdomadaire : le jour CHECK_WEEKDAY à CHECK_HOUR (heure locale)."""
    now = time.time() if now is None else now
    t = time.localtime(now)
    candidate = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, CHECK_HOUR, 0, 0, 0, 0, -1))
    candidate += ((CHECK_WEEKDAY - t.tm_wday) % 7) * 86400
    if candidate <= now:
        candidate += 7 * 86400
    return candidate


def _adult_keys() -> set[str]:
    """Clés des envois de films adultes (marque ou dossier adulte)."""
    return {key for key, entry in _snapshot()["entries"].items() if adult_media.is_adult_item(entry)}


def _etiquette(item: dict, adultes: set[str]) -> str:
    """Nom d'un élément pour une notification sortante : un élément adulte n'est désigné que par son numéro."""
    if item.get("key") in adultes:
        return f"élément n° {item.get('key')}"
    return str(item.get("folder") or item.get("key") or "")


def checks_status(unlocked: bool = True) -> dict:
    """État des contrôles. Sans session, les résultats des envois adultes sont retirés de la dernière
    passe (leur clé et leur dossier révéleraient le titre)."""
    last = _snapshot().get("last_check_run")
    if last and not unlocked:
        adultes = _adult_keys()
        last = {**last, **{champ: [x for x in last.get(champ, []) if not (isinstance(x, dict) and x.get("key") in adultes)]
                           for champ in ("links_lost", "tests")}}
    return {
        "running": _checks["running"],
        "last_run": last,
        "next_run_at": _next_run_at() if is_configured() else None,
    }


async def test_restore(key: str) -> dict:
    """Télécharge l'archive depuis 1fichier, vérifie son empreinte et son CRC 7z, puis la supprime.
    Rien n'est extrait : le test prouve que l'archive est lisible avec son mot de passe."""
    entry = await asyncio.to_thread(_get_entry, key)
    if not entry:
        raise OneFichierError("entrée introuvable", 404)
    size = int(entry.get("remote_size") or entry.get("size") or 0)
    staging = config.settings.onefichier_staging_dir
    result = {"key": key, "folder": entry.get("folder"), "ok": False, "error": None}
    try:
        await asyncio.to_thread(_ensure_space, staging, size * 2 + SAFETY_SLACK_BYTES)
    except OneFichierError as exc:
        # Problème de configuration (dossier de travail) : ce n'est pas un résultat de test.
        # Rien n'est enregistré, le test sera retenté au prochain contrôle.
        result.update(ok=None, error=str(exc), at=None)
        log_action("ALERTE", "1FICHIER", f"{entry.get('folder')} : test de restauration impossible ({exc})")
        return result
    work = None
    try:
        work = tempfile.mkdtemp(prefix="test-restore-", dir=staging)
        archive = os.path.join(work, entry["archive_name"])
        async with httpx.AsyncClient() as client:
            direct = await _download_url(client, entry["url"])
        holder: dict = {"done": 0}
        await asyncio.to_thread(_download_blocking, direct, archive, holder)
        if size and os.path.getsize(archive) != size:
            raise ArchiveCorrompue("téléchargement incomplet (taille différente)")
        if entry.get("sha256") and await asyncio.to_thread(_sha256_file, archive) != entry["sha256"]:
            raise ArchiveCorrompue("empreinte SHA-256 différente de celle de l'envoi")
        await asyncio.to_thread(_7z_test, archive, entry["password"])
        result["ok"] = True
    except Exception as exc:
        result["error"] = str(exc) if isinstance(exc, OneFichierError) else f"erreur inattendue ({exc})"
        # Seule une archive abîmée est marquée. Une erreur réseau ne dit rien de l'archive.
        result["corrompue"] = isinstance(exc, ArchiveCorrompue)
    finally:
        if work:
            shutil.rmtree(work, ignore_errors=True)
    checked = time.time()
    result["at"] = checked

    def fn(d):
        current = d["entries"].get(key)
        if current:
            current.update(last_test_at=checked, last_test_ok=result["ok"], last_test_error=result["error"])
            if result["ok"] is True:
                current["corrompu"] = False
            elif result.get("corrompue"):
                current["corrompu"] = True
    await asyncio.to_thread(_update, fn)
    log_action("OK" if result["ok"] else "ALERTE", "1FICHIER",
               f"{entry.get('folder')} : test de restauration {'réussi' if result['ok'] else 'échoué'}")
    return result


async def _pick_restore_test(exclus: frozenset = frozenset()) -> Optional[str]:
    """L'élément le moins récemment testé, parmi les archives assez petites et jamais testées depuis un mois."""
    now = time.time()
    candidates = []
    for key, e in _snapshot()["entries"].items():
        if e.get("status") != "done" or key in _ACTIVE or key in exclus:
            continue
        size = int(e.get("remote_size") or e.get("size") or 0)
        if size <= 0 or size > RESTORE_TEST_MAX_BYTES:
            continue
        last = e.get("last_test_at") or 0
        if now - last >= RESTORE_TEST_EVERY:
            candidates.append((last, key))
    return min(candidates)[1] if candidates else None


async def run_checks(trigger: str = "manuel") -> dict:
    """Vérifie tous les liens, puis teste la restauration d'un élément. Alerte en cas de problème."""
    if _checks["running"]:
        raise OneFichierError("contrôles déjà en cours.", 409)
    if transfer_active():
        raise OneFichierError("un envoi ou une restauration est en cours : contrôles reportés.", 409)
    _checks["running"] = True
    report = {"at": time.time(), "trigger": trigger, "links_ok": 0, "links_lost": [], "tailles_differentes": [],
              "errors": [], "tests": [], "dry_run": bool(config.settings.dry_run)}
    try:
        if config.settings.dry_run:
            log_action("SIMULE", "CONTROLE", f"{trigger} : contrôle simulé")
        else:
            for key, e in list(_snapshot()["entries"].items()):
                if e.get("status") != "done" or key in _ACTIVE:
                    continue
                try:
                    result = await check_entry(key)
                    if result["link_ok"]:
                        report["links_ok"] += 1
                    else:
                        report["links_lost"].append({"key": key, "folder": e.get("folder")})
                    if result.get("taille_ok") is False:
                        report["tailles_differentes"].append({"key": key, "folder": e.get("folder")})
                except OneFichierError as exc:
                    report["errors"].append({"key": key, "error": str(exc)})
                await asyncio.sleep(CHECK_PAUSE)
            choisis: set[str] = set()
            for _ in range(RESTORE_TESTS_PER_RUN):
                pick = await _pick_restore_test(frozenset(choisis))
                if not pick:
                    break
                if scheduler.active_window() is None:
                    # Le test télécharge une archive : il attend une plage (il sera retenté au prochain contrôle).
                    log_action("OK", "CONTROLE", f"{trigger} : test de restauration reporté (hors plage de transfert)")
                    break
                choisis.add(pick)
                test = await test_restore(pick)
                report["tests"].append({"key": pick, "folder": test["folder"], "ok": test["ok"], "error": test["error"]})
    finally:
        _checks["running"] = False

    await asyncio.to_thread(_update, lambda d: d.__setitem__("last_check_run", report))
    failed_tests = [t for t in report["tests"] if t["ok"] is not True]
    if report["links_lost"] or report["errors"] or failed_tests or report["tailles_differentes"]:
        # La notification sort du serveur : un élément adulte n'y est désigné que par son numéro.
        adultes = _adult_keys()
        lines = []
        if report["links_lost"]:
            lines.append(f"{len(report['links_lost'])} lien(s) introuvable(s) : "
                         + ", ".join(_etiquette(x, adultes) for x in report["links_lost"]))
        if report["tailles_differentes"]:
            lines.append(f"{len(report['tailles_differentes'])} archive(s) de taille différente chez 1fichier : "
                         + ", ".join(_etiquette(x, adultes) for x in report["tailles_differentes"]))
        if report["errors"]:
            lines.append(f"{len(report['errors'])} erreur(s) de contrôle (1fichier injoignable ?)")
        for t in failed_tests:
            detail = "détail masqué" if t.get("key") in adultes else t["error"]
            lines.append(f"test de restauration non réussi : {_etiquette(t, adultes)} ({detail})")
        await notify.send("1fichier : contrôle à vérifier", "\n".join(lines))
    log_action("OK" if not (report["links_lost"] or failed_tests) else "ALERTE", "CONTROLE",
               f"{trigger} : {report['links_ok']} lien(s) OK, {len(report['links_lost'])} introuvable(s), "
               f"{len(report['errors'])} erreur(s), {len(report['tests'])} test(s)")
    return report


async def run_checks_safely(trigger: str) -> None:
    try:
        await run_checks(trigger)
    except OneFichierError as exc:
        log_action("ALERTE", "CONTROLE", f"{trigger} : impossible ({exc})")
    except Exception as exc:
        log_action("ALERTE", "CONTROLE", f"{trigger} : erreur inattendue ({exc})")


async def scheduler_loop() -> None:
    """Un contrôle complet par semaine (jour CHECK_WEEKDAY, heure CHECK_HOUR). Reporté tant qu'un envoi ou une restauration tourne."""
    while True:
        now = time.time()
        await asyncio.sleep(max(60.0, _next_run_at(now) - now))
        if not is_configured():
            continue
        while transfer_active():
            await asyncio.sleep(1800)
        await run_checks_safely("planifié")
        await asyncio.sleep(120)  # évite de reprogrammer le même passage
