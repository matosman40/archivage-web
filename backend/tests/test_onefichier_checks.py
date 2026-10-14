"""Contrôles planifiés : liens, test de restauration, alertes."""
import asyncio
import os
import shutil
import subprocess
import time

import pytest

from app import onefichier as of
from app import scheduler

needs_7z = pytest.mark.skipif(shutil.which("7z") is None, reason="7z absent")


@needs_7z
def test_controle_liens_test_restauration_et_alertes(env, tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "active_window", lambda now=None: {"name": "test", "onefichier_mbps": 0, "disk_mbps": 0})  # fenêtre toujours ouverte : le test ne dépend pas de l'heure
    alerts = []

    async def fake_send(title, message):
        alerts.append((title, message))

    from app import notify
    monkeypatch.setattr(notify, "send", fake_send)
    monkeypatch.setattr(of, "CHECK_PAUSE", 0)

    orig = tmp_path / "orig" / "Film"; orig.mkdir(parents=True)
    (orig / "f.mkv").write_bytes(os.urandom(120000))
    archive = tmp_path / "remote" / "tmdb-101.7z"; archive.parent.mkdir()
    subprocess.run(["7z", "a", "-t7z", "-y", "-bd", "-mhe=on", "-mx=0", "-pPW101-XXXXXXXXXX", str(archive), "Film"],
                   cwd=tmp_path / "orig", check=True, capture_output=True)
    sha = of._sha256_file(str(archive))
    size = os.path.getsize(archive)
    of._update(lambda d: d["entries"].update({
        "101": {"kind": "film", "status": "done", "url": "https://1fichier.com/?a101", "password": "PW101-XXXXXXXXXX",
                "archive_name": "tmdb-101.7z", "folder": "tmdb-101", "folder_id": 1, "remote_size": size, "size": size, "sha256": sha},
        "102": {"kind": "film", "status": "done", "url": "https://1fichier.com/?a102", "password": "PW102-XXXXXXXXXX",
                "archive_name": "tmdb-102.7z", "folder": "tmdb-102", "folder_id": 2, "remote_size": 1000, "size": 1000},
        "103": {"kind": "film", "status": "done", "url": "https://1fichier.com/?a103", "password": "PW103-XXXXXXXXXX",
                "archive_name": "tmdb-103.7z", "folder": "tmdb-103", "folder_id": 3, "remote_size": 25 * 10**9, "size": 25 * 10**9},
    }))

    async def fake_call(client, method, path, payload=None):
        if path == "/file/ls.cgi":
            fid = payload["folder_id"]
            if fid == 1:
                return {"status": "OK", "files": [{"filename": "tmdb-101.7z", "size": size, "date": "2026-10-01 10:00:00"}]}
            if fid == 2:
                return {"status": "OK", "files": [{"filename": "tmdb-102.7z", "size": 1000}]}
            return {"status": "OK", "files": []}
        if path == "/download/get_token.cgi":
            return {"status": "OK", "url": "https://1fichier.com/dl/" + payload["url"].split("?")[-1]}
        raise AssertionError(path)

    async def fake_download_url(client, link):
        return "https://1fichier.com/dl/" + link.split("?")[-1]

    def fake_download(link, dest, holder):
        if link.endswith("a101"):
            shutil.copy(archive, dest)
            holder["done"] = os.path.getsize(dest)
            return
        raise of.OneFichierError("téléchargement refusé par 1fichier (HTTP 404)")

    monkeypatch.setattr(of, "_call", fake_call)
    monkeypatch.setattr(of, "_download_url", fake_download_url)
    monkeypatch.setattr(of, "_download_blocking", fake_download)

    report = asyncio.run(of.run_checks("test"))
    assert report["links_ok"] == 2
    assert [x["key"] for x in report["links_lost"]] == ["103"]
    assert report["tests"][0]["key"] == "101" and report["tests"][0]["ok"] is True
    assert of._get_entry("101")["last_test_ok"] is True
    assert len(alerts) == 1 and "1 lien(s) introuvable(s)" in alerts[0][1]

    # le suivant (102) est testé au passage suivant : téléchargement refusé -> alerte
    report2 = asyncio.run(of.run_checks("test2"))
    assert report2["tests"][0]["key"] == "102" and report2["tests"][0]["ok"] is False
    assert "test de restauration non réussi" in alerts[-1][1]

    # tout le monde a été testé récemment : aucun test inutile
    assert asyncio.run(of.run_checks("test3"))["tests"] == []


def test_dossier_de_travail_absent_n_est_pas_enregistre_comme_test(env, monkeypatch, tmp_path):
    of._update(lambda d: d["entries"].__setitem__("102", {"status": "done", "url": "https://1fichier.com/?x",
                                                           "password": "p", "archive_name": "a.7z", "folder": "tmdb-102",
                                                           "remote_size": 1000}))
    monkeypatch.setattr(config_settings(), "onefichier_staging_dir", str(tmp_path / "absent"))
    result = asyncio.run(of.test_restore("102"))
    assert result["ok"] is None
    assert of._get_entry("102").get("last_test_at") is None


def config_settings():
    from app import config
    return config.settings


def test_prochain_controle_a_l_heure_prevue(env, monkeypatch):
    # Version 50 : contrôle hebdomadaire (jour CHECK_WEEKDAY, heure CHECK_HOUR).
    monkeypatch.setattr(of, "CHECK_HOUR", 3)
    monkeypatch.setattr(of, "CHECK_WEEKDAY", 6)
    now = time.time()
    nxt = of._next_run_at(now)
    assert now < nxt <= now + 7 * 86400 + 60
    assert time.localtime(nxt).tm_hour == 3
    assert time.localtime(nxt).tm_wday == 6


def test_mode_simulation_ne_fait_aucun_appel(env, monkeypatch):
    monkeypatch.setattr(config_settings(), "dry_run", True)

    async def boom(*a, **k):
        raise AssertionError("aucun appel réseau en simulation")

    monkeypatch.setattr(of, "_call", boom)
    report = asyncio.run(of.run_checks("sec"))
    assert report["dry_run"] and report["links_ok"] == 0 and report["tests"] == []


def test_alerte_sans_url_n_envoie_rien(env, monkeypatch):
    from app import notify
    sent = []
    monkeypatch.setattr(notify.httpx, "AsyncClient", lambda *a, **k: sent.append(1) or (_ for _ in ()).throw(AssertionError))
    asyncio.run(notify.send("titre", "message"))  # notify_url vide par défaut dans env
    assert sent == []


def test_bilan_hebdomadaire_ok_envoye_meme_sans_probleme(env, monkeypatch):
    """Version 50 : le contrôle planifié envoie un bilan même quand tout va bien ; le contrôle manuel non."""
    from app import notify
    monkeypatch.setattr(of.config.settings, "dry_run", True)  # aucun lien à interroger : bilan propre
    monkeypatch.setattr(scheduler, "active_window", lambda now=None: {"name": "test", "onefichier_mbps": 0, "disk_mbps": 0})
    alerts = []

    async def fake_send(title, message):
        alerts.append(title)

    monkeypatch.setattr(notify, "send", fake_send)
    asyncio.run(of.run_checks("planifié"))
    assert alerts == ["1fichier : contrôle hebdomadaire OK"]
    alerts.clear()
    asyncio.run(of.run_checks("manuel"))
    assert alerts == []
