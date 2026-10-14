"""Audit de la version 42 : secrets, liens, sous-dossiers, simulation, code adulte, listes adultes,
journal, copies interrompues. Chaque test correspond à un constat de l'audit."""
import asyncio
import json
import os
import shutil
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import (adult_auth, adult_media, config, disk_library, jobs, logs, main, onefichier, plex,
                 scheduler, security, tmdb)


def _at(hour: int) -> datetime:
    return datetime(2026, 10, 12, hour, 0)


@pytest.fixture(autouse=True)
def reglages_restaures(monkeypatch):
    """update_settings remplace l'objet global : on le restaure après chaque test."""
    monkeypatch.setattr(config, "settings", config.settings)


@pytest.fixture
def client(env, monkeypatch):
    adult_auth.reset()
    monkeypatch.setattr(adult_auth, "ITERATIONS", 1000)
    monkeypatch.setattr(main, "log_action", lambda *a, **k: None)
    yield TestClient(main.app)
    adult_auth.reset()


def _deverrouille(client):
    adult_auth.set_code("1234")
    assert client.post("/api/adult/unlock", json={"code": "1234"}).status_code == 200


# --- Secrets -----------------------------------------------------------------

def test_reglages_ne_renvoient_pas_les_secrets(client, monkeypatch):
    monkeypatch.setattr(config.settings, "tmdb_api_key", "CLE-TMDB-SECRETE-1234")
    monkeypatch.setattr(config.settings, "plex_token", "JETON-PLEX-SECRET-5678")
    reglages = client.get("/api/settings").json()["settings"]
    assert "CLE-TMDB-SECRETE" not in json.dumps(reglages)
    assert "JETON-PLEX-SECRET" not in json.dumps(reglages)
    assert reglages["tmdb_api_key"].endswith("1234") and reglages["tmdb_api_key"].startswith("•")
    assert reglages["plex_token"].endswith("5678")


def test_enregistrer_un_secret_masque_ne_l_ecrase_pas(client, monkeypatch):
    monkeypatch.setattr(config.settings, "tmdb_api_key", "CLE-TMDB-REELLE-9999")
    masque = client.get("/api/settings").json()["settings"]["tmdb_api_key"]
    client.put("/api/settings", json={"tmdb_api_key": masque, "plex_token": masque})
    assert config.settings.tmdb_api_key == "CLE-TMDB-REELLE-9999"


def test_message_d_erreur_sans_secret(monkeypatch):
    monkeypatch.setattr(config.settings, "tmdb_api_key", "CLE-QUI-FUITE")
    assert "CLE-QUI-FUITE" not in main._sans_secret("GET https://x/?api_key=CLE-QUI-FUITE failed")


# --- Proxy d'affiches Plex ---------------------------------------------------

@pytest.mark.parametrize("chemin", ["/library/metadata/1/thumb/2", "/library/metadata/1/art/3.jpg"])
def test_chemin_plex_simple_accepte(chemin):
    assert plex.chemin_affiche_valide(chemin)


@pytest.mark.parametrize("chemin", ["@evil.example/x", "/x@evil.example", "/x?y=1", "/x#f", "/../secret",
                                    "x/relatif", "", "/a\\b"])
def test_chemin_plex_qui_change_d_hote_refuse(chemin):
    assert not plex.chemin_affiche_valide(chemin)


def test_route_affiche_plex_refuse_un_chemin_qui_change_d_hote(client, monkeypatch):
    monkeypatch.setattr(config.settings, "plex_url", "http://plex.lan:32400")
    monkeypatch.setattr(config.settings, "plex_token", "JETON")
    monkeypatch.setattr(type(config.settings), "plex_enabled", property(lambda self: True))
    assert client.get("/api/plex/poster", params={"path": "@evil.example/x"}).status_code == 400


# --- CORS --------------------------------------------------------------------

def test_pas_de_cors_pour_un_site_tiers(client):
    reponse = client.get("/api/settings", headers={"Origin": "https://site-malveillant.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in reponse.headers}


# --- Liens symboliques dans une source ---------------------------------------

def test_lien_de_dossier_dans_une_source_refuse(env, monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    hors = tmp_path / "hors"
    hors.mkdir()
    (hors / "victime.txt").write_text("ne pas toucher")
    (source / "lien").symlink_to(hors, target_is_directory=True)
    monkeypatch.setattr(config.settings, "source_dirs_films", [str(source)])
    assert security.is_inside_source_dirs(str(source / "dossier" / "f.mkv")) is True
    assert security.is_inside_source_dirs(str(source / "lien" / "victime.txt")) is False


def test_lien_de_fichier_dans_une_source_ne_bloque_pas_sa_propre_suppression(env, monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    hors = tmp_path / "hors.txt"
    hors.write_text("cible")
    (source / "raccourci.mkv").symlink_to(hors)
    monkeypatch.setattr(config.settings, "source_dirs_films", [str(source)])
    assert security.is_inside_source_dirs(str(source / "raccourci.mkv")) is True


# --- Réglages -----------------------------------------------------------------

@pytest.mark.parametrize("valeur", ["../../escape", "MEDIA/../../x", "MEDIA/./FILMS"])
def test_sous_dossier_avec_point_point_refuse(valeur):
    with pytest.raises(ValueError):
        config._as_relative_subdir(valeur)


def test_sous_dossier_normal_accepte():
    assert config._as_relative_subdir("/MEDIA//FILMS/") == "MEDIA/FILMS"


@pytest.mark.parametrize("valeur", [float("nan"), float("inf"), -1.0])
def test_debit_non_fini_refuse(valeur):
    with pytest.raises(ValueError):
        config._as_speed(valeur, "débit")


def test_fichier_reglages_illisible_force_la_simulation(env, monkeypatch):
    with open(config.SETTINGS_FILE, "w", encoding="utf-8") as fh:
        fh.write('{"dry_run": false, "source_dirs_films": [')  # tronqué
    assert config._load().dry_run is True


def test_ecriture_reglages_echouee_garde_l_etat_precedent(env, monkeypatch):
    avant = config.settings.dry_run

    def echec(_s):
        raise OSError("disque plein")

    monkeypatch.setattr(config, "_save", echec)
    with pytest.raises(OSError):
        config.update_settings({"dry_run": not avant})
    assert config.settings.dry_run is avant


def test_reglages_enregistres_sans_fichier_temporaire_restant(env):
    config.update_settings({"dry_run": True})
    assert os.path.exists(config.SETTINGS_FILE)
    assert not os.path.exists(config.SETTINGS_FILE + ".tmp")


# --- Simulation -----------------------------------------------------------------

def test_simulation_ne_retire_pas_un_envoi_de_l_index(env, monkeypatch):
    monkeypatch.setattr(config.settings, "dry_run", True)
    entry = {"kind": "film", "name": "Un", "status": "pending", "password": "mdp", "archived_at": 1}
    onefichier._write({"version": 1, "folders": {}, "entries": {"k1": entry}})
    onefichier.forget_pending("k1")
    assert "k1" in onefichier._read()["entries"], "la simulation ne doit rien modifier"


def test_simulation_ne_cree_pas_la_destination(env, monkeypatch, tmp_path):
    monkeypatch.setattr(config.settings, "dry_run", True)
    monkeypatch.setattr(config.settings, "archive_copy_dir", "")
    src = tmp_path / "src" / "Film.mkv"
    src.parent.mkdir()
    src.write_bytes(b"x" * 1000)
    dest = tmp_path / "disque" / "MEDIA"
    job = jobs.Job("sim-1", "copy", "films", [str(src)], str(dest), False)
    asyncio.run(jobs.run_job(job))
    assert not dest.exists()


# --- Code d'accès adulte ------------------------------------------------------

def test_code_illisible_n_est_pas_traite_comme_absent(client):
    with open(adult_auth._path(), "w", encoding="utf-8") as fh:
        fh.write("{ fichier tronqué")
    assert adult_auth._etat() == "illisible"
    reponse = client.post("/api/adult/code", json={"new": "9999"})
    assert reponse.status_code == 503
    assert adult_auth.session_is_valid(None) is False
    assert client.post("/api/adult/unlock", json={"code": "9999"}).status_code == 503


def test_code_absent_permet_la_premiere_definition(client):
    assert adult_auth._etat() == "absent"
    assert client.post("/api/adult/code", json={"new": "2468"}).status_code == 200


# --- Éléments adultes dans les dossiers et les listes -------------------------

def test_dossier_contenant_un_film_adulte_masque_sans_session(env, monkeypatch, tmp_path):
    dossier = tmp_path / "films" / "Dossier"
    dossier.mkdir(parents=True)
    film = dossier / "Titre (2003).mkv"
    film.write_bytes(b"x")
    adult_media.set_marked([str(film)], True)
    assert adult_media.folder_has_adult(str(dossier)) is True
    assert str(dossier) in adult_media.hidden_paths([str(dossier)], unlocked=False)
    assert adult_media.hidden_paths([str(dossier)], unlocked=True) == []


def test_dossier_dans_une_racine_adulte_masque(env, monkeypatch, tmp_path):
    racine = tmp_path / "adultes"
    (racine / "Titre").mkdir(parents=True)
    monkeypatch.setattr(config.settings, "source_dirs_adultes", [str(racine)])
    assert adult_media.folder_has_adult(str(racine / "Titre")) is True


def test_marque_posee_apres_cache_masque_l_element(env, tmp_path):
    film = tmp_path / "Film.mkv"
    film.write_bytes(b"x")
    item = {"name": "Film.mkv", "path": str(film)}  # objet mis en cache, sans drapeau adulte
    assert adult_media.is_adult_item(item) is False
    adult_media.set_marked([str(film)], True)
    assert adult_media.is_adult_item(item) is True


def test_copie_d_un_adulte_sans_sous_dossier_adulte_refusee(client, monkeypatch, tmp_path):
    _deverrouille(client)
    film = tmp_path / "src" / "Titre (2003).mkv"
    film.parent.mkdir()
    film.write_bytes(b"x" * 100)
    adult_media.set_marked([str(film)], True)
    monkeypatch.setattr(config.settings, "source_dirs_films", [str(film.parent)])
    monkeypatch.setattr(config.settings, "dest_subdir_adultes", "")
    dest = tmp_path / "disque"
    dest.mkdir()
    reponse = client.post("/api/jobs", json={"operation": "copy", "media_type": "films",
                                             "items": [str(film)], "destination": str(dest)})
    assert reponse.status_code == 400
    assert "sous-dossier adulte" in reponse.json()["detail"]


def test_plan_sans_session_ne_revele_pas_le_nombre_d_adultes(client, monkeypatch, tmp_path):
    film = tmp_path / "src" / "Titre (2003).mkv"
    film.parent.mkdir()
    film.write_bytes(b"x")
    adult_media.set_marked([str(film)], True)
    monkeypatch.setattr(config.settings, "source_dirs_films", [str(film.parent)])
    plan = client.post("/api/plan/films", json={"mode": "files", "paths": [str(film)]}).json()
    assert plan["items"] == []
    assert not any("adulte" in w for w in plan["warnings"])


def test_index_checks_sans_session_retire_les_resultats_adultes(env, monkeypatch):
    adulte = {"kind": "film", "name": "Titre", "status": "done", "tmdb_id": 1, "adult": True,
              "password": "p", "url": "https://1fichier.com/?a", "archived_at": 1}
    normal = {**adulte, "name": "Normal", "adult": False}
    onefichier._write({"version": 1, "folders": {}, "entries": {"ka": adulte, "kn": normal}})
    onefichier._update(lambda d: d.__setitem__("last_check_run", {
        "tests": [{"key": "ka", "folder": "tmdb-1", "ok": True, "error": None},
                  {"key": "kn", "folder": "tmdb-2", "ok": True, "error": None}],
        "links_lost": [{"key": "ka", "folder": "tmdb-1"}], "errors": []}))
    cache = onefichier.checks_status(unlocked=False)["last_run"]
    assert [t["key"] for t in cache["tests"]] == ["kn"]
    assert cache["links_lost"] == []
    assert [t["key"] for t in onefichier.checks_status(unlocked=True)["last_run"]["tests"]] == ["ka", "kn"]


def test_etiquette_sortante_d_un_adulte_sans_nom(env):
    assert onefichier._etiquette({"key": "604", "folder": "Titre adulte"}, {"604"}) == "élément n° 604"
    assert onefichier._etiquette({"key": "605", "folder": "Film"}, {"604"}) == "Film"


def test_exemples_d_integrite_sans_session_sans_dossier_adulte(client, monkeypatch):
    monkeypatch.setattr(config.settings, "dest_subdir_adultes", "MEDIA/ADULTE")
    etat = {"disques": [{"last_verify": {"new": 2, "new_examples": ["MEDIA/ADULTE/A (2005).mkv", "MEDIA/FILM/B.mkv"]}}]}
    filtre = main._sans_exemples_adultes(etat)
    assert filtre["disques"][0]["last_verify"]["new_examples"] == ["MEDIA/FILM/B.mkv"]
    assert filtre["disques"][0]["last_verify"]["new"] == 2


# --- Journal -------------------------------------------------------------------

def test_journal_n_ecrit_pas_le_nom_d_un_adulte(env, tmp_path):
    film = tmp_path / "Titre (2003).mkv"
    film.write_bytes(b"x")
    adult_media.set_marked([str(film)], True)
    logs.log_action("OK", "COPY", f"{film} -> disque/")
    lignes = logs.read_log(10)
    assert lignes and "Titre" not in json.dumps(lignes)
    assert lignes[0]["detail"] == "[élément adulte]"


def test_journal_limite_zero_ne_renvoie_pas_tout(env):
    for i in range(5):
        logs.log_action("OK", "TEST", f"ligne {i}")
    assert len(logs.read_log(0)) == 1
    assert len(logs.read_log(3)) == 3


# --- Copie interrompue ----------------------------------------------------------

def test_copie_partielle_detectee(tmp_path):
    src = tmp_path / "src.mkv"
    src.write_bytes(b"abcdefgh" * 1000)
    dst = tmp_path / "dst.mkv"
    dst.write_bytes(b"abcdefgh" * 300)  # début identique, tronqué
    assert jobs._copie_partielle(str(src), str(dst)) is True


def test_fichier_different_n_est_jamais_pris_pour_une_copie_partielle(tmp_path):
    src = tmp_path / "src.mkv"
    src.write_bytes(b"abcdefgh" * 1000)
    dst = tmp_path / "dst.mkv"
    dst.write_bytes(b"XXXXXXXX" * 300)
    assert jobs._copie_partielle(str(src), str(dst)) is False


def test_fichier_de_meme_taille_et_autre_date_n_est_pas_repris(tmp_path):
    src = tmp_path / "src.mkv"
    src.write_bytes(b"abcdefgh" * 100)
    dst = tmp_path / "dst.mkv"
    dst.write_bytes(b"ZZZZZZZZ" * 100)
    os.utime(dst, (time.time() - 3600, time.time() - 3600))
    assert jobs._copie_partielle(str(src), str(dst)) is False


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync requis")
def test_copie_interrompue_est_terminee_a_la_relance(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "_now", lambda: _at(10))
    monkeypatch.setattr(config, "LOG_FILE", str(tmp_path / "log.txt"))
    src = tmp_path / "src" / "Film.mkv"
    src.parent.mkdir()
    contenu = b"0123456789" * 5000
    src.write_bytes(contenu)
    dest = tmp_path / "disque" / "MEDIA"
    dest.mkdir(parents=True)
    (dest / "Film.mkv").write_bytes(contenu[:20000])  # copie interrompue
    job = jobs.Job("reprise-1", "copy", "films", [str(src)], str(dest), False)
    asyncio.run(jobs.run_job(job))
    assert (dest / "Film.mkv").read_bytes() == contenu
    assert not any(e.get("reason") == "already_exists" for e in job.events)
    assert job.events[-1]["summary"]["ok"] == 1


# --- Suite de la version 42 : TMDB, marge disque, garde de suppression, export verrouillé ----

def test_echec_tmdb_n_est_pas_mis_en_cache(monkeypatch):
    """Une panne réseau passagère ne doit pas devenir « aucun résultat » pour toute la session."""
    tmdb._match_cache.clear()
    appels = []

    async def capricieux(client, path, params):
        appels.append(dict(params))
        if len(appels) <= 2:  # recherche avec l'année, puis sans l'année : les deux échouent
            raise ConnectionError("panne passagère")
        return {"results": [{"id": 7, "title": "Titre", "name": "Titre", "genre_ids": [],
                             "overview": "Résumé", "release_date": "2003-01-01", "vote_average": 6.0}]}

    monkeypatch.setattr(tmdb, "_get_json", capricieux)
    assert asyncio.run(tmdb._search_one(None, "movie", "Titre", 2003)) is None
    trouve = asyncio.run(tmdb._search_one(None, "movie", "Titre", 2003))
    tmdb._match_cache.clear()
    assert trouve is not None and trouve["id"] == 7


def test_absence_reelle_de_resultat_tmdb_reste_mise_en_cache(monkeypatch):
    tmdb._match_cache.clear()
    appels = []

    async def vide(client, path, params):
        appels.append(1)
        return {"results": []}

    monkeypatch.setattr(tmdb, "_get_json", vide)
    assert asyncio.run(tmdb._search_one(None, "movie", "Inconnu", None)) is None
    assert asyncio.run(tmdb._search_one(None, "movie", "Inconnu", None)) is None
    tmdb._match_cache.clear()
    assert len(appels) == 1  # la seconde recherche ne repart pas vers TMDB


def test_cache_tmdb_est_borne(monkeypatch):
    monkeypatch.setattr(tmdb, "_CACHE_MAX", 3)
    monkeypatch.setattr(tmdb, "_match_cache", {})
    for i in range(10):
        tmdb._remember(f"k{i}", None)
    assert list(tmdb._match_cache) == ["k7", "k8", "k9"]


@pytest.mark.parametrize("valeur", [-1, "-500", "abc", float("nan")])
def test_marge_de_securite_invalide_refusee(valeur):
    with pytest.raises(ValueError):
        config._as_margin(valeur)


def test_marge_de_securite_nulle_ou_positive_acceptee():
    assert config._as_margin("0") == 0
    assert config._as_margin(1024) == 1024


def test_marge_negative_refusee_par_les_reglages(env):
    with pytest.raises(ValueError):
        config.update_settings({"safety_margin_bytes": -1})


def test_marge_d_environnement_invalide_ne_bloque_pas_le_demarrage(monkeypatch):
    monkeypatch.setenv("SAFETY_MARGIN_BYTES", "abc")
    assert config._env_margin() == 500 * 1024 * 1024
    assert config._env_defaults()["safety_margin_bytes"] == 500 * 1024 * 1024


def test_suppression_hors_zones_gerees_refusee_a_l_execution(env, monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    hors = tmp_path / "hors"
    hors.mkdir()
    (hors / "important.txt").write_text("ne pas supprimer")
    monkeypatch.setattr(config.settings, "source_dirs_films", [str(source)])
    monkeypatch.setattr(disk_library, "locate", lambda chemin: None)  # aucun disque connecté
    job = jobs.Job("del-hors", "delete", "films", [str(hors)], None, False)
    asyncio.run(jobs.run_job(job))
    assert (hors / "important.txt").exists()
    assert any(e["type"] == "item_error" for e in job.events)


def test_suppression_dans_une_source_reste_autorisee(env, monkeypatch, tmp_path):
    source = tmp_path / "source"
    film = source / "Film"
    film.mkdir(parents=True)
    (film / "f.mkv").write_bytes(b"x")
    monkeypatch.setattr(config.settings, "source_dirs_films", [str(source)])
    job = jobs.Job("del-ok", "delete", "films", [str(film)], None, False)
    asyncio.run(jobs.run_job(job))
    assert not film.exists()
    assert any(e["type"] == "item_done" for e in job.events)


def test_suppression_d_un_element_de_disque_connecte_reste_autorisee(env, monkeypatch, tmp_path):
    disque = tmp_path / "disque"
    element = disque / "MEDIA" / "FILMS" / "Film"
    element.mkdir(parents=True)
    (element / "f.mkv").write_bytes(b"x")
    monkeypatch.setattr(config.settings, "source_dirs_films", [])
    monkeypatch.setattr(disk_library, "locate",
                        lambda chemin: ("films", str(disque)) if chemin.startswith(str(disque)) else None)
    job = jobs.Job("del-disque", "delete", "films", [str(element)], None, False)
    asyncio.run(jobs.run_job(job))
    assert not element.exists()


# --- Performance (version 42) -------------------------------------------------

def test_identites_relues_apres_une_modification_exterieure(env):
    from app import identities
    identities.set_many("films", ["/films/A.mkv"], {"title": "Titre A", "year": 2001, "name": "Titre A (2001)"})
    assert identities.get("films", "/films/A.mkv")["title"] == "Titre A"
    # Modification faite hors de l'application : le cache doit suivre le fichier.
    chemin = identities._path()
    with open(chemin, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    data["films|/films/A.mkv"]["title"] = "Titre modifié à la main, plus long"
    with open(chemin, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    assert identities.get("films", "/films/A.mkv")["title"] == "Titre modifié à la main, plus long"


def test_identites_lecture_du_cache_ne_relit_pas_le_fichier(env, monkeypatch):
    from app import identities
    identities.set_many("films", ["/films/B.mkv"], {"title": "B"})
    identities.get("films", "/films/B.mkv")
    lectures = []
    originale = identities.json.load
    monkeypatch.setattr(identities.json, "load", lambda fh: (lectures.append(1), originale(fh))[1])
    for _ in range(20):
        identities.get("films", "/films/B.mkv")
    assert lectures == []


def test_store_metas_une_seule_reecriture(env, monkeypatch):
    from app import disk_library
    disque = str(env / "disque")
    data = {disque: {"path": disque, "connected": True, "items": {"films": []}, "meta": {}}}
    disk_library._write(data)
    ecritures = []
    originale = disk_library._write
    monkeypatch.setattr(disk_library, "_write", lambda d: (ecritures.append(1), originale(d))[1])
    disk_library.store_metas([(disque, f"{disque}/a", {"title": "A"}),
                              (disque, f"{disque}/b", {"title": "B"}),
                              ("/disque/absent", "/disque/absent/c", {"title": "C"})])
    assert len(ecritures) == 1
    meta = disk_library._read()[disque]["meta"]
    assert meta[f"{disque}/a"]["title"] == "A" and meta[f"{disque}/b"]["title"] == "B"


def test_restore_labels_lit_l_index_une_fois(env, monkeypatch):
    entries = {str(i): {"original_name": f"Film {i}", "kind": "film", "status": "done"} for i in range(50)}
    onefichier._write({"version": 1, "folders": {}, "entries": entries})
    lectures = []
    originale = onefichier._read
    monkeypatch.setattr(onefichier, "_read", lambda: (lectures.append(1), originale())[1])
    libelles = onefichier.restore_labels([str(i) for i in range(50)])
    assert len(lectures) == 1
    assert libelles[0] == "Film 0" and len(libelles) == 50


def test_taches_terminees_anciennes_sont_elaguees(env, monkeypatch):
    monkeypatch.setattr(jobs, "JOBS", {})
    monkeypatch.setattr(jobs, "JOB_KEEP_FINISHED", 5)
    for i in range(8):
        job = jobs.create_job(operation="copy", media_type="films", items=[])
        job.status = "done"
        job.created_at = 1000 + i
    en_cours = jobs.create_job(operation="copy", media_type="films", items=[])
    en_cours.status = "running"
    jobs.create_job(operation="copy", media_type="films", items=[])  # déclenche l'élagage
    terminees = sorted(j.created_at for j in jobs.JOBS.values() if j.status == "done")
    assert terminees == [1003, 1004, 1005, 1006, 1007]  # les plus anciennes partent d'abord
    assert en_cours.id in jobs.JOBS  # une tâche en cours n'est jamais retirée


def test_invalidation_pendant_reconstruction_relance_la_liste(tmp_path, monkeypatch):
    """Un changement des sources pendant la reconstruction ne doit pas être perdu (version 42)."""
    from app import scan_cache
    monkeypatch.setattr(config, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(scan_cache, "_ENTRIES", {})
    monkeypatch.setattr(scan_cache, "_TASKS", {})
    monkeypatch.setattr(scan_cache, "_GENERATION", {})
    appels = []

    async def constructeur(kind):
        appels.append(kind)
        numero = len(appels)
        await asyncio.sleep(0.05)
        return [{"name": f"v{numero}"}]

    monkeypatch.setattr(scan_cache, "_BUILDER", constructeur)

    async def scenario():
        scan_cache._start("films")                 # reconstruction lente en cours
        await asyncio.sleep(0.01)
        scan_cache.invalidate(("films",))          # un changement pendant ce calcul
        while scan_cache.is_refreshing("films") or len(appels) < 2:
            await asyncio.sleep(0.01)
        while scan_cache.is_refreshing("films"):
            await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert len(appels) == 2
    assert scan_cache._ENTRIES["films"]["items"] == [{"name": "v2"}]  # la liste reflète le changement
    assert scan_cache._ENTRIES["films"]["built_at"] > 0


# --- Scans des disques en arrière-plan (version 43) -----------------------------------

@pytest.fixture
def disque_scan(env, monkeypatch, tmp_path):
    """Un disque connecté ; `_scan` est remplacé par une fonction lente qui compte ses appels."""
    racine = tmp_path / "disque"
    (racine / "MEDIA" / "FILM" / "Film A (2001)").mkdir(parents=True)
    monkeypatch.setattr(disk_library.disks, "list_destination_disks",
                        lambda: [{"path": str(racine), "free_bytes": 1}])
    appels = []
    original = disk_library._scan

    def lent(root):
        appels.append(root)
        time.sleep(1.0)
        return original(root)

    monkeypatch.setattr(disk_library, "_scan", lent)
    yield {"racine": str(racine), "appels": appels}
    fin = time.monotonic() + 10
    while disk_library.scans_en_cours() and time.monotonic() < fin:
        time.sleep(0.05)
    disk_library._scans_prets.clear()


def test_scan_lent_ne_bloque_pas_la_reponse(disque_scan):
    debut = time.monotonic()
    liste = disk_library.refresh(attente=0.1)
    assert time.monotonic() - debut < 0.9  # on n'attend pas la fin du scan (1 s)
    assert disk_library.scans_en_cours() is True
    assert liste[0]["connected"] is True
    assert liste[0]["items"] == {}  # rien n'est encore inventorié
    fin = time.monotonic() + 10
    while disk_library.scans_en_cours() and time.monotonic() < fin:
        time.sleep(0.05)
    liste = disk_library.refresh(attente=0)  # le scan terminé est fusionné à l'appel suivant
    assert [it["name"] for it in liste[0]["items"]["films"]] == ["Film A (2001)"]


def test_pas_deux_scans_simultanes_pour_la_meme_racine(disque_scan):
    disk_library.refresh(attente=0.05)
    disk_library.refresh(attente=0.05)  # l'interface relit pendant le scan : aucun second scan
    assert len(disque_scan["appels"]) == 1


def test_route_disques_repond_pendant_un_scan(client, disque_scan, monkeypatch):
    monkeypatch.setattr(disk_library, "SCAN_WAIT_SECONDS", 0.2)  # le scan (1 s) dépasse l'attente
    debut = time.monotonic()
    reponse = client.get("/api/disk-library")
    assert reponse.status_code == 200
    assert time.monotonic() - debut < 0.9  # la réponse ne attend pas la fin du scan
    corps = reponse.json()
    assert corps["scanning"] is True and corps["meta_running"] is True

