"""Script de l'hôte scripts/disk_health.py (version 33). Testé sans disque ni smartctl :
les appels système sont remplacés. Le contrat avec l'application est vérifié à la fin."""
import importlib.util
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app import disk_health as dh

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "disk_health.py"
spec = importlib.util.spec_from_file_location("disk_health_host", SCRIPT)
hostscript = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hostscript)

ATA_SAMPLE = {
    "model_name": "WDC WD40EFRX",
    "smart_status": {"passed": True},
    "temperature": {"current": 38},
    "power_on_time": {"hours": 1234},
    "ata_smart_attributes": {"table": [
        {"id": 5, "name": "Reallocated_Sector_Ct", "raw": {"value": 0}},
        {"id": 9, "name": "Power_On_Hours", "raw": {"value": 1234}},
        {"id": 197, "name": "Current_Pending_Sector", "raw": {"value": 0}},
        {"id": 198, "name": "Offline_Uncorrectable", "raw": {"value": 2}},
    ]},
}

PROC_MOUNTS = r"""/dev/sda1 / ext4 rw 0 0
/dev/sdb1 /media/usb1 exfat rw 0 0
/dev/sdc1 /media/devmon/Disque\040Film exfat rw 0 0
tmpfs /media/tmpfs tmpfs rw 0 0
/dev/sdd1 /media exfat rw 0 0
/dev/sde1 /mnt/a/b/c exfat rw 0 0
/dev/sdb1 /media/usb1 exfat rw 0 0
"""


class FakeRun:
    """Remplace subprocess.run : renvoie une sortie JSON selon les arguments reçus."""

    def __init__(self, outputs):
        self.outputs = outputs  # liste de (sortie, code), consommée dans l'ordre
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        stdout, code = self.outputs.pop(0)
        return subprocess.CompletedProcess(args, code, stdout=stdout, stderr="")


def test_parse_ata_donne_les_compteurs():
    info = hostscript.parse_smartctl(ATA_SAMPLE)
    assert info == {
        "supported": True, "model": "WDC WD40EFRX", "passed": True, "temperature_c": 38,
        "power_on_hours": 1234, "reallocated": 0, "pending": 0, "uncorrectable": 2,
    }


def test_parse_nvme_prend_les_erreurs_de_support():
    data = {"smart_status": {"passed": False}, "temperature": {"current": 41},
            "nvme_smart_health_information_log": {"media_errors": 3}}
    info = hostscript.parse_smartctl(data)
    assert info["uncorrectable"] == 3 and info["reallocated"] is None and info["passed"] is False


def test_parse_boitier_sans_smart_donne_le_message_smartctl():
    data = {"smartctl": {"messages": [{"string": "Unknown USB bridge [0x152d:0x0578]"}]}}
    info = hostscript.parse_smartctl(data)
    assert info["supported"] is False and info["error"].startswith("Unknown USB bridge")
    assert hostscript.parse_smartctl("n'importe quoi")["supported"] is False


def test_parse_ignore_les_valeurs_aberrantes():
    data = {"smart_status": {"passed": "oui"}, "temperature": {"current": True},
            "power_on_time": {"hours": -4}, "ata_smart_attributes": {"table": [
                {"id": 5, "raw": {"value": -1}}, {"id": 197, "raw": {"value": "2"}}]}}
    info = hostscript.parse_smartctl(data)
    assert info["passed"] is None and info["temperature_c"] is None
    assert info["power_on_hours"] is None and info["reallocated"] is None and info["pending"] is None


def test_montages_sous_les_racines_seulement():
    found = hostscript.mounts_under(["/media", "/mnt", "/run/media"], PROC_MOUNTS)
    assert found == [("/dev/sdb1", "/media/usb1"), ("/dev/sdc1", "/media/devmon/Disque Film")]


def test_nom_de_rapport_sans_caracteres_dangereux():
    assert hostscript.report_name("/media/devmon/Disque Film") == "disque-media_devmon_Disque_Film.json"
    assert hostscript.report_name("/") == "disque-racine.json"


def test_smartctl_absent_est_signale(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("smartctl")

    monkeypatch.setattr(hostscript.subprocess, "run", missing)
    info = hostscript.read_smart("/dev/sdb")
    assert info["supported"] is False and "smartmontools" in info["error"]


def test_boitier_usb_reessaie_avec_le_pilote_sat(monkeypatch):
    fake = FakeRun([
        (json.dumps({"smartctl": {"messages": [{"string": "Unknown USB bridge"}]}}), 1),
        (json.dumps(ATA_SAMPLE), 0),
    ])
    monkeypatch.setattr(hostscript.subprocess, "run", fake)
    info = hostscript.read_smart("/dev/sdb")
    assert info["supported"] is True and info["temperature_c"] == 38
    assert fake.calls[1][-3:] == ["-d", "sat", "/dev/sdb"]


def test_sortie_smartctl_illisible(monkeypatch):
    monkeypatch.setattr(hostscript.subprocess, "run", FakeRun([("pas du json", 2)]))
    info = hostscript.read_smart("/dev/sdb")
    assert info == {"supported": False, "error": "sortie smartctl illisible (code 2)"}


def test_rapport_complet_passe_la_validation_de_l_application(monkeypatch, tmp_path):
    monkeypatch.setattr(hostscript, "resolve_device", lambda source: ("/dev/sdb", ""))
    monkeypatch.setattr(hostscript.subprocess, "run", FakeRun([(json.dumps(ATA_SAMPLE), 0)]))
    now = datetime(2026, 10, 12, 2, 30, tzinfo=timezone.utc)
    report = hostscript.build_report("/media/usb1", "/dev/sdb1", now=now)
    assert "model" not in report["smart"] and "error" not in report["smart"]
    path = hostscript.write_report(str(tmp_path), hostscript.report_name("/media/usb1"), report)
    assert oct(os.stat(path).st_mode & 0o777) == "0o644"
    assert [name for name in os.listdir(tmp_path) if name.endswith(".tmp")] == []
    checked = dh.validate(json.loads(Path(path).read_text(encoding="utf-8")))
    assert checked["device"] == "/dev/sdb" and checked["model"] == "WDC WD40EFRX"
    assert dh.assess(checked["smart"])[0] == "critique"  # 2 erreurs non corrigibles
    assert checked["checked_at"] == now


def test_lsblk_absent_donne_un_rapport_valide_avec_erreur(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("lsblk")

    monkeypatch.setattr(hostscript.subprocess, "run", missing)
    report = hostscript.build_report("/media/usb1", "/dev/sdb1")
    checked = dh.validate(report)
    assert checked["smart"]["supported"] is False
    assert checked["error"] == "lsblk absent : installer util-linux"


def test_sans_parent_la_source_est_le_disque(monkeypatch):
    monkeypatch.setattr(hostscript.subprocess, "run", FakeRun([("", 0)]))
    assert hostscript.resolve_device("/dev/sdb") == ("/dev/sdb", "")
