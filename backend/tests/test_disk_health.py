"""Santé SMART des disques (version 33) : validation des rapports, niveaux, rattachement aux disques branchés."""
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import disk_health as dh
from app import disks

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
CLEAN = {"supported": True, "passed": True, "temperature_c": 38, "power_on_hours": 1200,
         "reallocated": 0, "pending": 0, "uncorrectable": 0}


def _report(mount="/media/usb1", checked=NOW, smart=None, **extra):
    report = {
        "schema": 1,
        "mount": mount,
        "device": "/dev/sdb",
        "model": "WD Elements",
        "checked_at": checked.isoformat(timespec="seconds"),
        "error": None,
        "smart": dict(CLEAN if smart is None else smart),
    }
    report.update(extra)
    return report


@pytest.fixture
def health(tmp_path, monkeypatch):
    folder = tmp_path / "health"
    folder.mkdir()
    monkeypatch.setenv("DISK_HEALTH_DIR", str(folder))
    monkeypatch.setattr(disks, "list_destination_disks", lambda: [{"path": "/media/usb1"}])
    return folder


def _write(folder, name, data):
    (folder / name).write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def _status():
    return dh.status(now=NOW)


def test_rapport_sain_est_ok_et_rattache(health):
    _write(health, "usb1.json", _report())
    result = _status()
    disk = result["disks"][0]
    assert disk["status"] == "ok" and disk["reasons"] == []
    assert disk["attached"] is True and disk["stale"] is False
    assert result["worst"] == "ok"
    assert "model" in disk and "serial" not in disk  # pas de numéro de série dans l'API


@pytest.mark.parametrize("smart, level", [
    ({**CLEAN, "pending": 2}, "critique"),
    ({**CLEAN, "uncorrectable": 1}, "critique"),
    ({**CLEAN, "passed": False}, "critique"),
    ({**CLEAN, "temperature_c": 70}, "critique"),
    ({**CLEAN, "reallocated": 3}, "attention"),
    ({**CLEAN, "temperature_c": 58}, "attention"),
    ({**CLEAN, "passed": None}, "ok"),
    ({"supported": False, "passed": None, "temperature_c": None, "power_on_hours": None,
      "reallocated": None, "pending": None, "uncorrectable": None}, "inconnu"),
])
def test_niveaux_selon_les_compteurs(health, smart, level):
    _write(health, "usb1.json", _report(smart=smart))
    assert _status()["disks"][0]["status"] == level


def test_critique_garde_aussi_les_avertissements(health):
    _write(health, "usb1.json", _report(smart={**CLEAN, "pending": 1, "reallocated": 4}))
    disk = _status()["disks"][0]
    assert disk["status"] == "critique"
    assert any("réallocation" in reason for reason in disk["reasons"])
    assert any("réalloué" in reason for reason in disk["reasons"])


def test_un_rapport_invalide_ne_bloque_pas_les_autres(health):
    bad_type = _report(smart={**CLEAN, "temperature_c": "45"})
    bad_bool = _report(smart={**CLEAN, "pending": True})
    bad_device = _report(device="/tmp/pas-un-disque")
    _write(health, "a.json", bad_type)
    _write(health, "b.json", bad_bool)
    _write(health, "c.json", bad_device)
    _write(health, "d.json", "{ce n'est pas du JSON")
    _write(health, "e.json", _report(mount="/mnt/ok"))
    result = _status()
    assert [d["mount"] for d in result["disks"]] == ["/mnt/ok"]
    assert len(result["errors"]) == 4
    messages = " | ".join(e["error"] for e in result["errors"])
    assert "temperature_c" in messages and "pending" in messages and "/dev/" in messages


def test_schema_inconnu_et_fuseau_manquant_refuses():
    with pytest.raises(dh.ReportError, match="schéma"):
        dh.validate({**_report(), "schema": 2})
    naive = _report()
    naive["checked_at"] = "2026-10-12T10:00:00"
    with pytest.raises(dh.ReportError, match="fuseau horaire"):
        dh.validate(naive)


def test_champs_inconnus_ignores_sans_erreur(health):
    _write(health, "usb1.json", _report(serial="SECRET-123", extra_field=True))
    result = _status()
    assert result["errors"] == []
    assert "SECRET-123" not in json.dumps(result)


def test_rapport_perime_et_disque_debranche_apres_les_branches(health):
    _write(health, "ancien.json", _report(mount="/media/ancien", checked=NOW - timedelta(days=3),
                                          smart={**CLEAN, "pending": 5}))
    _write(health, "usb1.json", _report(smart={**CLEAN, "reallocated": 1}))
    result = _status()
    assert [d["mount"] for d in result["disks"]] == ["/media/usb1", "/media/ancien"]
    old = result["disks"][1]
    assert old["attached"] is False and old["stale"] is True and old["age_hours"] == 72.0
    assert old["status"] == "critique"
    # le pire niveau ne regarde que les disques branchés
    assert result["worst"] == "attention"


def test_fichiers_caches_et_temporaires_ignores(health):
    _write(health, ".health-123.tmp", "{en cours")
    _write(health, "notes.txt", "pas un rapport")
    result = _status()
    assert result["disks"] == [] and result["errors"] == []


def test_rapport_trop_volumineux_refuse(health):
    _write(health, "gros.json", "x" * (dh.MAX_REPORT_BYTES + 1))
    assert "trop volumineux" in _status()["errors"][0]["error"]


def test_dossier_absent_n_est_pas_une_erreur(tmp_path, monkeypatch):
    monkeypatch.setenv("DISK_HEALTH_DIR", str(tmp_path / "absent"))
    monkeypatch.setattr(disks, "list_destination_disks", lambda: [])
    result = _status()
    assert result == {"present": False, "worst": None, "disks": [], "errors": []}


def test_api_disques_health(health):
    from fastapi.testclient import TestClient

    from app.main import app

    _write(health, "usb1.json", _report())
    response = TestClient(app).get("/api/disks/health")
    assert response.status_code == 200
    assert response.json()["disks"][0]["mount"] == "/media/usb1"


def test_rapport_copie_non_modifiee_par_validation():
    raw = _report()
    original = copy.deepcopy(raw)
    dh.validate(raw)
    assert raw == original
