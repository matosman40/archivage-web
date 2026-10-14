"""Code d'accès aux médias adultes (version 36).

Le code est vérifié par le serveur. Seule son empreinte est enregistrée (PBKDF2-SHA256, sel
aléatoire) dans adult-code.json, à côté de settings.json. Elle n'apparaît jamais dans l'API.

Déverrouiller crée une session en mémoire : un jeton aléatoire envoyé dans un cookie HttpOnly
(hors de portée du JavaScript) et SameSite=Strict, sans date d'expiration : le navigateur
l'oublie à sa fermeture. Le serveur refuse aussi le jeton au bout de SESSION_MAX_AGE, et un
redémarrage efface toutes les sessions.

Après MAX_FAILURES échecs, les essais sont suspendus LOCKOUT_SECONDS. Le compteur n'est remis
à zéro qu'avec un code correct : après la pause, un nouvel échec suspend à nouveau.
"""
import hashlib
import hmac
import json
import math
import os
import secrets
import tempfile
import threading
import time
from typing import Optional

from . import config

COOKIE = "archivage_adult"
FILE_NAME = "adult-code.json"
ITERATIONS = 200_000
MIN_LENGTH = 4
MAX_LENGTH = 64
SESSION_MAX_AGE = 24 * 3600
MAX_FAILURES = 5
LOCKOUT_SECONDS = 300

_now = time.time  # remplaçable dans les tests
_lock = threading.RLock()
_sessions: dict[str, float] = {}   # empreinte du jeton -> fin de validité (horloge _now)
_failures = 0
_blocked_until = 0.0


class AdultAuthError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _path() -> str:
    return os.path.join(os.path.dirname(config.SETTINGS_FILE) or ".", FILE_NAME)


def _read() -> Optional[dict]:
    try:
        with open(_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not all(k in data for k in ("salt", "hash", "iterations")):
        return None
    return data


def _write(data: dict) -> None:
    path = _path()
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    # mkstemp crée le fichier en 0600 : seul le propriétaire peut le lire.
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".adult-code-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _digest(code: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", code.encode("utf-8"), salt, iterations)


def _matches(stored: dict, code: str) -> bool:
    try:
        salt = bytes.fromhex(stored["salt"])
        iterations = int(stored["iterations"])
        expected = bytes.fromhex(stored["hash"])
    except (ValueError, TypeError, KeyError):
        return False
    return hmac.compare_digest(_digest(code, salt, iterations), expected)


def _clean(code) -> str:
    return str(code or "").strip()


ILLISIBLE_MSG = ("Le fichier du code d'accès est illisible : il faut le réparer ou le supprimer "
                 "sur le serveur avant de continuer.")


def _etat() -> str:
    """« absent » (aucun code), « ok », ou « illisible » : le fichier existe mais ne se lit pas.
    Un fichier illisible ne doit JAMAIS passer pour « aucun code » : sinon n'importe qui pourrait
    en définir un nouveau et ouvrir une session (version 42)."""
    if not os.path.exists(_path()):
        return "absent"
    return "ok" if _read() is not None else "illisible"


def code_is_set() -> bool:
    return _etat() != "absent"


def _verify(code: str, stored: dict, wrong: str) -> None:
    """Lève AdultAuthError si l'essai est suspendu ou si le code est faux. Chaque échec compte,
    que ce soit pour déverrouiller ou pour changer le code : impossible de deviner l'ancien en boucle."""
    global _failures, _blocked_until
    with _lock:
        wait = retry_in()
        if wait:
            raise AdultAuthError(429, f"Trop d'essais. Réessaie dans {wait} s.")
        if not _matches(stored, code):
            _failures += 1
            if _failures >= MAX_FAILURES:
                _blocked_until = _now() + LOCKOUT_SECONDS
            raise AdultAuthError(403, wrong)
        _failures = 0


def set_code(new: str, current: Optional[str] = None) -> None:
    """Crée le code, ou le change si un code existe déjà (l'ancien est alors exigé).
    Toutes les sessions sont fermées : elles devront être recréées avec le nouveau code."""
    new = _clean(new)
    if not MIN_LENGTH <= len(new) <= MAX_LENGTH:
        raise AdultAuthError(400, f"Le code doit compter entre {MIN_LENGTH} et {MAX_LENGTH} caractères.")
    with _lock:
        etat = _etat()
        if etat == "illisible":
            raise AdultAuthError(503, ILLISIBLE_MSG)
        stored = _read() if etat == "ok" else None
        if stored is not None:
            _verify(_clean(current), stored, "Code actuel incorrect.")
        salt = secrets.token_bytes(16)
        _write({"algo": "pbkdf2_sha256", "iterations": ITERATIONS,
                "salt": salt.hex(), "hash": _digest(new, salt, ITERATIONS).hex()})
        _sessions.clear()


def retry_in() -> int:
    """Secondes avant le prochain essai autorisé (0 : essai possible)."""
    with _lock:
        return math.ceil(max(0.0, _blocked_until - _now()))


def unlock(code: str) -> str:
    """Vérifie le code et renvoie un jeton de session. Lève AdultAuthError sinon."""
    code = _clean(code)
    with _lock:
        etat = _etat()
        if etat == "absent":
            raise AdultAuthError(409, "Aucun code n'est défini.")
        if etat == "illisible":
            raise AdultAuthError(503, ILLISIBLE_MSG)
        _verify(code, _read(), "Code incorrect.")
        return create_session()


def _key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session() -> str:
    token = secrets.token_urlsafe(32)
    with _lock:
        _sessions[_key(token)] = _now() + SESSION_MAX_AGE
    return token


def session_is_valid(token: Optional[str]) -> bool:
    if not token or len(token) > 200:
        return False
    key = _key(token)
    with _lock:
        expires = _sessions.get(key)
        if expires is None:
            return False
        if expires < _now() or _etat() != "ok":
            _sessions.pop(key, None)
            return False
        return True


def revoke(token: Optional[str]) -> None:
    if token:
        with _lock:
            _sessions.pop(_key(token), None)


def reset() -> None:
    """Efface sessions et compteur d'essais (tests)."""
    global _failures, _blocked_until
    with _lock:
        _sessions.clear()
        _failures = 0
        _blocked_until = 0.0
