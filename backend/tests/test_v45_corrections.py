"""Corrections de la version 45 : copie, déplacement, suppression.

Chaque test correspond à un constat de la revue (voir la section 36 du README) :
1. fusion : l'ancienne version d'un fichier remplacé est gardée ;
2. transfert interrompu : aucun fichier tronqué au nom final, reprise à la relance ;
3. tâche urgente : elle passe pendant l'attente d'une plage horaire ;
4. lien symbolique cassé : il ne fait plus échouer la vérification de tout un dossier ;
5. VERIFY_SHA256=false : vérification par tailles ;
6. réservation d'espace entre tâches ;
7. tâches en attente : reprises après un redémarrage ;
8. ALLOW_DELETE : suppression, déplacement et suppression sur disque refusés, côté API et à l'exécution.
"""
import asyncio
import json
import os
import shutil
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from app import config, jobs, main, scheduler

RSYNC = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync requis")


@pytest.fixture
def pret(tmp_path, monkeypatch):
    """Réglages isolés, fenêtre de transfert toujours ouverte, aucune tâche héritée d'un autre test."""
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
    return tmp_path


def _vieillir(path) -> None:
    t = time.time() - 10_000
    os.utime(path, (t, t))


# --- 1. Fusion : l'ancienne version est gardée -------------------------------------------------

@RSYNC
def test_fusion_garde_l_ancienne_version_du_fichier_remplace(pret):
    src_dir = pret / "src" / "Saison 1"
    src_dir.mkdir(parents=True)
    (src_dir / "ep01.mkv").write_bytes(b"NOUVELLE")
    show = pret / "disque" / "MEDIA" / "SERIES" / "Show"
    (show / "Saison 1").mkdir(parents=True)
    ancien = show / "Saison 1" / "ep01.mkv"
    ancien.write_bytes(b"ANCIENNE__")
    _vieillir(ancien)

    job = jobs.Job("fus1", "copy", "series", [str(src_dir)], str(pret / "disque" / "MEDIA" / "SERIES"), True,
                   dest_dirs={str(src_dir): str(show)})
    asyncio.run(jobs.run_job(job))

    assert ancien.read_bytes() == b"NOUVELLE"
    gardee = pret / "disque" / "MEDIA" / "SERIES" / jobs.REPLACED_DIR / "fus1" / "Show" / "Saison 1" / "ep01.mkv"
    assert gardee.read_bytes() == b"ANCIENNE__"
    assert job.events[-1]["summary"]["ok"] == 1
    assert any("remplacée(s)" in e.get("line", "") for e in job.events if e["type"] == "item_log")


# --- 2. Transfert interrompu : pas de fichier tronqué au nom final ---------------------------

@RSYNC
def test_transfert_interrompu_ne_laisse_pas_de_fichier_tronque(pret):
    src = pret / "src" / "gros.mkv"
    src.parent.mkdir()
    src.write_bytes(os.urandom(40 * 1024 * 1024))
    dst = pret / "disque"
    dst.mkdir()
    cmd = ["rsync", *jobs.RSYNC_OPTS, "--bwlimit=4096", str(src), str(dst) + "/"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _en_cours() -> bool:
        # Pendant le transfert, rsync écrit dans un fichier caché « .gros.mkv.XXXXXX » à la destination.
        temporaires = [p for p in dst.iterdir() if p.name.startswith(".gros.mkv.")]
        return any(p.stat().st_size > 1024 * 1024 for p in temporaires)

    # On coupe seulement une fois la copie réellement commencée (un délai fixe était instable sous charge).
    limite = time.time() + 20
    while time.time() < limite and not _en_cours():
        time.sleep(0.05)
    assert _en_cours(), "la copie n'a pas démarré"
    proc.kill()
    proc.wait()
    limite = time.time() + 5  # rsync range sa copie partielle dans le dossier dédié juste après l'arrêt
    while time.time() < limite and not (dst / jobs.PARTIAL_DIR / "gros.mkv").exists():
        time.sleep(0.05)

    assert not (dst / "gros.mkv").exists(), "un fichier tronqué ne doit jamais porter le nom final"
    assert (dst / jobs.PARTIAL_DIR / "gros.mkv").exists(), "la partie copiée doit être conservée pour la reprise"


@RSYNC
def test_relance_reprend_et_nettoie_le_dossier_partiel(pret):
    src = pret / "src" / "Film.mkv"
    src.parent.mkdir()
    src.write_bytes(os.urandom(3 * 1024 * 1024))
    dst = pret / "disque" / "MEDIA"
    dst.mkdir(parents=True)
    (dst / jobs.PARTIAL_DIR).mkdir()
    (dst / jobs.PARTIAL_DIR / "Film.mkv").write_bytes(src.read_bytes()[:1024 * 1024])  # reste d'une coupure

    job = jobs.Job("rel1", "copy", "films", [str(src)], str(dst), False)
    asyncio.run(jobs.run_job(job))

    assert (dst / "Film.mkv").read_bytes() == src.read_bytes()
    assert not (dst / jobs.PARTIAL_DIR).exists()
    assert job.events[-1]["summary"]["ok"] == 1


def test_dossier_partiel_est_ignore_par_la_verification(pret):
    dst = pret / "dst" / "Film"
    (dst / jobs.PARTIAL_DIR).mkdir(parents=True)
    (dst / jobs.PARTIAL_DIR / "x.part").write_bytes(b"zz")
    (dst / "film.mkv").write_bytes(b"x" * 10)
    src = pret / "src" / "Film"
    src.mkdir(parents=True)
    (src / "film.mkv").write_bytes(b"x" * 10)

    assert jobs._verify_copy(str(src), str(dst))[0] is True
    assert jobs._covered(str(src), str(dst)) is True
    assert jobs._copie_partielle(str(src), str(dst)) is False  # un fichier partiel n'est pas « en trop »


# --- 3. Tâche urgente : elle passe pendant l'attente d'une plage -----------------------------

@RSYNC
def test_tache_urgente_passe_pendant_l_attente_de_plage(pret, monkeypatch):
    fenetre = {"ouverte": False}
    monkeypatch.setattr(scheduler, "active_window", lambda *a, **k: {"name": "t"} if fenetre["ouverte"] else None)
    monkeypatch.setattr(scheduler, "next_window", lambda *a, **k: None)
    monkeypatch.setattr(scheduler, "_wait_message", lambda *a, **k: "hors plage")
    monkeypatch.setattr(scheduler, "RECHECK_SECONDS", 0.01)

    async def _sleep(s):
        await asyncio.sleep(0.01)
    monkeypatch.setattr(scheduler, "_sleep", _sleep)

    src = pret / "src" / "Film.mkv"
    src.parent.mkdir()
    src.write_bytes(b"x" * 100)
    dst = pret / "disque"
    dst.mkdir()
    normale = jobs.Job("norm", "copy", "films", [str(src)], str(dst), False, priority="normal")
    urgente = jobs.Job("urg", "copy", "films", [], None, False, priority="urgent")
    passee = []

    async def scenario():
        tache = asyncio.create_task(jobs._copy_or_move_one(normale, str(src), str(dst), target="/cible"))
        await asyncio.sleep(0.05)
        async with scheduler.slot(urgente, "disk", "/cible"):
            passee.append(True)
        assert not tache.done(), "la tâche normale attend toujours la plage"
        fenetre["ouverte"] = True
        await tache

    asyncio.run(scenario())
    assert passee == [True]
    assert (dst / "Film.mkv").exists()


# --- 4. Lien symbolique cassé ----------------------------------------------------------------

@RSYNC
def test_lien_casse_n_empeche_pas_la_copie_d_un_dossier(pret):
    src = pret / "src" / "Film"
    src.mkdir(parents=True)
    (src / "film.mkv").write_bytes(b"x" * 1000)
    os.symlink(str(pret / "absent.mkv"), src / "bonus.mkv")
    dst = pret / "disque" / "MEDIA"
    dst.mkdir(parents=True)

    job = jobs.Job("lien1", "copy", "films", [str(src)], str(dst), False)
    asyncio.run(jobs.run_job(job))

    assert job.events[-1]["summary"]["ok"] == 1
    assert os.path.islink(dst / "Film" / "bonus.mkv")
    assert os.readlink(dst / "Film" / "bonus.mkv") == str(pret / "absent.mkv")


def test_verification_compare_la_cible_des_liens(pret):
    src = pret / "src" / "Film"
    src.mkdir(parents=True)
    os.symlink("cible-a", src / "lien")
    dst = pret / "dst" / "Film"
    dst.mkdir(parents=True)
    os.symlink("cible-b", dst / "lien")

    assert jobs._verify_copy(str(src), str(dst))[0] is False
    os.remove(dst / "lien")
    os.symlink("cible-a", dst / "lien")
    assert jobs._verify_copy(str(src), str(dst))[0] is True


# --- 5. Vérification par tailles -------------------------------------------------------------

def test_verification_par_tailles_quand_sha256_est_desactive(pret, monkeypatch):
    a = pret / "a"
    a.mkdir()
    (a / "f.mkv").write_bytes(b"AAAA")
    b = pret / "b"
    b.mkdir()
    (b / "f.mkv").write_bytes(b"BBBB")  # même taille, contenu différent

    monkeypatch.setattr(config, "VERIFY_SHA256", False)
    assert jobs._verify_copy(str(a), str(b))[0] is True  # limite assumée de ce mode
    monkeypatch.setattr(config, "VERIFY_SHA256", True)
    assert jobs._verify_copy(str(a), str(b))[0] is False


# --- 6. Espace réservé par les tâches en cours -----------------------------------------------

def test_espace_reserve_puis_libere_par_element(pret):
    src = pret / "src" / "Film"
    src.mkdir(parents=True)
    (src / "f.mkv").write_bytes(b"x" * 5000)
    dst = pret / "disque" / "MEDIA"

    job = jobs.create_job("copy", "films", [str(src)], destination=str(dst))
    jobs.reserve_for_job(job)
    dev = jobs.device_of(str(dst))
    assert jobs.reserved_bytes(dev) == 5000

    jobs._liberer_reservation(job, str(src))
    assert jobs.reserved_bytes(dev) == 0


def test_reservation_ignoree_en_simulation(pret, monkeypatch):
    monkeypatch.setattr(config.settings, "dry_run", True)
    src = pret / "src" / "Film.mkv"
    src.parent.mkdir()
    src.write_bytes(b"x" * 5000)
    job = jobs.create_job("copy", "films", [str(src)], destination=str(pret / "disque"))
    jobs.reserve_for_job(job)
    assert jobs.reserved_bytes(jobs.device_of(str(pret))) == 0


# --- 7. Tâches en attente : reprises après redémarrage ---------------------------------------

def test_taches_en_attente_reprises_apres_redemarrage(pret):
    src = pret / "src" / "Film.mkv"
    src.parent.mkdir()
    src.write_bytes(b"x" * 10)
    job = jobs.create_job("copy", "films", [str(src)], destination=str(pret / "disque" / "MEDIA"),
                          priority="urgent")
    donnees = json.loads(open(jobs.pending_file(), encoding="utf-8").read())
    assert [e["id"] for e in donnees] == [job.id]

    jobs.JOBS.clear()  # simule le redémarrage du serveur
    reprises = jobs.restore_pending()

    assert len(reprises) == 1
    assert reprises[0].id == job.id
    assert reprises[0].resumed is True
    assert reprises[0].priority == "urgent"
    assert reprises[0].items == [str(src)]

    # Une tâche terminée n'est plus reprise.
    reprises[0].status = "done"
    jobs.persist_pending()
    assert json.loads(open(jobs.pending_file(), encoding="utf-8").read()) == []


def test_reprise_saute_les_elements_deja_traites(pret):
    dest = pret / "disque"
    dest.mkdir()
    (dest / "Film.mkv").write_bytes(b"x" * 10)
    absent = str(pret / "src" / "Film.mkv")  # déjà déplacé avant l'arrêt

    job = jobs.Job("reprise-2", "move", "films", [absent], str(dest), False)
    job.resumed = True
    asyncio.run(jobs._copy_or_move_one(job, absent, str(dest)))
    assert any(e.get("reason") == "already_done" for e in job.events if e["type"] == "item_skipped")
    assert not any(e["type"] == "item_error" for e in job.events)


def test_arret_en_cours_de_tache_la_garde_dans_la_file(pret):
    src = pret / "src" / "Film.mkv"
    src.parent.mkdir()
    src.write_bytes(b"x" * 10)
    job = jobs.create_job("copy", "films", [str(src)], destination=str(pret / "disque"))

    async def arret_brutal():
        task = asyncio.create_task(jobs.run_job(job))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(arret_brutal())
    assert job.status != "done"
    assert [e["id"] for e in json.loads(open(jobs.pending_file(), encoding="utf-8").read())] == [job.id]


# --- 8. Interrupteur ALLOW_DELETE -------------------------------------------------------------

def test_api_refuse_suppression_et_deplacement_si_allow_delete_est_faux(pret, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_DELETE", False)
    client = TestClient(main.app)

    r = client.post("/api/jobs", json={"operation": "delete", "media_type": "films", "items": ["/x/y"],
                                       "confirm_delete_text": config.settings.delete_confirm_text})
    assert r.status_code == 403 and "ALLOW_DELETE" in r.text

    r = client.post("/api/jobs", json={"operation": "move", "media_type": "films", "items": ["/x/y"],
                                       "destination": "/media/a/b"})
    assert r.status_code == 403 and "ALLOW_DELETE" in r.text

    r = client.post("/api/disk-library/delete", json={"items": ["/x/y"],
                                                     "confirm_delete_text": config.settings.delete_confirm_text})
    assert r.status_code == 403 and "ALLOW_DELETE" in r.text


def test_garde_d_execution_refuse_la_suppression(pret, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_DELETE", False)
    fichier = pret / "Film.mkv"
    fichier.write_bytes(b"x")
    job = jobs.Job("del-1", "delete", "films", [str(fichier)], None, False)
    asyncio.run(jobs._run_delete(job))
    assert fichier.exists()
    assert any(e["type"] == "item_error" for e in job.events)


def test_config_publie_les_interrupteurs_et_les_caracteres_interdits(pret, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_DELETE", False)
    client = TestClient(main.app)
    cfg = client.get("/api/config").json()
    assert cfg["allow_delete"] is False
    assert cfg["verify_sha256"] is True
    assert ":" in cfg["forbidden_name_chars"] and "/" in cfg["forbidden_name_chars"]


# --- 9. Arrêt propre pendant un transfert, et nettoyage après arrêt brutal -----------------

@RSYNC
def test_arret_propre_range_la_copie_partielle_et_la_relance_reprend(pret, monkeypatch):
    monkeypatch.setattr(jobs, "bwlimit_args", lambda: ["--bwlimit=4096"])  # transfert assez lent pour l'arrêt
    src = pret / "src" / "gros.mkv"
    src.parent.mkdir()
    contenu = os.urandom(30 * 1024 * 1024)
    src.write_bytes(contenu)
    dst = pret / "disque" / "MEDIA"
    dst.mkdir(parents=True)

    job = jobs.Job("arr1", "copy", "films", [str(src)], str(dst), False)

    async def arret_en_cours_de_transfert():
        task = asyncio.create_task(jobs.run_job(job))
        limite = time.time() + 20
        while time.time() < limite and not any(p.name.startswith(".gros.mkv.") for p in dst.iterdir()):
            await asyncio.sleep(0.05)
        await jobs.arreter_transferts()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(arret_en_cours_de_transfert())

    assert job.status != "done", "la tâche arrêtée reste en attente"
    assert not any(e["type"] == "item_error" for e in job.events), "un arrêt n'est pas une erreur"
    assert not (dst / "gros.mkv").exists()
    assert (dst / jobs.PARTIAL_DIR / "gros.mkv").exists(), "la copie partielle est rangée pour la reprise"

    # Relance : la copie reprend et aboutit, identique à la source.
    monkeypatch.setattr(jobs, "ARRETE", False)
    monkeypatch.setattr(jobs, "bwlimit_args", lambda: [])
    relance = jobs.Job("arr1-b", "copy", "films", [str(src)], str(dst), False)
    relance.resumed = True
    asyncio.run(jobs.run_job(relance))
    assert (dst / "gros.mkv").read_bytes() == contenu
    assert not (dst / jobs.PARTIAL_DIR).exists()
    assert relance.events[-1]["summary"]["ok"] == 1


def test_reprise_range_le_temporaire_laisse_par_un_arret_brutal(pret):
    # Version 46 : le temporaire n'est plus supprimé mais rangé dans le dossier partiel (point de départ de la reprise).
    src = pret / "src" / "Film.mkv"
    src.parent.mkdir()
    src.write_bytes(b"x" * 100)
    dst = pret / "disque"
    dst.mkdir()
    (dst / ".Film.mkv.Ab3dEf").write_bytes(b"x" * 60)  # reste d'un arrêt brutal : le début de la copie
    (dst / ".Autre.mkv.Ab3dEf").write_bytes(b"ne doit pas etre touche")  # fichier caché sans rapport
    jobs.create_job("copy", "films", [str(src)], destination=str(dst))

    jobs.JOBS.clear()
    jobs.restore_pending()

    assert not (dst / ".Film.mkv.Ab3dEf").exists()
    assert (dst / jobs.PARTIAL_DIR / "Film.mkv").read_bytes() == b"x" * 60
    assert (dst / ".Autre.mkv.Ab3dEf").exists()


def test_archive_1fichier_refuse_de_retirer_la_copie_locale_si_allow_delete_est_faux(pret, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_DELETE", False)
    client = TestClient(main.app)
    r = client.post("/api/1fichier/archive", json={"paths": ["/x/Film.mkv"], "after": "delete",
                                                   "confirm_delete_text": config.settings.delete_confirm_text})
    assert r.status_code == 403 and "ALLOW_DELETE" in r.text
    r = client.post("/api/1fichier/archive", json={"paths": ["/x/Film.mkv"], "after": "move",
                                                   "destination": "/media/a/b"})
    assert r.status_code == 403 and "ALLOW_DELETE" in r.text
