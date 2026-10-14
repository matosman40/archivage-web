"""
Configuration éditable en direct depuis l'interface web.

Au premier démarrage, les valeurs par défaut viennent des variables
d'environnement (.env / docker-compose.yml). Dès qu'un réglage est modifié
depuis l'interface (PUT /api/settings), il est persisté dans SETTINGS_FILE
(sur le volume Docker /data, donc conservé entre redémarrages/rebuilds) et
prend effet immédiatement, sans redémarrer le conteneur.

Important : un chemin ne peut être utilisé ici que s'il est déjà visible à
l'intérieur du conteneur (monté via docker-compose.yml). Changer un chemin
depuis l'interface ne crée pas de nouveau montage Docker — voir le README,
section "Ajouter/changer un dossier source".
"""
import json
import os
import re
import threading

SETTINGS_FILE = os.environ.get("SETTINGS_FILE", "/data/settings.json")
LOG_FILE = os.environ.get("LOG_FILE", "/data/archivage.log")


def _split_paths(value) -> list[str]:
    if isinstance(value, list):
        return [str(p).strip() for p in value if str(p).strip()]
    return [p.strip() for p in str(value).split(",") if p.strip()]


def _split_exts(value) -> list[str]:
    if isinstance(value, list):
        items = value
    else:
        items = str(value).split(",")
    return [e.strip().lower() for e in items if e.strip()]


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_level(value) -> int:
    """Niveau de compression 7z (0 = stockage sans compression, 9 = maximum).
    Borné plutôt que rejeté : une valeur invalide dans settings.json ne doit
    jamais faire perdre TOUS les réglages persistés (voir _load)."""
    try:
        return max(0, min(9, int(value)))
    except (TypeError, ValueError):
        return 0


def _as_abs_dir(value) -> str:
    """Dossier absolu (ou vide = pas de dossier). Un chemin relatif est refusé."""
    v = str(value or "").strip()
    if not v:
        return ""
    if not v.startswith("/"):
        raise ValueError(f"chemin absolu attendu (ex. /MVE/MEDIA/VRAC/FILMS) : {v}")
    return os.path.normpath(v)


def _as_relative_subdir(value) -> str:
    """Un sous-dossier de destination doit être RELATIF (ex. 'MEDIA/FILMS').
    S'il commence par '/', os.path.join(disque, sous_dossier) ignore le
    disque et n'utilise que ce chemin absolu — c'est le bug exact qui a fait
    écrire une copie dans le système de fichiers du conteneur au lieu du
    disque externe choisi. On l'empêche ici, une fois pour toutes.
    Un « .. » sortirait du disque choisi : refusé aussi (version 42)."""
    morceaux = [m for m in str(value).strip().strip("/").split("/") if m]
    if any(m in (".", "..") for m in morceaux):
        raise ValueError("sous-dossier : « . » et « .. » ne sont pas permis")
    return "/".join(morceaux)


# Types de transfert, avec leur libellé. Chaque type a une vitesse par plage (clé « <type>_mbps »)
# et son emplacement dans la file. Un nouvel hébergeur s'ajoute ici ET dans l'interface
# (champ de vitesse de frontend/app.js) : sans champ, enregistrer remet sa vitesse à 0.
TRANSFER_KINDS = {"onefichier": "1fichier", "disk": "disque"}

# Plages de transfert (envois et restaurations 1fichier, copies vers disque).
# Hors plage, aucun élément ne démarre. Vitesses en Mo/s ; 0 = illimité.
DEFAULT_TRANSFER_WINDOWS = [
    {"name": "Jour", "start": "09:00", "end": "22:00", "enabled": True,
     "onefichier_mbps": 2, "disk_mbps": 30},
    {"name": "Nuit", "start": "01:00", "end": "07:00", "enabled": True,
     "onefichier_mbps": 0, "disk_mbps": 0},
]
MAX_TRANSFER_WINDOWS = 12
MAX_SPEED_MBPS = 10000


def parse_hhmm(value, allow_24: bool = False) -> int:
    """« HH:MM » -> minutes depuis minuit. « 24:00 » n'est accepté qu'en fin de plage."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(value or ""))
    if not m:
        raise ValueError(f"heure invalide {value!r} (format HH:MM, ex. 09:00)")
    hour, minute = int(m.group(1)), int(m.group(2))
    if allow_24 and (hour, minute) == (24, 0):
        return 24 * 60
    if hour > 23 or minute > 59:
        raise ValueError(f"heure invalide {value!r} (00:00 à 23:59)")
    return hour * 60 + minute


def format_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _as_speed(value, label: str) -> float:
    try:
        speed = float(value or 0)
    except (TypeError, ValueError):
        raise ValueError(f"{label} : nombre attendu")
    # `not 0 <= x <= max` rejette aussi NaN (toutes les comparaisons avec NaN sont fausses) :
    # un débit NaN faisait relancer rsync en boucle, puisque NaN != NaN.
    if not 0 <= speed <= MAX_SPEED_MBPS:
        raise ValueError(f"{label} : entre 0 (illimité) et {MAX_SPEED_MBPS} Mo/s")
    return speed


def _as_margin(value) -> int:
    """Marge de sécurité disque, en octets : entier positif ou nul. Une marge négative rendait
    tout l'espace libre utilisable, si bien que la copie pouvait remplir le disque à ras bord."""
    try:
        octets = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("marge de sécurité : nombre entier d'octets attendu")
    if octets < 0:
        raise ValueError("marge de sécurité : 0 octet ou plus")
    return octets


def _env_margin() -> int:
    """Marge lue dans l'environnement ; une valeur invalide revient à 500 Mio sans planter l'import."""
    try:
        return _as_margin(os.environ.get("SAFETY_MARGIN_BYTES", str(500 * 1024 * 1024)))
    except ValueError:
        return 500 * 1024 * 1024


def _as_windows(value) -> list:
    """Liste de plages normalisée. Une plage invalide refuse l'enregistrement (400 côté API)."""
    if not isinstance(value, list):
        raise ValueError("liste de plages attendue")
    if len(value) > MAX_TRANSFER_WINDOWS:
        raise ValueError(f"{MAX_TRANSFER_WINDOWS} plages au maximum")
    out = []
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"plage n° {index} : format invalide")
        name = str(raw.get("name") or "").strip()[:40] or f"Plage {index}"
        try:
            start = parse_hhmm(raw.get("start", "00:00"))
            end = parse_hhmm(raw.get("end", "24:00"), allow_24=True)
        except ValueError as exc:
            raise ValueError(f"plage « {name} » : {exc}")
        if start == end:
            raise ValueError(f"plage « {name} » : début et fin identiques")
        window = {
            "name": name,
            "start": format_hhmm(start),
            "end": format_hhmm(end),
            "enabled": _as_bool(raw.get("enabled", True)),
        }
        for kind, label in TRANSFER_KINDS.items():
            window[f"{kind}_mbps"] = _as_speed(raw.get(f"{kind}_mbps", 0), f"plage « {name} », {label}")
        out.append(window)
    return out


# Champs éditables, avec leur valeur par défaut lue depuis l'environnement
# et leur normalisation (liste de chemins, entier, booléen, ...).
_FIELD_DEFS = {
    "source_dirs_films": (lambda: _split_paths(os.environ.get("SOURCE_DIRS_FILMS", "/sources/films")), _split_paths),
    "source_dirs_series": (lambda: _split_paths(os.environ.get("SOURCE_DIRS_SERIES", "/sources/series")), _split_paths),
    "source_dirs_ebooks": (lambda: _split_paths(os.environ.get("SOURCE_DIRS_EBOOKS", "/sources/ebooks")), _split_paths),
    "dest_subdir_films": (lambda: _as_relative_subdir(os.environ.get("DEST_SUBDIR_FILMS", "MEDIA/FILM")), _as_relative_subdir),
    "dest_subdir_series": (lambda: _as_relative_subdir(os.environ.get("DEST_SUBDIR_SERIES", "MEDIA/SERIES")), _as_relative_subdir),
    "dest_subdir_ebooks": (lambda: _as_relative_subdir(os.environ.get("DEST_SUBDIR_EBOOKS", "MEDIA/EBOOKS")), _as_relative_subdir),
    # Médias adultes (version 34) : dossiers sources à part, sous-dossier de copie à part,
    # et affichage. Désactivé par défaut : éteint, ces films n'apparaissent dans aucune liste.
    "source_dirs_adultes": (lambda: _split_paths(os.environ.get("SOURCE_DIRS_ADULTES", "")), _split_paths),
    "dest_subdir_adultes": (lambda: _as_relative_subdir(os.environ.get("DEST_SUBDIR_ADULTES", "MEDIA/ADULTE")), _as_relative_subdir),
    "dest_scan_roots": (lambda: _split_paths(os.environ.get("DEST_SCAN_ROOTS", "/media,/mnt,/run/media")), _split_paths),
    "safety_margin_bytes": (_env_margin, _as_margin),
    "video_extensions": (lambda: _split_exts(os.environ.get("VIDEO_EXTENSIONS", ".mkv,.mp4,.avi,.m4v,.mov,.ts")), _split_exts),
    "ebook_extensions": (lambda: _split_exts(os.environ.get("EBOOK_EXTENSIONS", ".epub,.mobi,.azw3,.azw,.pdf,.cbz,.cbr,.djvu")), _split_exts),
    "plex_url": (lambda: os.environ.get("PLEX_URL", "").rstrip("/"), lambda v: str(v).rstrip("/")),
    "plex_token": (lambda: os.environ.get("PLEX_TOKEN", ""), str),
    "plex_timeout": (lambda: float(os.environ.get("PLEX_TIMEOUT", "10")), float),
    "tmdb_api_key": (lambda: os.environ.get("TMDB_API_KEY", ""), str),
    "tmdb_language": (lambda: os.environ.get("TMDB_LANGUAGE", "fr-FR"), str),
    "dry_run": (lambda: _as_bool(os.environ.get("DRY_RUN", "false")), _as_bool),
    "ebook_online_covers": (lambda: _as_bool(os.environ.get("EBOOK_ONLINE_COVERS", "true")), _as_bool),
    "delete_confirm_text": (lambda: os.environ.get("DELETE_CONFIRM_TEXT", "SUPPRIMER"), str),
    # 1fichier (compte premium) : clé API, dossier de travail pour les archives
    # temporaires, niveau de compression 7z.
    "onefichier_api_key": (lambda: os.environ.get("ONEFICHIER_API_KEY", "").strip(), lambda v: str(v or "").strip()),
    "onefichier_staging_dir": (lambda: _as_abs_dir(os.environ.get("ONEFICHIER_STAGING_DIR", "/MVE/TMP")) or "/MVE/TMP",
                               lambda v: _as_abs_dir(v) or "/MVE/TMP"),
    "onefichier_compression": (lambda: _as_level(os.environ.get("ONEFICHIER_COMPRESSION", "0")), _as_level),
    # Dossiers où remettre les éléments restaurés depuis 1fichier (vide = aucun).
    "restore_dir_films": (lambda: _as_abs_dir(os.environ.get("RESTORE_DIR_FILMS", "/MVE/MEDIA/VRAC/FILMS")), _as_abs_dir),
    "restore_dir_series": (lambda: _as_abs_dir(os.environ.get("RESTORE_DIR_SERIES", "/MVE/MEDIA/VRAC/SERIES")), _as_abs_dir),
    "restore_dir_ebooks": (lambda: _as_abs_dir(os.environ.get("RESTORE_DIR_EBOOKS", "/MVE/MEDIA/VRAC/EBOOKS")), _as_abs_dir),
    # Alertes : URL ntfy ou webhook Discord (vide = aucune alerte).
    "notify_url": (lambda: os.environ.get("NOTIFY_URL", "").strip(), lambda v: str(v or "").strip()),
    # Copie de chaque archive chiffrée sur un disque externe (vide = aucune copie).
    "archive_copy_dir": (lambda: _as_abs_dir(os.environ.get("ARCHIVE_COPY_DIR", "")), _as_abs_dir),
    # Plages horaires des transferts et vitesse de chacune (voir scheduler.py).
    "transfer_windows": (lambda: _as_windows(DEFAULT_TRANSFER_WINDOWS), _as_windows),
}

FIELDS = list(_FIELD_DEFS.keys())


class Settings:
    def __init__(self, data: dict):
        for key in FIELDS:
            setattr(self, key, data[key])

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in FIELDS}

    @property
    def all_source_dirs(self) -> list[str]:
        # Les dossiers adultes sont des sources comme les autres : les contrôles de sécurité
        # (copie, déplacement, suppression, destination) doivent les couvrir.
        return (self.source_dirs_films + self.source_dirs_adultes
                + self.source_dirs_series + self.source_dirs_ebooks)

    @property
    def video_extensions_tuple(self) -> tuple:
        return tuple(self.video_extensions)

    @property
    def ebook_extensions_tuple(self) -> tuple:
        return tuple(self.ebook_extensions)

    @property
    def plex_enabled(self) -> bool:
        return bool(self.plex_url and self.plex_token)

    @property
    def tmdb_enabled(self) -> bool:
        return bool(self.tmdb_api_key)

    @property
    def onefichier_enabled(self) -> bool:
        return bool(self.onefichier_api_key)


_lock = threading.Lock()


def _env_defaults() -> dict:
    """Valeurs de l'environnement, normalisées comme celles du fichier. Un débit NaN ou infini
    venu de l'environnement devient 0 (illimité) au lieu de passer tel quel."""
    out = {}
    for key, (default, norm) in _FIELD_DEFS.items():
        value = default()
        try:
            out[key] = norm(value)
        except (ValueError, TypeError):
            out[key] = 0.0 if isinstance(value, float) else value
    return out


def _load() -> Settings:
    data = _env_defaults()
    saved: dict = {}
    illisible = False
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                saved = loaded
            else:
                illisible = True
        except (OSError, ValueError, TypeError):
            illisible = True  # JSONDecodeError et UnicodeDecodeError sont des ValueError
        for key, value in saved.items():
            if key in _FIELD_DEFS:
                # Une valeur invalide garde la valeur par défaut de SA clé, sans perdre les autres réglages.
                try:
                    data[key] = _FIELD_DEFS[key][1](value)
                except (ValueError, TypeError):
                    pass
    if illisible:
        # Réglages illisibles : les valeurs de repli ne doivent pas lancer de vrais transferts.
        # Enregistrer les réglages depuis l'interface rétablit le fichier et désactive ce repli.
        data["dry_run"] = True
    return Settings(data)


def _save(s: Settings) -> None:
    """Écriture atomique : un fichier temporaire, fsync, puis remplacement. Un arrêt pendant
    l'écriture ne laisse jamais un settings.json tronqué (qui relancerait les valeurs de l'environnement)."""
    directory = os.path.dirname(SETTINGS_FILE)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{SETTINGS_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(s.as_dict(), fh, indent=2, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, SETTINGS_FILE)


# Instance courante, importée par tous les autres modules via
# `from . import config` puis `config.settings.xxx` (jamais
# `from .config import settings`, qui figerait une référence obsolète
# après un update_settings()).
settings = _load()


def update_settings(patch: dict) -> Settings:
    global settings
    with _lock:
        data = settings.as_dict()
        for key, value in patch.items():
            if key not in _FIELD_DEFS:
                continue
            norm = _FIELD_DEFS[key][1]
            try:
                data[key] = norm(value)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"Valeur invalide pour '{key}' : {exc}")
        nouveau = Settings(data)
        _save(nouveau)  # si l'écriture échoue, l'état en mémoire reste celui d'avant
        settings = nouveau
    return settings


def reset_settings() -> Settings:
    global settings
    with _lock:
        nouveau = Settings(_env_defaults())
        _save(nouveau)
        settings = nouveau
    return settings
