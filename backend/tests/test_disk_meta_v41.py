"""Fiches des disques (version 41) : meta_running ne reste levé que s'il reste du travail."""
import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import config, disk_meta, disks, main


def _disque(env, monkeypatch, fiche):
    """Un disque connecté avec un film ; `fiche` est la fiche déjà calculée (ou None)."""
    root = env / "disque"
    dossier = root / "MEDIA" / "FILM"
    dossier.mkdir(parents=True)
    fichier = dossier / "Film-A.mkv"
    fichier.write_bytes(b"x")
    path = str(fichier)
    monkeypatch.setattr(disks, "list_destination_disks", lambda: [{"path": str(root), "free_bytes": 10}])
    item = {"name": "Film-A.mkv", "path": path, "folder": "MEDIA/FILM", "is_dir": False, "size": 1, "mtime": 0}
    entry = {"path": str(root), "label": "disque", "connected": True, "scanned_at": time.time(),
             "items": {"films": [item], "series": [], "ebooks": []}, "meta": {}}
    if fiche is not None:
        entry["meta"][path] = fiche
    (env / "disk-library.json").write_text(json.dumps({str(root): entry}), encoding="utf-8")
    monkeypatch.setitem(disk_meta._state, "running", False)
    return path


@pytest.fixture
def appels(monkeypatch):
    """Remplace la récupération TMDB : on compte les appels, aucun réseau."""
    calls = []

    async def fake(kind, item):
        calls.append(item["name"])
        return {"title": "Film A", "tmdb_id": 1}

    monkeypatch.setattr(disk_meta, "_fetch", fake)
    return calls


def test_sans_fiche_a_calculer_le_calcul_ne_se_lance_pas(env, monkeypatch, appels):
    _disque(env, monkeypatch, {"title": "Film A", "tmdb_id": 1, "checked_at": time.time()})
    assert asyncio.run(disk_meta.kick()) is False
    assert asyncio.run(disk_meta.kick()) is False
    assert appels == []
    assert disk_meta.running() is False


def test_avec_une_fiche_a_calculer_le_calcul_tourne_puis_s_arrete(env, monkeypatch, appels):
    _disque(env, monkeypatch, None)

    async def scenario():
        first = await disk_meta.kick()
        for _ in range(200):  # le calcul s'exécute pendant ces attentes
            if not disk_meta.running():
                break
            await asyncio.sleep(0.01)
        second = await disk_meta.kick()
        return first, second

    first, second = asyncio.run(scenario())
    assert first is True, "un élément sans fiche lance le calcul et le signale"
    assert second is False, "une fois la fiche écrite, le drapeau retombe"
    assert appels == ["Film-A.mkv"]


def test_la_route_ne_signale_pas_de_calcul_quand_tout_est_a_jour(env, monkeypatch, appels):
    _disque(env, monkeypatch, {"title": "Film A", "tmdb_id": 1, "checked_at": time.time()})
    monkeypatch.setattr(main, "log_action", lambda *a, **k: None)
    client = TestClient(main.app)
    reponse = client.get("/api/disk-library")
    assert reponse.status_code == 200
    assert reponse.json()["meta_running"] is False
    assert appels == []


def test_tmdb_desactive_pas_de_calcul_pour_les_films(env, monkeypatch, appels):
    _disque(env, monkeypatch, None)
    monkeypatch.setattr(type(config.settings), "tmdb_enabled", property(lambda self: False))
    assert asyncio.run(disk_meta.kick()) is False
    assert appels == []
