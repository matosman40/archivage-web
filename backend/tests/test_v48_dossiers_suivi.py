"""Version 48 : dossier de destination par disque, déplacement dans le même disque, suivi des
copies et déplacements.

Les disques sont des dossiers temporaires (disks.list_destination_disks remplacé) ; les
déplacements sont de vrais renommages sur ce dossier, et les copies de vrais rsync.
"""
import asyncio
import json
import os

import pytest

from app import adult_auth, config, disk_library, disk_moves, jobs, scheduler, tmdb
from app import disks as disks_mod


@pytest.fixture(autouse=True)
def _isolation(monkeypatch):
    adult_auth.reset()
    monkeypatch.setattr(adult_auth, "ITERATIONS", 1000)
    monkeypatch.setattr(jobs, "JOBS", {})
    monkeypatch.setattr(tmdb, "_search_cache", {})
    monkeypatch.setattr(config, "ALLOW_DELETE", True)
    yield
    adult_auth.reset()


@pytest.fixture
def disque(env, monkeypatch, tmp_path):
    """Un disque connecté : MEDIA/FILM (deux films), MEDIA/FILMS (vide), MEDIA/SERIES (une série),
    MEDIA/ADULTE (un film adulte). Dossiers sources : films, adultes, séries, ebooks."""
    films = tmp_path / "films"
    adultes = tmp_path / "adultes"
    films.mkdir()
    adultes.mkdir()
    root = tmp_path / "disque"
    (root / "MEDIA" / "FILM").mkdir(parents=True)
    (root / "MEDIA" / "FILMS").mkdir(parents=True)
    (root / "MEDIA" / "SERIES" / "Serie X").mkdir(parents=True)
    (root / "MEDIA" / "ADULTE").mkdir(parents=True)
    (root / "MEDIA" / "FILM" / "Film A (2001).mkv").write_bytes(b"A" * 1000)
    (root / "MEDIA" / "FILM" / "Film B (2002).mkv").write_bytes(b"B" * 1000)
    (root / "MEDIA" / "ADULTE" / "Titre C (2003).mkv").write_bytes(b"C" * 1000)
    (root / "MEDIA" / "SERIES" / "Serie X" / "S01E01.mkv").write_bytes(b"S" * 1000)
    monkeypatch.setattr(config.settings, "source_dirs_films", [str(films)])
    monkeypatch.setattr(config.settings, "source_dirs_adultes", [str(adultes)])
    monkeypatch.setattr(config.settings, "source_dirs_series", [])
    monkeypatch.setattr(config.settings, "source_dirs_ebooks", [])
    monkeypatch.setattr(config.settings, "dest_subdir_films", "MEDIA/FILM")
    monkeypatch.setattr(config.settings, "dest_subdir_series", "MEDIA/SERIES")
    monkeypatch.setattr(config.settings, "dest_subdir_ebooks", "MEDIA/EBOOKS")
    monkeypatch.setattr(config.settings, "dest_subdir_adultes", "MEDIA/ADULTE")
    monkeypatch.setattr(config.settings, "disk_folders", {})
    monkeypatch.setattr(config.settings, "dry_run", False)
    monkeypatch.setattr(config.settings, "safety_margin_bytes", 0)
    monkeypatch.setattr(disks_mod, "list_destination_disks", lambda: [{"path": str(root), "free_bytes": 10**12}])
    return {"films": films, "adultes": adultes, "root": root}


@pytest.fixture
def client(disque):
    from fastapi.testclient import TestClient

    from app import main

    return TestClient(main.app)


@pytest.fixture
def unlocked(disque):
    from fastapi.testclient import TestClient

    from app import main

    adult_auth.set_code("1234")
    client = TestClient(main.app)
    assert client.post("/api/adult/unlock", json={"code": "1234"}).status_code == 200
    return client


def _root(disque):
    return str(disque["root"])


def _set_folder(client, disque, kind, folder, create=True):
    return client.put("/api/disks/folder", json={"path": _root(disque), "kind": kind, "folder": folder, "create": create})


# --- Configuration ---------------------------------------------------------------

def test_dossier_par_disque_est_normalise():
    assert config._as_disk_folders({"/media/d/": {"films": "/Ciné/Films/"}}) == {"/media/d": {"films": "Ciné/Films"}}
    assert config._as_disk_folders({"/media/d": {"films": ""}}) == {}
    assert config._as_disk_folders(None) == {}


@pytest.mark.parametrize("valeur", [
    {"media/d": {"films": "x"}},          # racine relative
    {"/": {"films": "x"}},                # racine du système
    {"/media/d": {"musique": "x"}},       # type inconnu
    {"/media/d": {"films": "../x"}},      # sortie du disque
    {"/media/d": {"films": "a/./b"}},     # point
    ["pas", "un", "objet"],
])
def test_dossier_par_disque_refuse_les_valeurs_dangereuses(valeur):
    with pytest.raises(ValueError):
        config._as_disk_folders(valeur)


def test_subdir_for_prefere_le_dossier_du_disque(disque, monkeypatch):
    root = _root(disque)
    monkeypatch.setattr(config.settings, "disk_folders", {root: {"films": "Ciné/Films"}})
    assert config.settings.subdir_for(root, "films") == "Ciné/Films"
    assert config.settings.subdir_for(root, "series") == "MEDIA/SERIES"   # pas de choix : global
    assert config.settings.subdir_for(str(disque["films"]), "films") == "MEDIA/FILM"  # autre disque : global


def test_la_bibliotheque_reconnait_un_dossier_propre_au_disque(disque, monkeypatch):
    root = _root(disque)
    monkeypatch.setattr(config.settings, "disk_folders", {root: {"films": "Ciné/Films"}})
    assert disk_library.kind_dirs(root)["films"][0] == os.path.join(root, "Ciné/Films")
    rangé = os.path.join(root, "Ciné/Films", "Nouveau (2010).mkv")
    os.makedirs(os.path.dirname(rangé))
    open(rangé, "wb").close()
    assert disk_library.locate(rangé) == ("films", root)


# --- Parcours des dossiers --------------------------------------------------------

def test_parcours_liste_les_sous_dossiers_seulement(client, disque):
    root = disque["root"]
    (root / "Fichier.txt").write_text("x")
    (root / ".caché").mkdir()
    os.symlink(str(disque["films"]), str(root / "lien"))
    response = client.get("/api/disks/folders", params={"path": _root(disque)})
    assert response.status_code == 200, response.text
    noms = [f["name"] for f in response.json()["folders"]]
    assert noms == ["MEDIA"]


def test_parcours_descend_et_remonte(unlocked, disque):
    response = unlocked.get("/api/disks/folders", params={"path": _root(disque), "folder": "MEDIA"})
    body = response.json()
    assert body["folder"] == "MEDIA" and body["parent"] == ""
    assert {f["name"] for f in body["folders"]} == {"FILM", "FILMS", "SERIES", "ADULTE"}
    assert {f["folder"] for f in body["folders"]} >= {"MEDIA/FILM"}


def test_parcours_cache_le_dossier_adulte_sans_session(client, disque):
    response = client.get("/api/disks/folders", params={"path": _root(disque), "folder": "MEDIA"})
    assert "ADULTE" not in [f["name"] for f in response.json()["folders"]]
    assert client.get("/api/disks/folders", params={"path": _root(disque), "folder": "MEDIA/ADULTE"}).status_code == 404


def test_parcours_refuse_de_sortir_du_disque(client, disque):
    assert client.get("/api/disks/folders", params={"path": _root(disque), "folder": "../.."}).status_code == 400


def test_parcours_disque_hors_ligne_refuse(client, disque, monkeypatch):
    monkeypatch.setattr(disks_mod, "list_destination_disks", lambda: [])
    assert client.get("/api/disks/folders", params={"path": _root(disque)}).status_code == 403


# --- Choix du dossier de destination ----------------------------------------------

def test_choix_du_dossier_est_enregistre(client, disque):
    response = _set_folder(client, disque, "films", "Ciné/Films")
    assert response.status_code == 200, response.text
    assert response.json()["effective"] == "Ciné/Films"
    assert (disque["root"] / "Ciné" / "Films").is_dir()  # créé à la demande
    assert config.settings.disk_folders == {_root(disque): {"films": "Ciné/Films"}}


def test_choix_d_un_dossier_inexistant_sans_creation_est_refusé(client, disque):
    assert _set_folder(client, disque, "films", "Absent", create=False).status_code == 404
    assert not (disque["root"] / "Absent").exists()


def test_choix_en_simulation_ne_cree_rien(client, disque, monkeypatch):
    monkeypatch.setattr(config.settings, "dry_run", True)
    assert _set_folder(client, disque, "films", "Simulé/Films").status_code == 200
    assert not (disque["root"] / "Simulé").exists()


def test_dossier_vide_retablit_le_dossier_global(client, disque):
    _set_folder(client, disque, "films", "Ciné/Films")
    response = _set_folder(client, disque, "films", "")
    assert response.status_code == 200
    assert response.json()["effective"] == "MEDIA/FILM"
    assert config.settings.disk_folders == {}


def test_choix_refuse_un_lien_qui_sort_du_disque(client, disque, tmp_path):
    hors = tmp_path / "hors"
    hors.mkdir()
    os.symlink(str(hors), str(disque["root"] / "sortie"))
    assert _set_folder(client, disque, "films", "sortie").status_code == 400


def test_choix_refuse_un_dossier_source(client, disque, monkeypatch):
    src = disque["root"] / "Sources"
    src.mkdir()
    monkeypatch.setattr(config.settings, "source_dirs_films", [str(src)])
    assert _set_folder(client, disque, "films", "Sources/Sous").status_code == 403


def test_choix_du_dossier_adulte_refuse_sans_session(client, unlocked, disque):
    assert _set_folder(client, disque, "films", "MEDIA/ADULTE", create=False).status_code == 404
    assert _set_folder(unlocked, disque, "films", "MEDIA/ADULTE", create=False).status_code == 200


def test_copie_vers_le_disque_suit_le_dossier_choisi(client, disque, monkeypatch):
    _set_folder(client, disque, "films", "Ciné/Films")
    monkeypatch.setattr(main_module(), "_launch", lambda job: None)  # on regarde la destination, sans copier
    film = disque["films"] / "Base (2000).mkv"
    film.write_bytes(b"x" * 100)
    response = client.post("/api/jobs", json={"operation": "copy", "media_type": "films",
                                               "items": [str(film)], "destination": _root(disque)})
    assert response.status_code == 200, response.text
    assert jobs.get_job(response.json()["job_id"]).destination == os.path.join(_root(disque), "Ciné/Films")


def main_module():
    from app import main
    return main


# --- Déplacement dans le même disque ----------------------------------------------

def _move(client, paths, folder):
    return client.post("/api/disk-library/move", json={"paths": paths, "folder": folder})


def test_deplace_vers_un_autre_dossier_du_meme_disque(client, disque):
    old = disque["root"] / "MEDIA" / "FILM" / "Film A (2001).mkv"
    inode = os.stat(old).st_ino
    response = _move(client, [str(old)], "MEDIA/FILMS")
    assert response.status_code == 200, response.text
    new = disque["root"] / "MEDIA" / "FILMS" / "Film A (2001).mkv"
    assert response.json()["moved"] == [str(new)]
    assert new.exists() and not old.exists()
    assert os.stat(new).st_ino == inode  # un renommage : aucune copie


def test_deplacement_met_a_jour_la_bibliotheque(client, disque):
    old = str(disque["root"] / "MEDIA" / "FILM" / "Film B (2002).mkv")
    _move(client, [old], "MEDIA/FILMS")
    new = str(disque["root"] / "MEDIA" / "FILMS" / "Film B (2002).mkv")
    assert disk_library.locate(new) == ("films", _root(disque))


def test_deplace_vers_un_dossier_propre_au_disque(client, disque):
    _set_folder(client, disque, "films", "Classiques")
    old = disque["root"] / "MEDIA" / "FILM" / "Film A (2001).mkv"
    response = _move(client, [str(old)], "Classiques")
    assert response.status_code == 200, response.text
    assert (disque["root"] / "Classiques" / "Film A (2001).mkv").exists()


def test_deplacement_plusieurs_elements_d_un_coup(client, disque):
    paths = [str(disque["root"] / "MEDIA" / "FILM" / n) for n in ("Film A (2001).mkv", "Film B (2002).mkv")]
    response = _move(client, paths, "MEDIA/FILMS")
    assert response.status_code == 200 and len(response.json()["moved"]) == 2


def test_deplacement_refuse_un_dossier_non_reconnu(client, disque):
    (disque["root"] / "Divers").mkdir()
    old = disque["root"] / "MEDIA" / "FILM" / "Film A (2001).mkv"
    response = _move(client, [str(old)], "Divers")
    assert response.status_code == 400
    assert "n'est pas reconnu" in response.json()["detail"]
    assert old.exists()


def test_deplacement_refuse_de_sortir_du_disque(client, disque):
    old = disque["root"] / "MEDIA" / "FILM" / "Film A (2001).mkv"
    assert _move(client, [str(old)], "../autre").status_code == 400
    assert old.exists()


def test_deplacement_collision_sans_rien_deplacer(client, disque):
    (disque["root"] / "MEDIA" / "FILMS" / "Film A (2001).mkv").write_bytes(b"X")
    old = disque["root"] / "MEDIA" / "FILM" / "Film A (2001).mkv"
    response = _move(client, [str(old)], "MEDIA/FILMS")
    assert response.status_code == 409
    assert old.exists() and old.read_bytes() == b"A" * 1000


def test_deplacement_deja_au_bon_endroit_est_inchange(client, disque):
    old = str(disque["root"] / "MEDIA" / "FILM" / "Film A (2001).mkv")
    response = _move(client, [old], "MEDIA/FILM")
    assert response.status_code == 200
    assert response.json()["unchanged"] == 1 and response.json()["moved"] == []


def test_deplacement_refuse_un_melange_de_types(client, disque):
    paths = [str(disque["root"] / "MEDIA" / "FILM" / "Film A (2001).mkv"),
             str(disque["root"] / "MEDIA" / "SERIES" / "Serie X")]
    response = _move(client, paths, "MEDIA/FILMS")
    assert response.status_code == 400


def test_deplacement_interdit_si_allow_delete_coupe(client, disque, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_DELETE", False)
    old = disque["root"] / "MEDIA" / "FILM" / "Film A (2001).mkv"
    assert _move(client, [str(old)], "MEDIA/FILMS").status_code == 403
    assert old.exists()


def test_deplacement_dry_run_ne_bouge_rien_et_ne_trace_rien(client, disque, monkeypatch):
    monkeypatch.setattr(config.settings, "dry_run", True)
    old = disque["root"] / "MEDIA" / "FILM" / "Film A (2001).mkv"
    response = _move(client, [str(old)], "MEDIA/FILMS")
    assert response.status_code == 200 and response.json()["dry_run"] is True
    assert old.exists()
    assert disk_moves.list_entries()["total"] == 0


def test_deplacement_adulte_refuse_sans_session(client, unlocked, disque):
    adulte = disque["root"] / "MEDIA" / "ADULTE" / "Titre C (2003).mkv"
    assert _move(client, [str(adulte)], "MEDIA/FILM").status_code == 404
    assert adulte.exists()
    response = _move(unlocked, [str(adulte)], "MEDIA/FILM")
    assert response.status_code == 200, response.text
    assert (disque["root"] / "MEDIA" / "FILM" / "Titre C (2003).mkv").exists()


def test_deplacement_refuse_si_une_tache_utilise_l_element(client, disque):
    old = str(disque["root"] / "MEDIA" / "FILM" / "Film A (2001).mkv")
    jobs.JOBS["t1"] = jobs.Job("t1", "copy", "films", [old], str(disque["root"]), False)
    jobs.JOBS["t1"].status = "running"
    assert _move(client, [old], "MEDIA/FILMS").status_code == 409
    assert os.path.exists(old)


def test_deplacement_refuse_un_autre_volume(client, disque, monkeypatch):
    from app import disk_layout
    monkeypatch.setattr(disk_layout, "_meme_volume", lambda a, b: False)
    old = disque["root"] / "MEDIA" / "FILM" / "Film A (2001).mkv"
    response = _move(client, [str(old)], "MEDIA/FILMS")
    assert response.status_code == 409
    assert old.exists()


def test_deplacement_disque_hors_ligne_refuse(client, disque, monkeypatch):
    old = disque["root"] / "MEDIA" / "FILM" / "Film A (2001).mkv"
    monkeypatch.setattr(disks_mod, "list_destination_disks", lambda: [])
    assert _move(client, [str(old)], "MEDIA/FILMS").status_code == 403


# --- Suivi des copies et déplacements -----------------------------------------------

def _tache(items, operation="copy", dest_names=None, destination=None, adult=False, dest_dirs=None):
    return jobs.Job("job-test", operation, "films", items, destination, False,
                    dest_names=dest_names, dest_dirs=dest_dirs, adult=adult)


def test_suivi_enregistre_un_deplacement_dans_le_disque(client, disque):
    old = str(disque["root"] / "MEDIA" / "FILM" / "Film A (2001).mkv")
    _move(client, [old], "MEDIA/FILMS")
    entries = client.get("/api/disk-moves").json()["items"]
    assert len(entries) == 1
    entree = entries[0]
    assert entree["status"] == "ok" and entree["operation"] == "déplacement sur le disque"
    assert entree["destination"] == str(disque["root"] / "MEDIA" / "FILMS" / "Film A (2001).mkv")
    assert "sans copie" in entree["detail"]


def test_suivi_suit_les_evenements_d_une_copie(disque):
    src = disque["films"] / "Base (2000).mkv"
    src.write_bytes(b"x")
    tache = _tache([str(src)], destination=str(disque["root"]))
    tache.emit({"type": "item_start", "item": src.name})
    tache.emit({"type": "item_done", "item": src.name})
    entree = disk_moves.list_entries()["items"][0]
    assert entree["status"] == "ok" and entree["operation"] == "copie"
    assert entree["source"] == str(src)
    assert "SHA-256" in entree["detail"]


def test_suivi_un_deplacement_mentionne_le_retrait_de_la_source(disque):
    src = disque["films"] / "Base (2000).mkv"
    tache = _tache([str(src)], operation="move", destination=str(disque["root"]))
    tache.emit({"type": "item_done", "item": src.name})
    assert "source retirée" in disk_moves.list_entries()["items"][0]["detail"]


def test_suivi_erreur_et_ignore(disque):
    src = disque["films"] / "Base (2000).mkv"
    tache = _tache([str(src)], destination=str(disque["root"]))
    tache.emit({"type": "item_error", "item": src.name, "error": "rsync exit code 23"})
    assert disk_moves.list_entries(status="erreur")["items"][0]["detail"] == "rsync exit code 23"
    autre = disque["films"] / "Autre.mkv"
    tache2 = _tache([str(autre)], destination=str(disque["root"]))
    tache2.emit({"type": "item_skipped", "item": autre.name, "message": "Déjà présent à l'identique."})
    assert disk_moves.list_entries(status="ignore")["items"][0]["status_label"] == "Ignoré"


def test_suivi_utilise_le_nom_de_la_copie(disque):
    src = disque["films"] / "Base (2000).mkv"
    tache = _tache([str(src)], destination=str(disque["root"]), dest_names={str(src): "Titre (2000).mkv"})
    tache.emit({"type": "item_done", "item": src.name})
    assert disk_moves.list_entries()["items"][0]["destination"].endswith("Titre (2000).mkv")


def test_suivi_masque_les_adultes_sans_session(client, unlocked, disque):
    src = disque["adultes"] / "Titre C (2003).mkv"
    tache = _tache([str(src)], destination=str(disque["root"]), adult=True)
    tache.emit({"type": "item_done", "item": src.name})
    tache2 = _tache([str(disque["films"] / "Film.mkv")], destination=str(disque["root"]))
    tache2.emit({"type": "item_done", "item": "Film.mkv"})
    cache = client.get("/api/disk-moves").json()
    assert cache["total"] == 1 and [e["name"] for e in cache["items"]] == ["Film.mkv"]
    voit = unlocked.get("/api/disk-moves").json()
    assert voit["total"] == 2 and {e["name"] for e in voit["items"]} == {"Titre C (2003).mkv", "Film.mkv"}


def test_suivi_ignore_la_simulation(disque, monkeypatch):
    monkeypatch.setattr(config.settings, "dry_run", True)
    src = disque["films"] / "Base (2000).mkv"
    _tache([str(src)], destination=str(disque["root"])).emit({"type": "item_done", "item": src.name})
    assert disk_moves.list_entries()["total"] == 0


def test_suivi_filtres_et_compteurs(disque):
    for statut in ("ok", "erreur", "en_cours", "ignore"):
        disk_moves.record("copy", "films", f"/s/{statut}.mkv", f"/d/{statut}.mkv", statut)
    assert {e["status"] for e in disk_moves.list_entries(status="problèmes")["items"]} == {"erreur"}
    assert {e["status"] for e in disk_moves.list_entries(status="terminés")["items"]} == {"ok", "erreur", "ignore"}
    compte = disk_moves.list_entries()["counts"]
    assert compte["ok"] == 1 and compte["en_cours"] == 1 and compte["erreur"] == 1 and compte["ignore"] == 1


def test_suivi_au_demarrage_marque_interrompu_ce_qui_etait_en_cours(disque):
    disk_moves.record("copy", "films", "/s/a.mkv", "/d/a.mkv", "en_cours")
    disk_moves.record("copy", "films", "/s/b.mkv", "/d/b.mkv", "ok")
    assert disk_moves.marquer_interrompus() == 1
    statuts = {e["name"]: e["status"] for e in disk_moves.list_entries()["items"]}
    assert statuts == {"a.mkv": "interrompu", "b.mkv": "ok"}


def test_suivi_effacer_l_historique_garde_les_transferts_en_cours(client, disque):
    disk_moves.record("copy", "films", "/s/a.mkv", "/d/a.mkv", "en_cours")
    disk_moves.record("copy", "films", "/s/b.mkv", "/d/b.mkv", "ok")
    response = client.post("/api/disk-moves/clear")
    assert response.json() == {"removed": 1}
    assert [e["name"] for e in disk_moves.list_entries()["items"]] == ["a.mkv"]


def test_suivi_plafond_retire_les_plus_anciennes_entrees_terminees(monkeypatch):
    monkeypatch.setattr(disk_moves, "HISTORY_MAX", 3)
    disk_moves.record("copy", "films", "/s/en.mkv", "/d/en.mkv", "en_cours")
    for i in range(5):
        disk_moves.record("copy", "films", f"/s/{i}.mkv", f"/d/{i}.mkv", "ok")
    entrees = disk_moves.list_entries()["items"]
    assert len(entrees) == 3
    assert "en.mkv" in [e["name"] for e in entrees]  # un transfert en cours n'est jamais retiré


def test_suivi_trace_la_recategorisation_adulte(unlocked, client, disque):
    old = str(disque["root"] / "MEDIA" / "FILM" / "Film A (2001).mkv")
    response = unlocked.post("/api/disk-library/category", json={"paths": [old], "adult": True})
    assert response.status_code == 200, response.text
    assert unlocked.get("/api/disk-moves").json()["total"] == 1
    assert client.get("/api/disk-moves").json()["total"] == 0  # trace adulte masquée sans session


def test_copie_reelle_laisse_une_trace_verifiee(disque, monkeypatch, client):
    monkeypatch.setattr(scheduler, "active_window",
                        lambda now=None: {"name": "test", "start": "00:00", "end": "23:59",
                                          "onefichier_mbps": 0, "disk_mbps": 0})
    monkeypatch.setattr(main_module(), "_launch", lambda job: None)
    film = disque["films"] / "Base (2000).mkv"
    film.write_bytes(b"x" * 4096)
    response = client.post("/api/jobs", json={"operation": "copy", "media_type": "films",
                                               "items": [str(film)], "destination": _root(disque)})
    assert response.status_code == 200, response.text
    job = jobs.get_job(response.json()["job_id"])
    asyncio.run(jobs.run_job(job))
    copie = disque["root"] / "MEDIA" / "FILM" / "Base (2000).mkv"
    assert copie.exists()
    entree = disk_moves.list_entries()["items"][0]
    assert entree["status"] == "ok" and entree["destination"] == str(copie)
    assert "SHA-256" in entree["detail"]
    assert film.exists()  # une copie laisse la source


def test_fichier_de_suivi_est_du_json_lisible(disque):
    disk_moves.record("copy", "films", "/s/a.mkv", "/d/a.mkv", "ok")
    with open(disk_moves._path(), encoding="utf-8") as fh:
        data = json.load(fh)
    assert isinstance(data, list) and data[0]["name"] == "a.mkv"


def test_bibliotheque_expose_les_destinations_et_masque_l_adulte(client, unlocked, disque, monkeypatch):
    root = _root(disque)
    monkeypatch.setattr(config.settings, "disk_folders", {root: {"films": "Ciné/Films"}})
    sans_session = client.get("/api/disk-library").json()["disks"][0]
    assert sans_session["destinations"]["films"] == {"folder": "Ciné/Films", "custom": True}
    assert sans_session["destinations"]["series"] == {"folder": "MEDIA/SERIES", "custom": False}
    assert not any(dossier.endswith("ADULTE") for dossier in sans_session["subdirs"]["films"])
    avec_session = unlocked.get("/api/disk-library").json()["disks"][0]
    assert any(p.endswith("ADULTE") for p in avec_session["subdirs"]["films"])


def test_routes_de_modification_protegees_par_le_jeton(client, disque, monkeypatch):
    """Les routes ajoutées en version 48 qui modifient quelque chose exigent le jeton d'accès."""
    monkeypatch.setattr(config, "ACCESS_TOKEN", "JETON-DE-TEST")
    root = _root(disque)
    old = str(disque["root"] / "MEDIA" / "FILM" / "Film A (2001).mkv")
    assert client.put("/api/disks/folder", json={"path": root, "kind": "films", "folder": "X", "create": True}).status_code == 401
    assert client.post("/api/disk-library/move", json={"paths": [old], "folder": "MEDIA/FILMS"}).status_code == 401
    assert client.post("/api/disk-moves/clear").status_code == 401
    assert os.path.exists(old)
    # Lecture libre : le suivi et le parcours restent consultables sans jeton.
    assert client.get("/api/disk-moves").status_code == 200
    assert client.get("/api/disks/folders", params={"path": root}).status_code == 200
    avec_jeton = {"X-Access-Token": "JETON-DE-TEST"}
    assert client.post("/api/disk-library/move", json={"paths": [old], "folder": "MEDIA/FILMS"}, headers=avec_jeton).status_code == 200
