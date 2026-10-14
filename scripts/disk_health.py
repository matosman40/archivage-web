#!/usr/bin/env python3
"""Rapport SMART des disques de sauvegarde, à lancer SUR L'HÔTE (version 33).

Pour chaque disque monté sous les racines de montage (mêmes règles que l'application), écrit un
fichier JSON dans le dossier --out. Ce dossier est monté en lecture seule dans le conteneur
(variable HOST_HEALTH_DIR de docker-compose.yml). Le conteneur ne reçoit donc jamais d'accès à /dev.

Prérequis sur l'hôte : python3, smartmontools (smartctl >= 7.0, option -j), util-linux (lsblk),
et de préférence root (sudo) : la lecture SMART d'un disque exige les droits root.

Usage :
    sudo python3 scripts/disk_health.py --out /var/lib/archivage-health
    sudo python3 scripts/disk_health.py --out /var/lib/archivage-health --dry-run   # affiche sans écrire

Planification (exemple, chaque jour à 02:30) : un service systemd oneshot appelant la commande
ci-dessus, déclenché par un timer. Un disque débranché garde son dernier rapport : l'application
l'affiche comme « non branché » avec la date de ce contrôle.

Ce script n'utilise que la bibliothèque standard. Il ne modifie jamais un disque : smartctl ne
fait que LIRE les compteurs (aucun autotest n'est lancé).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from typing import Optional

DEFAULT_ROOTS = "/media,/mnt,/run/media"
MAX_DEPTH_UNDER_ROOT = 2  # identique à disks.py de l'application
SCHEMA = 1
ATA_REALLOCATED, ATA_PENDING, ATA_UNCORRECTABLE = 5, 197, 198
SMARTCTL_TIMEOUT = 180
LSBLK_TIMEOUT = 30


class SmartUnavailable(Exception):
    pass


def _sub(data, key) -> dict:
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, dict) else {}


def _nat(value) -> Optional[int]:
    """Entier positif ou nul, sinon None (les booléens ne comptent pas comme des entiers)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _ata_raw(data: dict, attr_id: int) -> Optional[int]:
    for attr in _sub(data, "ata_smart_attributes").get("table") or []:
        if isinstance(attr, dict) and attr.get("id") == attr_id:
            return _nat(_sub(attr, "raw").get("value"))
    return None


def _smartctl_message(data: dict) -> str:
    for item in _sub(data, "smartctl").get("messages") or []:
        if isinstance(item, dict) and isinstance(item.get("string"), str) and item["string"].strip():
            return item["string"].strip()[:200]
    return "SMART non disponible sur ce boîtier"


def parse_smartctl(data) -> dict:
    """Extrait les champs utiles de la sortie JSON de `smartctl -j -a`. Fonction pure."""
    if not isinstance(data, dict):
        return {"supported": False, "error": "réponse smartctl inattendue"}
    nvme = _sub(data, "nvme_smart_health_information_log")
    has_smart = "smart_status" in data or "ata_smart_attributes" in data or bool(nvme)
    if not has_smart:
        return {"supported": False, "error": _smartctl_message(data)}
    passed = _sub(data, "smart_status").get("passed")
    temperature = _sub(data, "temperature").get("current")
    uncorrectable = _ata_raw(data, ATA_UNCORRECTABLE)
    if uncorrectable is None and nvme:  # NVMe : erreurs de support non corrigées
        uncorrectable = _nat(nvme.get("media_errors"))
    model = data.get("model_name")
    return {
        "supported": True,
        "model": model.strip()[:80] if isinstance(model, str) and model.strip() else None,
        "passed": passed if isinstance(passed, bool) else None,
        "temperature_c": temperature if isinstance(temperature, int) and not isinstance(temperature, bool) else None,
        "power_on_hours": _nat(_sub(data, "power_on_time").get("hours")),
        "reallocated": _ata_raw(data, ATA_REALLOCATED),
        "pending": _ata_raw(data, ATA_PENDING),
        "uncorrectable": uncorrectable,
    }


def _smartctl(device: str, extra: tuple = ()) -> dict:
    try:
        proc = subprocess.run(["smartctl", "-j", "-a", *extra, device],
                              capture_output=True, text=True, timeout=SMARTCTL_TIMEOUT)
    except FileNotFoundError:
        raise SmartUnavailable("smartctl absent : installer le paquet smartmontools")
    except subprocess.TimeoutExpired:
        raise SmartUnavailable("smartctl : délai dépassé")
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise SmartUnavailable(f"sortie smartctl illisible (code {proc.returncode})")


def read_smart(device: str) -> dict:
    """Lit le SMART d'un périphérique. Un boîtier USB ne le donne souvent qu'avec le pilote SAT."""
    try:
        info = parse_smartctl(_smartctl(device))
        if not info["supported"]:
            info = parse_smartctl(_smartctl(device, ("-d", "sat")))
    except SmartUnavailable as exc:
        return {"supported": False, "error": str(exc)}
    return info


def unescape_mount(text: str) -> str:
    """/proc/mounts écrit les espaces et tabulations en octal (\\040)."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


def mounts_under(roots: list[str], proc_mounts: str) -> list[tuple[str, str]]:
    """Couples (périphérique, point de montage) des disques sous les racines, sans doublon."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in proc_mounts.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        source, mount = unescape_mount(parts[0]), unescape_mount(parts[1])
        if not source.startswith("/dev/") or mount in seen:
            continue
        for root in roots:
            root = root.rstrip("/")
            if not root or mount == root or not mount.startswith(root + "/"):
                continue
            rel = mount[len(root) + 1:].strip("/")
            depth = len(rel.split("/")) if rel else 0
            if 1 <= depth <= MAX_DEPTH_UNDER_ROOT:
                seen.add(mount)
                found.append((source, mount))
            break
    return found


def resolve_device(source: str) -> tuple[Optional[str], str]:
    """Disque physique d'une partition (smartctl ne lit que le disque entier). (chemin, erreur)."""
    try:
        proc = subprocess.run(["lsblk", "-no", "PKNAME", source],
                              capture_output=True, text=True, timeout=LSBLK_TIMEOUT)
    except FileNotFoundError:
        return None, "lsblk absent : installer util-linux"
    except subprocess.TimeoutExpired:
        return None, "lsblk : délai dépassé"
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        return source, ""  # pas de parent : la source est déjà le disque entier
    return f"/dev/{lines[0]}", ""


def build_report(mount: str, source: str, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now().astimezone()
    device, error = resolve_device(source)
    if device is None:
        smart = {"supported": False, "error": error}
    else:
        smart = read_smart(device)
    report_error = smart.pop("error", None) if not smart.get("supported") else None
    model = smart.pop("model", None)
    return {
        "schema": SCHEMA,
        "mount": mount,
        "device": device,
        "model": model,
        "checked_at": now.isoformat(timespec="seconds"),
        "error": report_error,
        "smart": smart,
    }


def report_name(mount: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", mount.strip("/")).strip("_") or "racine"
    return f"disque-{slug}.json"


def write_report(out_dir: str, name: str, report: dict) -> str:
    """Écriture atomique : le conteneur ne lit jamais un fichier à moitié écrit."""
    os.makedirs(out_dir, mode=0o755, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".health-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o644)
    final = os.path.join(out_dir, name)
    os.replace(tmp, final)
    return final


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Rapport SMART des disques de sauvegarde (à lancer sur l'hôte).")
    parser.add_argument("--out", required=True, help="dossier des rapports (monté en lecture seule dans le conteneur)")
    parser.add_argument("--roots", default=DEFAULT_ROOTS, help="racines de montage, séparées par des virgules")
    parser.add_argument("--dry-run", action="store_true", help="affiche les rapports sans rien écrire")
    args = parser.parse_args(argv)

    if os.geteuid() != 0:
        print("attention : lancer en root (sudo) pour lire le SMART des disques", file=sys.stderr)
    roots = [r.strip() for r in args.roots.split(",") if r.strip()]
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        print(f"impossible de lire /proc/mounts : {exc}", file=sys.stderr)
        return 1

    found = mounts_under(roots, text)
    if not found:
        print(f"aucun disque monté sous {', '.join(roots)}")
        return 0
    for source, mount in found:
        report = build_report(mount, source)
        if args.dry_run:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            path = write_report(args.out, report_name(mount), report)
            state = "SMART ok" if report["smart"].get("supported") else f"sans SMART ({report['error']})"
            print(f"{mount} : {state} -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
