"""Version 46 : jeton d'accès, reprises visibles dans l'interface, avertissement persistant d'un envoi repris.

- ACCESS_TOKEN vide (valeur par défaut) : aucune protection, comportement inchangé.
- ACCESS_TOKEN renseigné : les modifications (POST, PUT, PATCH, DELETE sous /api/) exigent l'en-tête X-Access-Token ;
  la lecture reste ouverte, et le jeton n'est jamais renvoyé par /api/config.
- Une tâche reprise après un redémarrage est signalée comme telle dans la liste des tâches.
- Un envoi 1fichier repris depuis zéro garde son avertissement sur l'entrée (pas seulement dans le journal).
"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app import config, jobs, main, onefichier

from tests import test_v46_reprises as v46

# Fixture et aides de l'envoi simulé, partagées avec test_v46_reprises.py.
envoi = v46.envoi
_envoi = v46._envoi
_redemarrer = v46._redemarrer
Arret = v46.Arret

JETON = "jeton-de-test-0123456789"


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def jeton(monkeypatch):
    monkeypatch.setattr(config, "ACCESS_TOKEN", JETON)
    return JETON


# --- Jeton d'accès ---------------------------------------------------------------------------

def test_sans_jeton_configure_les_modifications_restent_libres(client, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_TOKEN", "")
    r = client.post("/api/jobs", json={})
    assert r.status_code == 422, "la validation répond : aucune barrière d'accès"
    assert client.get("/api/config").json()["access_protected"] is False


def test_modification_sans_jeton_est_refusee(client, jeton):
    r = client.post("/api/jobs", json={})
    assert r.status_code == 401
    assert r.json()["code"] == "token_required"


def test_mauvais_jeton_est_refuse(client, jeton):
    r = client.post("/api/jobs", json={}, headers={"X-Access-Token": "faux"})
    assert r.status_code == 401
    assert r.json()["code"] == "token_invalid"


def test_bon_jeton_laisse_passer(client, jeton):
    r = client.post("/api/jobs", json={}, headers={"X-Access-Token": JETON})
    assert r.status_code == 422  # la validation répond : l'accès est accordé


def test_lecture_toujours_ouverte_et_jeton_jamais_renvoye(client, jeton):
    r = client.get("/api/config")
    assert r.status_code == 200
    assert r.json()["access_protected"] is True
    assert JETON not in r.text


def test_suppression_et_put_sont_aussi_proteges(client, jeton):
    assert client.delete("/api/1fichier/entries/101").status_code == 401
    assert client.put("/api/settings", json={}).status_code == 401


# --- Tâches reprises ------------------------------------------------------------------------

def test_liste_des_taches_signale_une_reprise(pret_jobs):
    src = pret_jobs / "src" / "Film.mkv"
    src.parent.mkdir()
    src.write_bytes(b"x" * 10)
    jobs.create_job("copy", "films", [str(src)], destination=str(pret_jobs / "disque"))
    assert [j["resumed"] for j in jobs.list_jobs()] == [False]  # une tâche neuve n'est pas reprise

    _redemarrer()

    assert all(j["resumed"] is True for j in jobs.list_jobs())


@pytest.fixture
def pret_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_FILE", str(tmp_path / "log" / "archivage.log"))
    monkeypatch.setattr(config, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(config.settings, "dry_run", False)
    monkeypatch.setattr(jobs, "JOBS", {})
    monkeypatch.setattr(jobs, "_RESERVED", {})
    return tmp_path


# --- Avertissement persistant d'un envoi repris ----------------------------------------------

def test_avertissement_de_reprise_reste_sur_l_entree(envoi):
    job = _envoi(envoi)
    envoi["crash"] = "pendant_upload"
    with pytest.raises(Arret):
        asyncio.run(onefichier.run_archive(job))

    envoi["crash"] = None
    envoi["listing"] = [{"filename": "tmdb-101.7z", "size": 400, "url": "abc"}]  # partiel chez 1fichier
    restaures = _redemarrer()
    asyncio.run(onefichier.run_archive(restaures[0]))

    entree = onefichier._get_entry("101")
    assert entree["status"] == "done"
    assert "fichier partiel" in (entree.get("notice") or ""), "l'avertissement survit à la relance"
    assert "fichier partiel" in json.dumps(onefichier.list_entries())


def test_envoi_sans_reprise_n_a_pas_d_avertissement(envoi):
    job = _envoi(envoi)
    asyncio.run(onefichier.run_archive(job))
    assert onefichier._get_entry("101").get("notice") is None


def test_recherche_et_plans_restent_ouverts_meme_avec_jeton(client, jeton):
    # Envoyés en POST sans rien modifier : ne doivent pas exiger le jeton (sinon la recherche serait bloquée).
    for route in ("/api/search", "/api/plan/films", "/api/plan/ebooks", "/api/plan/series"):
        r = client.post(route, json={})
        assert r.status_code != 401, route


def test_export_de_l_index_reste_protege(client, jeton):
    r = client.post("/api/1fichier/index/export", json={"passphrase": "x" * 12})
    assert r.status_code == 401 and r.json()["code"] == "token_required"
