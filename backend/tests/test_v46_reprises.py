"""Reprises après arrêt brutal (version 46).

Étape 5 — copies : après un SIGKILL, rsync laisse un fichier « .nom.XXXXXX » à destination. Il est rangé dans
le dossier partiel, et la relance le réutilise (option --no-whole-file, sans laquelle rsync renvoie tout le
fichier : constat mesuré sur rsync 3.2.7).

Étape 6 — envois 1fichier : un envoi interrompu est relancé au redémarrage, selon ce que 1fichier contient :
rien d'envoyé → on repart de zéro ; archive complète chez 1fichier → on l'enregistre sans la renvoyer ;
fichier incomplet ou absent → on repart de zéro avec un avertissement ; 1fichier injoignable → on ne touche à rien.

Les appels réseau et 7z sont remplacés : aucun test ne contacte 1fichier.
"""
import asyncio
import json
import os
import re
import shutil
import subprocess

import pytest

from app import config, jobs, onefichier, scheduler

RSYNC = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync requis")


# ---------------------------------------------------------------------------------------------
# Étape 5 : copies
# ---------------------------------------------------------------------------------------------

@pytest.fixture
def pret(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_FILE", str(tmp_path / "log" / "archivage.log"))
    monkeypatch.setattr(config, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(config.settings, "dry_run", False)
    monkeypatch.setattr(config, "ALLOW_DELETE", True)
    monkeypatch.setattr(config, "VERIFY_SHA256", True)
    monkeypatch.setattr(scheduler, "active_window", lambda *a, **k: {"name": "test"})
    monkeypatch.setattr(scheduler, "rate_mbps", lambda *a, **k: 0)
    monkeypatch.setattr(jobs, "JOBS", {})
    monkeypatch.setattr(jobs, "_RESERVED", {})
    monkeypatch.setattr(jobs, "ARRETE", False)
    monkeypatch.setattr(jobs, "bwlimit_args", lambda: [])
    return tmp_path


def _redemarrer() -> list:
    """Simule le redémarrage du serveur : la file est relue depuis le fichier d'attente."""
    jobs.JOBS.clear()
    return jobs.restore_pending()


@RSYNC
def test_rsync_reutilise_la_partie_copiee_avec_no_whole_file(tmp_path):
    src = tmp_path / "src.bin"
    contenu = os.urandom(4 * 1024 * 1024)
    src.write_bytes(contenu)
    dst = tmp_path / "dst"
    (dst / jobs.PARTIAL_DIR).mkdir(parents=True)
    (dst / jobs.PARTIAL_DIR / "src.bin").write_bytes(contenu[: 2 * 1024 * 1024])  # moitié copiée

    r = subprocess.run(["rsync", *jobs.RSYNC_OPTS, "--no-whole-file", "--stats", str(src), str(dst) + "/"],
                       capture_output=True, text=True)

    assert r.returncode == 0, r.stderr
    matched = int(re.search(r"Matched data: ([\d,]+) bytes", r.stdout).group(1).replace(",", ""))
    assert matched >= 1_900_000, "la moitié déjà copiée doit être réutilisée, pas renvoyée"
    assert (dst / "src.bin").read_bytes() == contenu


def test_arret_brutal_range_le_temporaire_pour_la_reprise_d_un_fichier(pret):
    src = pret / "src" / "Film.mkv"
    src.parent.mkdir()
    src.write_bytes(b"x" * 100)
    dst = pret / "disque"
    dst.mkdir()
    (dst / ".Film.mkv.Ab3dEf").write_bytes(b"x" * 60)
    jobs.create_job("copy", "films", [str(src)], destination=str(dst))

    _redemarrer()

    assert not (dst / ".Film.mkv.Ab3dEf").exists()
    assert (dst / jobs.PARTIAL_DIR / "Film.mkv").read_bytes() == b"x" * 60
    assert not (dst / "Film.mkv").exists(), "rien n'est rangé sous le nom final"


def test_arret_brutal_retire_le_temporaire_si_le_fichier_final_existe(pret):
    src = pret / "src" / "Film.mkv"
    src.parent.mkdir()
    src.write_bytes(b"x" * 100)
    dst = pret / "disque"
    dst.mkdir()
    (dst / "Film.mkv").write_bytes(b"version finale")
    (dst / ".Film.mkv.Ab3dEf").write_bytes(b"partiel")
    jobs.create_job("copy", "films", [str(src)], destination=str(dst), merge=True)

    _redemarrer()

    assert not (dst / ".Film.mkv.Ab3dEf").exists()
    assert (dst / "Film.mkv").read_bytes() == b"version finale"
    assert not (dst / jobs.PARTIAL_DIR).exists()


def test_arret_brutal_range_les_temporaires_d_un_dossier(pret):
    src = pret / "src" / "Saison 1"
    src.mkdir(parents=True)
    (src / "ep01.mkv").write_bytes(b"x" * 100)
    dst = pret / "disque"
    (dst / "Saison 1").mkdir(parents=True)
    (dst / "Saison 1" / ".ep01.mkv.Ab3dEf").write_bytes(b"x" * 40)
    jobs.create_job("copy", "series", [str(src)], destination=str(dst))

    _redemarrer()

    assert (dst / "Saison 1" / jobs.PARTIAL_DIR / "ep01.mkv").read_bytes() == b"x" * 40
    assert not (dst / "Saison 1" / ".ep01.mkv.Ab3dEf").exists()


def test_seule_une_tache_reprise_utilise_no_whole_file(pret, monkeypatch):
    captures = []
    original = jobs._run_rsync

    async def espion(job, name, base_cmd, control):
        captures.append(list(base_cmd))
        return await original(job, name, base_cmd, control)

    monkeypatch.setattr(jobs, "_run_rsync", espion)
    src = pret / "src" / "Film.mkv"
    src.parent.mkdir()
    src.write_bytes(b"x" * 1000)

    normale = jobs.Job("normale", "copy", "films", [str(src)], str(pret / "a"), False)
    asyncio.run(jobs.run_job(normale))
    reprise = jobs.Job("reprise", "copy", "films", [str(src)], str(pret / "b"), False)
    reprise.resumed = True
    asyncio.run(jobs.run_job(reprise))

    assert "--no-whole-file" not in captures[0]
    assert "--no-whole-file" in captures[1]
    assert (pret / "b" / "Film.mkv").read_bytes() == b"x" * 1000


def test_reprise_apres_arret_brutal_aboutit_identique(pret):
    src = pret / "src" / "Film.mkv"
    src.parent.mkdir()
    contenu = os.urandom(3 * 1024 * 1024)
    src.write_bytes(contenu)
    dst = pret / "disque"
    dst.mkdir()
    (dst / ".Film.mkv.Ab3dEf").write_bytes(contenu[: 1024 * 1024])  # arrêt brutal à 1/3
    jobs.create_job("copy", "films", [str(src)], destination=str(dst))

    reprises = _redemarrer()
    asyncio.run(jobs.run_job(reprises[0]))

    assert (dst / "Film.mkv").read_bytes() == contenu
    assert not (dst / jobs.PARTIAL_DIR).exists()
    assert reprises[0].events[-1]["summary"]["ok"] == 1


# ---------------------------------------------------------------------------------------------
# Étape 6 : envois 1fichier
# ---------------------------------------------------------------------------------------------

class Arret(BaseException):
    """Simule un arrêt brutal : n'est pas une erreur (BaseException) et traverse le code sans être rattrapé."""


@pytest.fixture
def envoi(env, tmp_path, monkeypatch):
    """Envoi 1fichier simulé : compression, téléversement et réseau remplacés. `etat` garde les compteurs."""
    monkeypatch.setattr(scheduler, "active_window", lambda *a, **k: {"name": "test", "onefichier_mbps": 0, "disk_mbps": 0})
    monkeypatch.setattr(config, "LOG_FILE", str(tmp_path / "journal" / "archivage.log"))
    monkeypatch.setattr(jobs, "JOBS", {})  # sans cela, les tâches des tests précédents seraient réécrites dans le fichier d'attente
    monkeypatch.setattr(jobs, "_RESERVED", {})
    etat = {"uploads": 0, "taille": None, "crash": None, "listing": [], "reseau_coupe": False,
            "passwords": [], "appels": []}

    film = tmp_path / "films" / "Film (2001)"
    film.mkdir(parents=True)
    fichier = film / "Film.mkv"
    fichier.write_bytes(b"f" * 5000)
    etat["unit"] = str(fichier)

    def fake_classify(path):
        return onefichier.KIND_FILM, path
    monkeypatch.setattr(onefichier, "classify", fake_classify)

    async def fake_identify(job, kind, unit, name):
        return {"key": "101", "folder": "tmdb-101", "tmdb_id": 101, "title": "Film", "year": None, "meta": None}
    monkeypatch.setattr(onefichier, "_identify", fake_identify)

    monkeypatch.setattr(onefichier, "_7z_create", _compression_simulee(etat))
    monkeypatch.setattr(onefichier, "_7z_test", lambda archive, password: None)
    monkeypatch.setattr(onefichier, "_ensure_space", lambda *a, **k: None)

    async def fake_folder(client, key, name, parent_key=None):
        return 7
    monkeypatch.setattr(onefichier, "_ensure_folder", fake_folder)

    async def fake_server(client):
        return "node", "uid"
    monkeypatch.setattr(onefichier, "_upload_server", fake_server)

    async def fake_progress(job, name, total, read_done, blocking, key=None):
        etat["uploads"] += 1
        etat["taille"] = total
        if etat["crash"] == "pendant_upload":
            raise Arret()
    monkeypatch.setattr(onefichier, "_with_progress", fake_progress)

    async def fake_end(node, upload_id):
        return {"size": etat["taille"], "download": "https://1fichier.com/?abc123"}
    monkeypatch.setattr(onefichier, "_end_upload", fake_end)

    async def fake_call(client, method, path, payload=None):
        etat["appels"].append(path)
        if etat["reseau_coupe"]:
            raise onefichier.OneFichierError("1fichier ne répond pas (réseau)")
        if path == "/file/ls.cgi":
            return {"status": "OK", "files": etat["listing"]}
        raise AssertionError(path)
    monkeypatch.setattr(onefichier, "_call", fake_call)

    async def fake_refresh(keys=None, force=False):
        return 0
    monkeypatch.setattr(onefichier, "refresh_meta", fake_refresh)

    async def fake_notify(title, message):
        return None
    from app import notify
    monkeypatch.setattr(notify, "send", fake_notify)

    etat["tmp"] = tmp_path
    return etat


def _compression_simulee(etat):
    def fake(parent, base, archive, password, level):
        etat["passwords"].append(password)
        with open(archive, "wb") as fh:
            fh.write(b"A" * 1000)
    return fake


def _envoi(etat) -> jobs.Job:
    job = jobs.create_job("archive_1fichier", "films", [etat["unit"]])
    job.after = "keep"
    jobs.persist_pending()
    return job


def _entree(key: str = "101") -> dict:
    return onefichier._get_entry(key) or {}


def _evenements(job, type_) -> list:
    return [e for e in job.events if e["type"] == type_]


def test_envoi_persiste_et_restaure_avec_son_mode_apres(envoi):
    job = _envoi(envoi)
    donnees = json.loads(open(jobs.pending_file(), encoding="utf-8").read())
    assert donnees[0]["operation"] == "archive_1fichier"
    assert donnees[0]["after"] == "keep"

    jobs.JOBS.clear()
    restaures = jobs.restore_pending()
    assert [j.id for j in restaures] == [job.id]
    assert restaures[0].operation == "archive_1fichier"
    assert restaures[0].after == "keep"
    assert restaures[0].resumed is True


def test_arret_avant_le_televersement_repart_de_zero(envoi, monkeypatch):
    job = _envoi(envoi)

    def compression_coupee(parent, base, archive, password, level):
        raise Arret()  # arrêt pendant la compression : aucun téléversement n'a commencé
    monkeypatch.setattr(onefichier, "_7z_create", compression_coupee)
    with pytest.raises(Arret):
        asyncio.run(onefichier.run_archive(job))
    monkeypatch.setattr(onefichier, "_7z_create", _compression_simulee(envoi))
    premier_mot_de_passe = _entree()["password"]
    assert _entree()["status"] == "pending"
    assert _entree().get("upload_started_at") is None

    restaures = _redemarrer()
    asyncio.run(onefichier.run_archive(restaures[0]))

    assert _entree()["status"] == "done"
    assert _entree()["password"] != premier_mot_de_passe, "nouvel envoi, nouveau mot de passe"
    assert envoi["uploads"] == 1
    assert "/file/ls.cgi" not in envoi["appels"], "rien n'avait été envoyé : pas besoin d'interroger 1fichier"
    assert _evenements(restaures[0], "item_done")


def test_arret_pendant_le_televersement_archive_complete_retrouvee_sans_renvoi(envoi):
    envoi["crash"] = "pendant_upload"
    job = _envoi(envoi)
    with pytest.raises(Arret):
        asyncio.run(onefichier.run_archive(job))
    assert _entree()["status"] == "pending"
    assert _entree()["upload_started_at"] is not None
    assert _entree()["archive_size"] == 1000

    envoi["crash"] = None
    envoi["listing"] = [{"filename": "tmdb-101.7z", "size": 1000, "url": "abc123"}]
    restaures = _redemarrer()
    asyncio.run(onefichier.run_archive(restaures[0]))

    assert envoi["uploads"] == 1, "l'archive complète est retrouvée : aucun nouvel envoi"
    assert _entree()["status"] == "done"
    assert _entree()["recovered"] is True
    assert _entree()["url"] == "https://1fichier.com/?abc123"
    assert _evenements(restaures[0], "item_done")


def test_arret_pendant_le_televersement_fichier_incomplet_renvoye_avec_alerte(envoi):
    envoi["crash"] = "pendant_upload"
    job = _envoi(envoi)
    with pytest.raises(Arret):
        asyncio.run(onefichier.run_archive(job))

    envoi["crash"] = None
    envoi["listing"] = [{"filename": "tmdb-101.7z", "size": 400, "url": "abc"}]  # partiel
    restaures = _redemarrer()
    asyncio.run(onefichier.run_archive(restaures[0]))

    assert envoi["uploads"] == 2, "le fichier distant est incomplet : l'envoi repart de zéro"
    assert _entree()["status"] == "done"
    assert _entree().get("recovered") is None
    journal = open(config.LOG_FILE, encoding="utf-8").read()
    assert "n'est pas complet sur 1fichier" in journal, "l'utilisateur est averti de vérifier le dossier"


def test_1fichier_injoignable_au_redemarrage_ne_change_rien(envoi):
    envoi["crash"] = "pendant_upload"
    job = _envoi(envoi)
    with pytest.raises(Arret):
        asyncio.run(onefichier.run_archive(job))

    envoi["crash"] = None
    envoi["reseau_coupe"] = True
    restaures = _redemarrer()
    asyncio.run(onefichier.run_archive(restaures[0]))

    assert envoi["uploads"] == 1, "aucun nouvel envoi tant que 1fichier ne répond pas"
    assert _entree()["status"] == "pending", "l'entrée reste en attente, rien n'est perdu"
    assert _evenements(restaures[0], "item_error")


def test_envoi_d_avant_la_version_46_n_est_pas_repris_automatiquement(envoi):
    # Entrée créée sans le suivi de la version 46 : son état est inconnu, on ne relance rien.
    _envoi(envoi)
    onefichier._update(lambda d: d["entries"].update({"101": {
        "kind": "film", "status": "pending", "password": "ANCIEN-MOT-DE-PASSE-1",
        "archive_name": "tmdb-101.7z", "folder": "tmdb-101", "folder_id": 7, "tmdb_id": 101,
        "title": "Film", "original_name": "Film.mkv", "original_path": envoi["unit"], "created_at": 0,
        "archived_at": None, "restored_at": None, "error": None, "url": None, "remote_size": None,
        "sha256": None, "local_copy": None, "local_copy_at": None}}))

    restaures = _redemarrer()
    asyncio.run(onefichier.run_archive(restaures[0]))

    assert envoi["uploads"] == 0
    assert _entree()["status"] == "pending"
    assert any("avant la version 46" in e.get("error", "") for e in _evenements(restaures[0], "item_error"))


def test_source_absente_pendant_une_reprise_n_est_pas_une_erreur(envoi):
    _envoi(envoi)
    shutil.rmtree(os.path.dirname(envoi["unit"]))  # retirée après un envoi réussi, avant l'arrêt
    restaures = _redemarrer()
    asyncio.run(onefichier.run_archive(restaures[0]))
    assert not _evenements(restaures[0], "item_error")
    assert _evenements(restaures[0], "item_skipped")
