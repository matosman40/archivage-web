"""Médias adultes (versions 34 et 36) : classement, session déverrouillée, scraping TMDB, destinations, routes."""
import asyncio
import json
import os

import pytest

from app import adult_auth, adult_media, config, disk_library, disks, media, onefichier, tmdb


@pytest.fixture(autouse=True)
def _registre_des_taches_isole(monkeypatch):
    """Une tâche créée par un test reste « en attente » dans jobs.JOBS et bloquerait les
    tests suivants (l'intégrité des disques refuse de tourner pendant une copie)."""
    from app import jobs

    monkeypatch.setattr(jobs, "JOBS", {})


@pytest.fixture(autouse=True)
def _sessions_isolees(monkeypatch):
    """Code et sessions adultes propres à chaque test ; PBKDF2 allégé (ces tests ne mesurent pas la force)."""
    adult_auth.reset()
    monkeypatch.setattr(adult_auth, "ITERATIONS", 1000)
    yield
    adult_auth.reset()


@pytest.fixture
def library(env, monkeypatch, tmp_path):
    """Un dossier films, un dossier adulte, un disque avec MEDIA/FILM et MEDIA/ADULTE."""
    films = tmp_path / "films"
    adults = tmp_path / "adultes"
    films.mkdir()
    adults.mkdir()
    (films / "Film A (2001).mkv").write_bytes(b"A" * 1000)
    (films / "Film B (2002).mkv").write_bytes(b"B" * 1000)
    (adults / "Titre C (2003).mkv").write_bytes(b"C" * 1000)
    disk = tmp_path / "disque"
    (disk / "MEDIA" / "FILM").mkdir(parents=True)
    (disk / "MEDIA" / "ADULTE").mkdir(parents=True)
    (disk / "MEDIA" / "ADULTE" / "Titre C (2003).mkv").write_bytes(b"C" * 1000)
    (disk / "MEDIA" / "FILM" / "Film A (2001).mkv").write_bytes(b"A" * 1000)
    monkeypatch.setattr(config.settings, "source_dirs_films", [str(films)])
    monkeypatch.setattr(config.settings, "source_dirs_adultes", [str(adults)])
    monkeypatch.setattr(config.settings, "source_dirs_series", [])
    monkeypatch.setattr(config.settings, "source_dirs_ebooks", [])
    monkeypatch.setattr(config.settings, "dest_subdir_adultes", "MEDIA/ADULTE")
    monkeypatch.setattr(disks, "list_destination_disks", lambda: [{"path": str(disk), "free_bytes": 10**12}])
    return {"films": films, "adults": adults, "disk": disk}


# --- Réglages ----------------------------------------------------------

def test_reglages_par_defaut():
    assert "adult_visible" not in config._FIELD_DEFS  # remplacé par le code d'accès (v36)
    assert config._FIELD_DEFS["source_dirs_adultes"][0]() == []
    assert config._FIELD_DEFS["dest_subdir_adultes"][0]() == "MEDIA/ADULTE"


def test_dossiers_adultes_sont_des_sources_pour_la_securite(library):
    assert str(library["adults"]) in config.settings.all_source_dirs


def test_avertissement_de_chevauchement_inclut_les_adultes(library, monkeypatch):
    from app.main import _detect_source_overlap

    monkeypatch.setattr(config.settings, "source_dirs_adultes", [str(library["films"]) + "/sous"])
    warnings = _detect_source_overlap(config.settings)
    assert any("films adultes" in w for w in warnings)


# --- Classement et affichage --------------------------------------------

def test_classement_par_dossier_et_par_marque(library):
    adult_file = str(library["adults"] / "Titre C (2003).mkv")
    film_file = str(library["films"] / "Film A (2001).mkv")
    assert adult_media.is_adult_path(adult_file)
    assert not adult_media.is_adult_path(film_file)
    adult_media.set_marked([film_file], True)
    assert adult_media.is_adult_path(film_file)
    adult_media.set_marked([film_file], False)
    assert not adult_media.is_adult_path(film_file)


def test_marques_stockees_dans_un_fichier_a_part(library):
    film_file = str(library["films"] / "Film B (2002).mkv")
    adult_media.set_marked([film_file], True)
    stored = json.loads(open(adult_media._path(), encoding="utf-8").read())
    assert stored == {"paths": [os.path.normpath(film_file)]}
    assert os.path.basename(adult_media._path()) == "library-adult.json"


def test_dossier_de_copie_adulte(library):
    assert adult_media.is_adult_folder("MEDIA/ADULTE")
    assert adult_media.is_adult_folder("MEDIA/ADULTE/sous")
    assert not adult_media.is_adult_folder("MEDIA/ADULTEX")  # pas de préfixe trompeur
    assert not adult_media.is_adult_folder("MEDIA/FILM")


def test_masquage_selon_la_session(library):
    item = {"name": "x", "adult": True}
    assert not adult_media.visible(item, False)
    assert adult_media.visible({"name": "y"}, False)  # un film non adulte reste visible
    assert adult_media.visible(item, True)
    assert adult_media.filter_visible([item, {"name": "y"}], False) == [{"name": "y"}]
    assert adult_media.filter_visible([item, {"name": "y"}], True) == [item, {"name": "y"}]


def test_entree_1fichier_ancienne_reste_masquee_par_son_chemin_d_origine(library):
    # Entrée créée avant le drapeau « adult » (v34) : son chemin d'origine suffit à la classer.
    entry = {"key": "1", "original_path": str(library["adults"] / "Titre C (2003).mkv")}
    assert adult_media.is_adult_item(entry)
    assert not adult_media.visible(entry, False)


def test_chemins_masques_refuses_sans_session(library):
    adult_path = str(library["adults"] / "Titre C (2003).mkv")
    film_path = str(library["films"] / "Film A (2001).mkv")
    assert adult_media.hidden_paths([adult_path, film_path], False) == [adult_path]
    assert adult_media.hidden_paths([adult_path], True) == []


# --- Scan des films ------------------------------------------------------

def test_scan_des_films_marque_les_adultes_sans_doublon(library):
    # Le dossier adulte est aussi listé comme dossier films : un fichier n'apparaît qu'une fois.
    config.settings.source_dirs_films.append(str(library["adults"]))
    try:
        items = media.search_movies("")
    finally:
        config.settings.source_dirs_films.pop()
    names = sorted((it["name"], it["adult"]) for it in items)
    assert names == [("Film A (2001).mkv", False), ("Film B (2002).mkv", False),
                     ("Titre C (2003).mkv", True)]


def test_scan_marque_une_film_marque_a_la_main(library):
    film_file = str(library["films"] / "Film B (2002).mkv")
    adult_media.set_marked([film_file], True)
    flags = {it["name"]: it["adult"] for it in media.search_movies("")}
    assert flags["Film B (2002).mkv"] is True and flags["Film A (2001).mkv"] is False


# --- Scraping TMDB ----------------------------------------------------------

class _Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, client, path, params):
        self.calls.append(dict(params))
        return {"results": []}


@pytest.fixture
def tmdb_calls(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(tmdb, "_get_json", recorder)
    tmdb._match_cache.clear()
    yield recorder
    tmdb._match_cache.clear()


def test_recherche_tmdb_inclut_les_titres_adultes_seulement_si_demande(tmdb_calls):
    asyncio.run(tmdb._search_one(None, "movie", "Titre", None, adult=True))
    asyncio.run(tmdb._search_one(None, "movie", "Titre", None))
    assert [c["include_adult"] for c in tmdb_calls.calls] == ["true", "false"]


def test_cache_tmdb_separe_adulte_et_non_adulte(tmdb_calls):
    asyncio.run(tmdb._search_one(None, "movie", "Titre", None, adult=True))
    asyncio.run(tmdb._search_one(None, "movie", "Titre", None, adult=False))
    assert len(tmdb_calls.calls) == 2  # pas de réponse réutilisée d'une catégorie à l'autre


def test_find_movie_et_search_transmettent_le_drapeau(tmdb_calls, env):
    asyncio.run(tmdb.find_movie("Titre (2003)", adult=True))
    asyncio.run(tmdb.search("movie", "Titre", adult=True))
    # find_movie relance sans l'année si rien ne sort : au moins deux appels, tous avec les titres adultes
    assert len(tmdb_calls.calls) >= 2
    assert {c["include_adult"] for c in tmdb_calls.calls} == {"true"}


def test_enrichissement_passe_le_drapeau_par_element(monkeypatch, env):
    seen = []

    async def fake_search(client, kind, title, year, adult=False):
        seen.append((title, adult))
        return {"id": 1, "poster_url": None, "genres": [], "rating": None, "summary": None, "year": None}

    async def no_genres(client, kind):
        return None

    monkeypatch.setattr(tmdb, "_search_one", fake_search)
    monkeypatch.setattr(tmdb, "_ensure_genre_cache", no_genres)
    items = [{"name": "Titre Adulte (2010)", "adult": True}, {"name": "Film Normal (2000)"}]
    asyncio.run(tmdb.enrich(items, "movie"))
    assert sorted(seen) == [("Film Normal", False), ("Titre Adulte", True)]


# --- Destinations ------------------------------------------------------------

def test_copie_sur_disque_part_dans_le_sous_dossier_adulte(library):
    disk = str(library["disk"])
    assert onefichier._disk_dest(onefichier.KIND_FILM, disk, adult=True) == os.path.join(disk, "MEDIA", "ADULTE")
    assert onefichier._disk_dest(onefichier.KIND_FILM, disk) == os.path.join(disk, "MEDIA", "FILM")


def test_restauration_adulte_revient_dans_le_dossier_adulte(library, monkeypatch):
    disk = str(library["disk"])
    entry = {"kind": onefichier.KIND_FILM, "adult": True, "original_path": "/x/Titre C (2003).mkv"}
    parent = onefichier.restore_parent(entry, "disk", disk)
    assert parent == os.path.join(disk, "MEDIA", "ADULTE")


def test_sous_dossier_adulte_vide_retombe_sur_les_films(library, monkeypatch):
    monkeypatch.setattr(config.settings, "dest_subdir_adultes", "")
    assert onefichier._subdir_for(onefichier.KIND_FILM, True) == config.settings.dest_subdir_films


def test_disque_scanne_classe_les_copies_adultes(library):
    disk = next(d for d in disk_library.refresh(force=True)
                if d["path"] == os.path.normpath(str(library["disk"])))
    films = {it["name"]: it["adult"] for it in disk["items"]["films"]}
    assert films == {"Film A (2001).mkv": False, "Titre C (2003).mkv": True}


def test_dossiers_films_du_disque_incluent_le_dossier_adulte(library):
    dirs = disk_library.kind_dirs(str(library["disk"]))["films"]
    assert os.path.join(str(library["disk"]), "MEDIA", "ADULTE") in dirs


# --- Routes ------------------------------------------------------------------

@pytest.fixture
def client(library, monkeypatch):
    """Client SANS session : celui d'un visiteur qui ne connaît pas le code."""
    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.setattr(main, "log_action", lambda *args, **kwargs: None)
    return TestClient(main.app)


@pytest.fixture
def unlocked_client(library, monkeypatch):
    """Client déverrouillé : code « 1234 », session ouverte par la route de déverrouillage."""
    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.setattr(main, "log_action", lambda *args, **kwargs: None)
    adult_auth.set_code("1234")
    client = TestClient(main.app)
    assert client.post("/api/adult/unlock", json={"code": "1234"}).status_code == 200
    return client


def test_route_recherche_masque_les_adultes(library, client, unlocked_client, monkeypatch):
    from app import scan_cache

    items = media.search_movies("")

    async def fake_search(kind, term):
        return items, {"built_at": 1, "refreshing": False}

    monkeypatch.setattr(scan_cache, "search", fake_search)
    names = lambda r: sorted(i["name"] for i in r.json()["items"])  # noqa: E731
    cached = client.post("/api/search", json={"media_type": "films", "term": ""})
    assert names(cached) == ["Film A (2001).mkv", "Film B (2002).mkv"]
    shown = unlocked_client.post("/api/search", json={"media_type": "films", "term": ""})
    assert "Titre C (2003).mkv" in names(shown)


def test_plan_films_retire_les_adultes_masques(library, client):
    adult_path = str(library["adults"] / "Titre C (2003).mkv")
    film_path = str(library["films"] / "Film A (2001).mkv")
    response = client.post("/api/plan/films", json={"mode": "files", "paths": [adult_path, film_path]})
    plan = response.json()
    assert plan["items"] == [film_path]
    # Version 42 : aucun avertissement ne compte les adultes masqués (il révélerait leur présence).
    assert not any("adulte" in w for w in plan["warnings"])


def test_creation_de_tache_refuse_un_adulte_masque(library, client, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "_launch", lambda job: None)
    adult_path = str(library["adults"] / "Titre C (2003).mkv")
    response = client.post("/api/jobs", json={"operation": "copy", "media_type": "films",
                                              "items": [adult_path], "destination": str(library["disk"])})
    assert response.status_code == 404  # comme s'il n'existait pas
    assert "introuvable" in response.json()["detail"]


def test_creation_de_tache_envoie_l_adulte_dans_son_dossier(library, unlocked_client, monkeypatch):
    from app import jobs, main

    monkeypatch.setattr(main, "_launch", lambda job: None)  # pas de copie réelle dans un test
    captured = {}
    real_create = jobs.create_job

    def spy(**kwargs):
        captured.update(kwargs)
        return real_create(**kwargs)

    monkeypatch.setattr(jobs, "create_job", spy)
    adult_path = str(library["adults"] / "Titre C (2003).mkv")
    film_path = str(library["films"] / "Film A (2001).mkv")
    response = unlocked_client.post("/api/jobs", json={"operation": "copy", "media_type": "films",
                                                       "items": [adult_path, film_path],
                                                       "destination": str(library["disk"])})
    assert response.status_code == 200
    assert captured["adult"] is True  # la tâche est masquée des listes pour une session verrouillée
    assert captured["dest_dirs"][adult_path] == os.path.join(str(library["disk"]), "MEDIA", "ADULTE")
    assert film_path not in captured["dest_dirs"]  # le film reste dans son dossier habituel


def test_identification_marque_le_film_et_respecte_le_masquage(library, client):
    film_path = str(library["films"] / "Film B (2002).mkv")
    response = client.post("/api/library/identify", json={
        "kind": "films", "paths": [film_path], "new_name": "Film B (2002)", "adult": True})
    assert response.status_code == 200 and response.json()["adult"] is True
    assert adult_media.is_adult_path(film_path)
    again = client.post("/api/library/identify", json={
        "kind": "films", "paths": [film_path], "new_name": "Film B (2002)"})
    assert again.status_code == 404  # masqué : impossible à renommer sans session déverrouillée


def test_recherche_tmdb_route_transmet_adult(library, unlocked_client, monkeypatch):
    seen = {}

    async def fake_search(kind, query, year=None, limit=8, adult=False):
        seen["adult"] = adult
        return []

    monkeypatch.setattr(tmdb, "search", fake_search)
    unlocked_client.get("/api/tmdb/search", params={"kind": "films", "q": "Titre", "adult": "true"})
    assert seen["adult"] is True


def test_recherche_tmdb_adulte_refusee_sans_session(library, client, monkeypatch):
    called = []

    async def fake_search(*args, **kwargs):
        called.append(True)
        return []

    monkeypatch.setattr(tmdb, "search", fake_search)
    response = client.get("/api/tmdb/search", params={"kind": "films", "q": "Titre", "adult": "true"})
    assert response.status_code == 403
    assert called == []  # aucun titre adulte n'est cherché pour une session verrouillée


# --- Actions sur les éléments adultes, sans session -------------------------

def test_fiche_1fichier_adulte_ne_revele_pas_le_lien_sans_session(library, client, monkeypatch):
    entry = {"key": "603", "original_path": str(library["adults"] / "Titre C (2003).mkv"), "status": "done"}
    monkeypatch.setattr(onefichier, "get_entry", lambda key: entry)
    monkeypatch.setattr(onefichier, "reveal_entry", lambda key: pytest.fail("lien révélé sans session"))
    response = client.get("/api/1fichier/entries/603/access")
    assert response.status_code == 404


def test_export_index_refuse_si_adultes_et_sans_session(library, client, monkeypatch):
    monkeypatch.setattr(onefichier, "list_entries", lambda: [
        {"key": "603", "original_path": str(library["adults"] / "Titre C (2003).mkv")}])
    response = client.post("/api/1fichier/index/export", json={"passphrase": "x" * 12})
    assert response.status_code == 403


def test_export_index_refuse_sans_session_meme_sans_adultes(library, client, monkeypatch):
    # Version 42 : le refus ne dépend plus du contenu de l'index (sinon il révélerait la présence d'adultes).
    monkeypatch.setattr(onefichier, "list_entries", lambda: [{"key": "1", "original_path": "/films/A.mkv"}])

    def export_factice(passphrase):
        raise AssertionError("l'export ne doit pas être atteint sans session")

    monkeypatch.setattr(onefichier, "export_index", export_factice)
    response = client.post("/api/1fichier/index/export", json={"passphrase": "x" * 12})
    assert response.status_code == 403


def test_suppression_disque_adulte_refusee_sans_session(library, client, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "_launch", lambda job: None)
    adult_item = str(library["disk"] / "MEDIA" / "ADULTE" / "Titre C (2003).mkv")
    response = client.post("/api/disk-library/delete", json={
        "items": [adult_item], "confirm_delete_text": "SUPPRIMER"})
    assert response.status_code == 404
    assert os.path.exists(adult_item)  # rien n'a été supprimé


def test_renommage_disque_adulte_refuse_sans_session(library, client):
    adult_item = str(library["disk"] / "MEDIA" / "ADULTE" / "Titre C (2003).mkv")
    response = client.post("/api/disk-library/rename", json={"path": adult_item, "new_name": "Autre"})
    assert response.status_code == 404
    assert os.path.exists(adult_item)


def test_journal_masque_les_lignes_qui_citent_un_adulte(library, client, unlocked_client, monkeypatch):
    from app import main, scan_cache

    adult_name = "Titre C (2003).mkv"
    lines = [{"timestamp": "t1", "status": "OK", "operation": "COPIE",
              "detail": f"{library['films'] / 'Film A (2001).mkv'} -> disque"},
             {"timestamp": "t2", "status": "OK", "operation": "COPIE",
              "detail": f"{library['adults'] / adult_name} -> disque"}]
    monkeypatch.setattr(main, "read_log", lambda limit: list(lines))
    monkeypatch.setattr(scan_cache, "cached_items", lambda kind: [
        {"name": adult_name, "path": str(library["adults"] / adult_name), "adult": True}] if kind == "films" else [])
    locked = client.get("/api/logs").json()["entries"]
    assert [e["timestamp"] for e in locked] == ["t1"]          # la ligne adulte est retirée
    unlocked = unlocked_client.get("/api/logs").json()["entries"]
    assert [e["timestamp"] for e in unlocked] == ["t1", "t2"]  # déverrouillé : tout est là, dans l'ordre du journal


def test_taches_adultes_absentes_de_la_liste_sans_session(library, client, unlocked_client):
    from app import jobs

    adult_job = jobs.create_job("copy", "films", [str(library["adults"] / "Titre C (2003).mkv")], adult=True)
    film_job = jobs.create_job("copy", "films", [str(library["films"] / "Film A (2001).mkv")])
    locked_ids = [j["id"] for j in client.get("/api/jobs").json()["jobs"]]
    assert adult_job.id not in locked_ids and film_job.id in locked_ids
    unlocked_ids = [j["id"] for j in unlocked_client.get("/api/jobs").json()["jobs"]]
    assert adult_job.id in unlocked_ids
    assert client.get(f"/api/jobs/{adult_job.id}").status_code == 404
    assert unlocked_client.get(f"/api/jobs/{adult_job.id}").status_code == 200
