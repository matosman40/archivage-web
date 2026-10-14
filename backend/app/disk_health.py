"""Santé matérielle des disques de sauvegarde (version 33).

Complète disk_integrity (ce qu'il y a DANS les fichiers) par l'état du support lui-même (SMART).

- Un script tourne SUR L'HÔTE (scripts/disk_health.py, en root, avec smartmontools) et écrit un JSON
  par disque monté, dans un dossier de l'hôte.
- Ce dossier est monté EN LECTURE SEULE dans le conteneur (DISK_HEALTH_DIR, /health par défaut).
  L'application ne touche jamais à /dev : elle lit, valide et rattache les rapports aux disques branchés.
- Le rapport d'un disque débranché reste consultable : c'est son « dernier contrôle connu ».
"""
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import disks

SCHEMA = 1
MAX_REPORT_BYTES = 64 * 1024
MAX_REPORTS = 200
MAX_ERRORS = 20
STALE_AFTER = timedelta(hours=36)  # le script de l'hôte tourne chaque jour
TEMP_WARN_C = 55
TEMP_CRIT_C = 65
COUNTER_MAX = 10**9

COUNTERS = ("reallocated", "pending", "uncorrectable")
SMART_FIELDS = ("passed", "temperature_c", "power_on_hours") + COUNTERS
LEVEL_ORDER = ("critique", "attention", "inconnu", "ok")  # du plus grave au plus sain


class ReportError(ValueError):
    pass


def health_dir() -> str:
    return os.environ.get("DISK_HEALTH_DIR", "/health")


def _int_or_none(value, label: str, minimum: int = 0, maximum: int = COUNTER_MAX) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReportError(f"{label} : nombre entier attendu")
    if not minimum <= value <= maximum:
        raise ReportError(f"{label} : valeur hors limites ({value})")
    return value


def _text(value, label: str, limit: int) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReportError(f"{label} : texte attendu")
    return value.strip()[:limit] or None


def _parse_time(value) -> datetime:
    if not isinstance(value, str):
        raise ReportError("checked_at : date ISO 8601 attendue")
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        raise ReportError("checked_at : date ISO 8601 invalide")
    if when.tzinfo is None:
        raise ReportError("checked_at : fuseau horaire manquant (ex. +02:00)")
    return when


def validate(raw) -> dict:
    """Contrôle strict d'un rapport. Lève ReportError avec un message lisible."""
    if not isinstance(raw, dict):
        raise ReportError("objet JSON attendu")
    if raw.get("schema") != SCHEMA:
        raise ReportError(f"schéma {raw.get('schema')!r} non géré (attendu {SCHEMA})")
    mount = _text(raw.get("mount"), "mount", 300)
    if not mount or not mount.startswith("/"):
        raise ReportError("mount : chemin absolu attendu")
    device = _text(raw.get("device"), "device", 64)
    if device and not device.startswith("/dev/"):
        raise ReportError("device : chemin /dev/... attendu")
    smart_raw = raw.get("smart")
    if not isinstance(smart_raw, dict):
        raise ReportError("smart : objet attendu")
    supported = smart_raw.get("supported")
    if not isinstance(supported, bool):
        raise ReportError("smart.supported : booléen attendu")

    smart: dict = {field: None for field in SMART_FIELDS}
    smart["supported"] = supported
    if supported:
        passed = smart_raw.get("passed")
        if passed is not None and not isinstance(passed, bool):
            raise ReportError("smart.passed : booléen attendu")
        smart["passed"] = passed
        smart["temperature_c"] = _int_or_none(smart_raw.get("temperature_c"), "smart.temperature_c", -40, 150)
        smart["power_on_hours"] = _int_or_none(smart_raw.get("power_on_hours"), "smart.power_on_hours")
        for name in COUNTERS:
            smart[name] = _int_or_none(smart_raw.get(name), f"smart.{name}")

    return {
        "schema": SCHEMA,
        "mount": mount,
        "device": device,
        "model": _text(raw.get("model"), "model", 80),
        "checked_at": _parse_time(raw.get("checked_at")),
        "error": _text(raw.get("error"), "error", 200),
        "smart": smart,
    }


def assess(smart: dict) -> tuple[str, list[str]]:
    """Niveau (critique, attention, inconnu, ok) et raisons en français."""
    if not smart.get("supported"):
        return "inconnu", ["SMART non disponible sur ce boîtier"]
    critical: list[str] = []
    warning: list[str] = []
    if smart.get("passed") is False:
        critical.append("autotest SMART en échec")
    if (smart.get("pending") or 0) > 0:
        critical.append(f"{smart['pending']} secteur(s) en attente de réallocation")
    if (smart.get("uncorrectable") or 0) > 0:
        critical.append(f"{smart['uncorrectable']} erreur(s) non corrigible(s)")
    temp = smart.get("temperature_c")
    if temp is not None and temp >= TEMP_CRIT_C:
        critical.append(f"température {temp} °C")
    elif temp is not None and temp >= TEMP_WARN_C:
        warning.append(f"température élevée : {temp} °C")
    if (smart.get("reallocated") or 0) > 0:
        warning.append(f"{smart['reallocated']} secteur(s) réalloué(s)")
    if critical:
        return "critique", critical + warning
    if warning:
        return "attention", warning
    return "ok", []


def read_reports(directory: str) -> tuple[list[dict], list[dict]]:
    """Rapports valides et erreurs. Un fichier invalide ne bloque pas les autres."""
    reports: list[dict] = []
    errors: list[dict] = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return reports, errors  # dossier absent : pas encore de rapport
    for name in names:
        if not name.endswith(".json") or name.startswith("."):
            continue
        if len(reports) >= MAX_REPORTS:
            break
        path = os.path.join(directory, name)
        try:
            if os.path.getsize(path) > MAX_REPORT_BYTES:
                raise ReportError("fichier trop volumineux")
            with open(path, "r", encoding="utf-8") as fh:
                reports.append(validate(json.load(fh)))
        except (OSError, ValueError) as exc:  # ReportError et erreurs JSON sont des ValueError
            errors.append({"file": name, "error": str(exc)[:200]})
    return reports, errors[:MAX_ERRORS]


def _describe(report: dict, attached: set, now: datetime) -> dict:
    level, reasons = assess(report["smart"])
    age = now - report["checked_at"]
    return {
        "mount": report["mount"],
        "device": report["device"],
        "model": report["model"],
        "attached": report["mount"] in attached,
        "checked_at": report["checked_at"].isoformat(timespec="seconds"),
        "age_hours": round(max(age.total_seconds(), 0) / 3600, 1),
        "stale": age > STALE_AFTER,
        "status": level,
        "reasons": reasons,
        "error": report["error"],
        "smart": report["smart"],
    }


def status(now: Optional[datetime] = None) -> dict:
    """État de tous les rapports. Les disques branchés d'abord, puis par point de montage."""
    now = now or datetime.now(timezone.utc)
    directory = health_dir()
    reports, errors = read_reports(directory)
    attached = {d["path"] for d in disks.list_destination_disks()}
    items = [_describe(r, attached, now) for r in reports]
    items.sort(key=lambda item: (not item["attached"], item["mount"]))
    plugged = [item["status"] for item in items if item["attached"]]
    worst = min(plugged, key=LEVEL_ORDER.index) if plugged else None
    return {
        "present": os.path.isdir(directory),
        "worst": worst,
        "disks": items,
        "errors": errors,
    }
