"""Planification des transferts : plages horaires, vitesse, file d'attente par priorité.

Règles, réglables dans Réglages › Plages de transfert et vitesse :

1. Plages. Un envoi ou une restauration 1fichier ne démarre un élément que pendant
   une plage activée. Une copie disque aussi. Hors plage, la file attend.
   - Un envoi 1fichier déjà commencé va jusqu'au bout : il ne se reprend pas.
   - Une copie disque déjà commencée est SUSPENDUE hors plage (SIGSTOP) puis reprise
     à la plage suivante. C'est possible sans risque : rien ne transite par le réseau.

2. Vitesse. Chaque plage fixe une vitesse pour 1fichier et une pour les copies disque
   (Mo/s, 0 = illimité). Un envoi 1fichier en cours suit la plage active ; hors plage,
   il garde la dernière vitesse connue. Une copie disque en cours est relancée quand la
   vitesse change : rsync ne sait pas changer sa limite en cours de route, et --partial
   reprend là où il s'était arrêté.

3. Emplacements. Un seul envoi ou une seule restauration 1fichier à la fois. Une seule
   copie PAR DISQUE : deux disques différents peuvent être copiés en même temps.
   Un emplacement libéré va à la tâche la plus prioritaire (urgent, normal, bas), puis
   à la plus ancienne. La priorité peut être changée pendant l'attente ; elle est relue
   à chaque élément.

Les heures sont celles du serveur : régler la variable TZ (ex. Europe/Paris).
"""
import asyncio
import itertools
import os
import signal
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Callable, Optional

from . import config

PRIORITIES = ("urgent", "normal", "bas")
PRIORITY_LABELS = {"urgent": "urgent", "normal": "normal", "bas": "basse"}
DEFAULT_PRIORITY = "normal"
_RANK = {p: i for i, p in enumerate(PRIORITIES)}

# Types de transfert : chacun a sa vitesse dans les plages (voir config.TRANSFER_KINDS).
KINDS = tuple(config.TRANSFER_KINDS)
_RATE_KEY = {kind: f"{kind}_mbps" for kind in KINDS}
MIB = 1024 * 1024
RECHECK_SECONDS = 60      # délai maximal entre deux vérifications d'une plage
PAUSE_TICK_SECONDS = 1.0  # délai entre deux surveillances d'une copie disque en cours

# Dernière vitesse connue par type (Mo/s) : sert hors plage, à un envoi déjà commencé.
_last_rate: dict[str, float] = {k: 0.0 for k in KINDS}

# Remplaçables dans les tests.
_now: Callable[[], datetime] = datetime.now
_sleep = asyncio.sleep


# ---------------------------------------------------------------------
# Plages
# ---------------------------------------------------------------------

def _span(window: dict) -> tuple[int, int]:
    return config.parse_hhmm(window["start"]), config.parse_hhmm(window["end"], allow_24=True)


def _contains(start: int, end: int, minute: int) -> bool:
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end  # plage à cheval sur minuit (ex. 22:00 -> 06:00)


def _enabled_windows() -> list[dict]:
    return [w for w in config.settings.transfer_windows if w.get("enabled")]


def active_window(now: Optional[datetime] = None) -> Optional[dict]:
    """Première plage activée qui contient l'heure donnée (ou None : hors plage)."""
    now = now or _now()
    minute = now.hour * 60 + now.minute
    for window in _enabled_windows():
        start, end = _span(window)
        if _contains(start, end, minute):
            return window
    return None


def next_window(now: Optional[datetime] = None) -> Optional[tuple[float, dict]]:
    """(secondes avant l'ouverture, plage) de la prochaine plage activée, ou None si aucune."""
    now = now or _now()
    best: Optional[tuple[float, dict]] = None
    for window in _enabled_windows():
        start, _end = _span(window)
        opening = now.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
        if opening <= now:
            opening += timedelta(days=1)
        delta = (opening - now).total_seconds()
        if best is None or delta < best[0]:
            best = (delta, window)
    return best


def rate_mbps(kind: str, now: Optional[datetime] = None) -> float:
    """Vitesse courante en Mo/s pour le type de transfert (0 = illimité)."""
    window = active_window(now)
    if window is not None:
        _last_rate[kind] = float(window[_RATE_KEY[kind]])
    return _last_rate[kind]


class Throttle:
    """Limite le débit d'un flux. `wait_for(n)` s'appelle après chaque bloc lu ou
    écrit (dans le thread du transfert) et endort le temps nécessaire.

    Pas de « crédit » accumulé : une pause longue ne permet pas de rattraper en
    rafale ensuite, le débit moyen reste celui de la vitesse réglée."""

    def __init__(self, kind: str, *, rate_fn: Optional[Callable[[], float]] = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self._rate_fn = rate_fn or (lambda: rate_mbps(kind) * MIB)
        self._clock = clock
        self._sleep = sleep
        self._due: Optional[float] = None

    def wait_for(self, nbytes: int) -> float:
        rate = self._rate_fn()
        if rate <= 0 or nbytes <= 0:
            self._due = None
            return 0.0
        now = self._clock()
        if self._due is None or self._due < now:
            self._due = now
        self._due += nbytes / rate
        delay = self._due - now
        if delay > 0:
            self._sleep(delay)
            return delay
        return 0.0


# ---------------------------------------------------------------------
# Emplacements : une file par (type, disque)
# ---------------------------------------------------------------------

class _Waiter:
    __slots__ = ("job", "future", "seq", "shown")

    def __init__(self, job, future, seq: int):
        self.job = job
        self.future = future
        self.seq = seq
        self.shown: Optional[tuple[int, str]] = None  # (position, priorité) déjà annoncées


class _Slot:
    def __init__(self, key: str, kind: str, label: str, capacity: int = 1):
        self.key = key
        self.kind = kind
        self.label = label
        self.capacity = capacity
        self.holders: set[str] = set()   # identifiants des tâches qui ont l'emplacement
        self.waiters: list[_Waiter] = []


_slots: dict[str, _Slot] = {}
_seq = itertools.count()


def _slot_key(kind: str, target: Optional[str]) -> str:
    return f"{kind}:{target}" if target else kind


def _get_slot(kind: str, target: Optional[str]) -> _Slot:
    key = _slot_key(kind, target)
    if key not in _slots:
        label = config.TRANSFER_KINDS.get(kind, kind)
        if target:  # une copie par disque : le libellé donne le nom du disque
            label = f"{label} {os.path.basename(target.rstrip('/')) or target}"
        _slots[key] = _Slot(key, kind, label)
    return _slots[key]


def _priority_of(job) -> str:
    return job.priority if getattr(job, "priority", None) in _RANK else DEFAULT_PRIORITY


def _sorted_waiters(slot: _Slot) -> list[_Waiter]:
    return sorted(slot.waiters, key=lambda w: (_RANK[_priority_of(w.job)], w.seq))


def _announce(slot: _Slot) -> None:
    """Prévient chaque tâche en attente de sa position (seulement si elle a changé)."""
    for position, waiter in enumerate(_sorted_waiters(slot), start=1):
        shown = (position, _priority_of(waiter.job))
        if waiter.shown == shown:
            continue
        waiter.shown = shown
        waiter.job.emit({
            "type": "job_queued",
            "resource": slot.kind,
            "label": slot.label,
            "position": position,
            "waiting": len(slot.waiters),
            "priority": shown[1],
        })


def _dispatch(slot: _Slot) -> None:
    while len(slot.holders) < slot.capacity and slot.waiters:
        waiter = _sorted_waiters(slot)[0]
        slot.waiters.remove(waiter)
        if waiter.future.done():  # annulée entre-temps
            continue
        slot.holders.add(waiter.job.id)
        waiter.future.set_result(None)
    _announce(slot)


async def acquire(job, kind: str, target: Optional[str] = None) -> None:
    slot_ = _get_slot(kind, target)
    if len(slot_.holders) < slot_.capacity and not slot_.waiters:
        slot_.holders.add(job.id)
        return
    waiter = _Waiter(job, asyncio.get_running_loop().create_future(), next(_seq))
    slot_.waiters.append(waiter)
    _announce(slot_)
    try:
        await waiter.future
    except BaseException:
        if waiter.future.done() and not waiter.future.cancelled():
            release(job, kind, target)  # emplacement attribué juste avant l'annulation : rendu
        elif waiter in slot_.waiters:
            slot_.waiters.remove(waiter)
        raise


def release(job, kind: str, target: Optional[str] = None) -> None:
    slot_ = _get_slot(kind, target)
    slot_.holders.discard(job.id)
    _dispatch(slot_)


@asynccontextmanager
async def slot(job, kind: str, target: Optional[str] = None):
    """Tient l'emplacement du type (et du disque) le temps d'un élément."""
    await acquire(job, kind, target)
    try:
        yield
    finally:
        release(job, kind, target)


def set_priority(job, priority: str) -> None:
    if priority not in _RANK:
        raise ValueError(f"priorité inconnue : {priority!r}")
    job.priority = priority
    job.emit({"type": "job_priority", "priority": priority})
    for slot_ in _slots.values():
        _announce(slot_)


def queues_snapshot(hidden: frozenset = frozenset()) -> dict:
    """Occupation des emplacements. Les tâches `hidden` n'apparaissent pas, ni dans les
    compteurs ni dans les positions : une file visible ne révèle pas les tâches cachées."""
    out = {}
    for key, slot_ in _slots.items():
        running = [h for h in slot_.holders if h not in hidden]
        waiting = [w for w in _sorted_waiters(slot_) if w.job.id not in hidden]
        if not running and not waiting:
            continue
        out[key] = {
            "label": slot_.label,
            "running": len(running),
            "waiting": [
                {"job_id": w.job.id, "position": pos, "priority": _priority_of(w.job)}
                for pos, w in enumerate(waiting, start=1)
            ],
        }
    return out


# ---------------------------------------------------------------------
# Attente de plage (à appeler avant chaque élément, emplacement tenu)
# ---------------------------------------------------------------------

def _wait_message() -> str:
    upcoming = next_window()
    if upcoming is None:
        return "Aucune plage de transfert activée : transferts en pause (Réglages › Plages de transfert)."
    seconds, window = upcoming
    opening = _now() + timedelta(seconds=seconds)
    return f"Hors plage : reprise à {opening:%H:%M} (plage « {window['name']} »)."


async def wait_for_window(job) -> None:
    """Attend une plage activée avant de démarrer un élément. Sans effet en simulation."""
    if config.settings.dry_run or active_window() is not None:
        return
    job.emit({"type": "job_wait", "message": _wait_message()})
    while active_window() is None:
        upcoming = next_window()
        delay = upcoming[0] if upcoming else RECHECK_SECONDS
        await _sleep(min(max(delay, 1.0), RECHECK_SECONDS))
    job.emit({"type": "job_resume"})


def _signal(proc, sig: int) -> None:
    try:
        proc.send_signal(sig)
    except ProcessLookupError:  # déjà terminé
        pass


async def control_process(job, proc, kind: str, started_rate: float) -> str:
    """Surveille une copie disque en cours (rsync) tant qu'elle tourne.

    - Hors plage : le processus est suspendu (SIGSTOP), l'événement job_wait l'annonce.
    - Plage suivante : il reprend (SIGCONT), sauf si la vitesse a changé : il est alors
      arrêté (SIGTERM) et "restart" est renvoyé pour qu'il soit relancé avec la nouvelle limite.
    - Processus terminé : "done".
    """
    paused = False
    while proc.returncode is None:
        if active_window() is None:
            if not paused:
                paused = True
                _signal(proc, signal.SIGSTOP)
                job.emit({"type": "job_wait", "message": f"Copie suspendue. {_wait_message()}"})
        else:
            if paused:
                paused = False
                _signal(proc, signal.SIGCONT)
                job.emit({"type": "job_resume"})
            if rate_mbps(kind) != started_rate:
                _signal(proc, signal.SIGTERM)
                return "restart"
        await _sleep(PAUSE_TICK_SECONDS)
    return "done"


# ---------------------------------------------------------------------
# État pour l'interface
# ---------------------------------------------------------------------

def status(hidden: frozenset = frozenset()) -> dict:
    """État public de la file. `hidden` : tâches à taire (films adultes, session verrouillée)."""
    now = _now()
    window = active_window(now)
    upcoming = next_window(now)
    return {
        "now": now.isoformat(timespec="minutes"),
        "active": None if window is None else {
            "name": window["name"], "end": window["end"],
            "onefichier_mbps": window["onefichier_mbps"], "disk_mbps": window["disk_mbps"],
        },
        "paused": window is None,
        "next": None if upcoming is None else {
            "name": upcoming[1]["name"],
            "start": upcoming[1]["start"],
            "in_seconds": int(upcoming[0]),
        },
        "rates": {k: rate_mbps(k, now) for k in KINDS},
        "queues": queues_snapshot(hidden),
    }
