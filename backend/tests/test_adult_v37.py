"""Catégorie Adultes, déplacement, renommage, dossier 1fichier ADULTES et cache TMDB (version 37)."""
import asyncio
import os

import pytest

from app import adult_auth, adult_media, config, disks, identities, jobs, media, onefichier, tmdb


@pytest.fixture(autouse=True)
def _isolation(monkeypatch):
    """Code, sessions, tâches et cache TMDB propres à chaque test ; PBKDF2 allégé."""
    adult_auth.reset()
    monkeypatch.setattr(adult_auth, "ITERATIONS", 1000)
    monkeypatch.setattr(jobs, "JOBS", {})
    monkeypatch.setattr(tmdb, "_search_cache", {})
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
    (disk / "MEDIA" / "FILM" / "Film A (2001).mkv").write_bytes(b"A" * 1000)
    (disk / "MEDIA" / "ADULTE" / "Titre C (2003).mkv").write_bytes(b"C" * 1000)
    monkeypatch.setattr(config.settings, "source_dirs_films", [str(films)])
    monkeypatch.setattr(config.settings, "source_dirs_adultes", [str(adults)])
    monkeypatch.setattr(config.settings, "source_dirs_series", [])
    monkeypatch.setattr(config.settings, "source_dirs_ebooks", [])
    monkeypatch.setattr(config.settings, "dest_subdir_films", "MEDIA/FILM")
    monkeypatch.setattr(config.settings, "dest_subdir_adultes", "MEDIA/ADULTE")
    monkeypatch.setattr(disks, "list_destination_disks", lambda: [{"path": str(disk), "free_bytes": 10**12}])
    return {"films": films, "adults": adults, "disk": disk}


@pytest.fixture
def client(library, monkeypatch):
    """Visiteur sans session."""
    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.setattr(main, "log_action", lambda *args, **kwargs: None)
    return TestClient(main.app)


@pytest.fixture
def unlocked_client(library, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.setattr(main, "log_action", lambda *args, **kwargs: None)
    adult_auth.set_code("1234")
    client = TestClient(main.app)
    assert client.post("/api/adult/unlock", json={"code": "1234"}).status_code == 200
    return client


def _fake_scan(monkeypatch):
    """La recherche relit les marques à chaque appel, comme le ferait le scan réel."""
    from app import scan_cache

    async def fake_search(kind, term):
        return media.search_movies(term), {"built_at": 1, "refreshing": False}

    monkeypatch.setattr(scan_cache, "search", fake_search)


def _names(response):
    return sorted(i["name"] for i in response.json()["items"])


# --- Catégorie Adultes (recherche) -------------------------------------------

def test_categorie_adultes_verrouillee_ne_renvoie_rien(library, client, monkeypatch):
    _fake_scan(monkeypatch)
    response = client.post("/api/search", json={"media_type": "adultes", "term": ""})
    assert response.status_code == 200 and response.json()["items"] == []


def test_categorie_adultes_deverrouillee_ne_renvoie_que_les_adultes(library, unlocked_client, monkeypatch):
    _fake_scan(monkeypatch)
    response = unlocked_client.post("/api/search", json={"media_type": "adultes", "term": ""})
    assert _names(response) == ["Titre C (2003).mkv"]


def test_type_de_recherche_inconnu_refuse(client):
    assert client.post("/api/search", json={"media_type": "inconnu", "term": ""}).status_code == 422


# --- Déplacer vers la catégorie Adultes (serveur) -----------------------------

def test_categorie_serveur_refusee_sans_session_comme_absente(library, client):
    film = str(library["films"] / "Film A (2001).mkv")
    response = client.post("/api/library/category", json={"paths": [film], "adult": True})
    assert response.status_code == 404
    assert not adult_media.is_adult_path(film)


def test_categorie_serveur_marque_puis_retire(library, unlocked_client, monkeypatch):
    _fake_scan(monkeypatch)
    film = str(library["films"] / "Film A (2001).mkv")
    assert unlocked_client.post("/api/library/category", json={"paths": [film], "adult": True}).status_code == 200
    assert adult_media.is_adult_path(film)
    assert "Film A (2001).mkv" in _names(unlocked_client.post("/api/search", json={"media_type": "adultes", "term": ""}))
    assert unlocked_client.post("/api/library/category", json={"paths": [film], "adult": False}).status_code == 200
    assert not adult_media.is_adult_path(film)
    assert os.path.exists(film)  # le fichier ne bouge pas sur le serveur


def test_sortie_refusee_pour_un_film_du_dossier_adulte(library, unlocked_client):
    adult_file = str(library["adults"] / "Titre C (2003).mkv")
    response = unlocked_client.post("/api/library/category", json={"paths": [adult_file], "adult": False})
    assert response.status_code == 409
    assert adult_media.is_adult_path(adult_file)


# --- Déplacer sur un disque : même disque, sous-dossier adulte -----------------

def test_disque_deplace_un_film_dans_le_dossier_adulte(library, unlocked_client):
    disk = library["disk"]
    old = str(disk / "MEDIA" / "FILM" / "Film A (2001).mkv")
    response = unlocked_client.post("/api/disk-library/category", json={"paths": [old], "adult": True})
    assert response.status_code == 200, response.text
    new = disk / "MEDIA" / "ADULTE" / "Film A (2001).mkv"
    assert response.json()["moved"] == [str(new)]
    assert new.exists() and not os.path.exists(old)
    assert new.read_bytes() == b"A" * 1000  # déplacé, pas réécrit


def test_disque_sort_un_film_du_dossier_adulte(library, unlocked_client):
    disk = library["disk"]
    old = str(disk / "MEDIA" / "ADULTE" / "Titre C (2003).mkv")
    response = unlocked_client.post("/api/disk-library/category", json={"paths": [old], "adult": False})
    assert response.status_code == 200, response.text
    assert (disk / "MEDIA" / "FILM" / "Titre C (2003).mkv").exists()


def test_disque_cree_le_dossier_adulte_si_absent(library, unlocked_client):
    disk = library["disk"]
    (disk / "MEDIA" / "ADULTE" / "Titre C (2003).mkv").unlink()
    os.rmdir(disk / "MEDIA" / "ADULTE")  # un disque neuf n'a pas encore ce dossier
    old = str(disk / "MEDIA" / "FILM" / "Film A (2001).mkv")
    assert unlocked_client.post("/api/disk-library/category", json={"paths": [old], "adult": True}).status_code == 200
    assert (disk / "MEDIA" / "ADULTE" / "Film A (2001).mkv").exists()


def test_disque_categorie_verrouillee_comme_absente(library, client):
    disk = library["disk"]
    old = str(disk / "MEDIA" / "FILM" / "Film A (2001).mkv")
    assert client.post("/api/disk-library/category", json={"paths": [old], "adult": True}).status_code == 404
    assert os.path.exists(old)


def test_disque_collision_refusee_sans_rien_deplacer(library, unlocked_client):
    disk = library["disk"]
    (disk / "MEDIA" / "ADULTE" / "Film A (2001).mkv").write_bytes(b"X")
    old = str(disk / "MEDIA" / "FILM" / "Film A (2001).mkv")
    response = unlocked_client.post("/api/disk-library/category", json={"paths": [old], "adult": True})
    assert response.status_code == 409
    assert os.path.exists(old)


def test_disque_dry_run_ne_deplace_rien(library, unlocked_client, monkeypatch):
    monkeypatch.setattr(config.settings, "dry_run", True)
    disk = library["disk"]
    old = str(disk / "MEDIA" / "FILM" / "Film A (2001).mkv")
    response = unlocked_client.post("/api/disk-library/category", json={"paths": [old], "adult": True})
    assert response.status_code == 200 and response.json()["dry_run"] is True
    assert os.path.exists(old)


# --- Renommer un film sur le serveur -----------------------------------------

def _identify(client, body):
    return client.post("/api/library/identify", json=body)


def test_renommage_logique_par_defaut_ne_touche_pas_le_fichier(library, client):
    film = str(library["films"] / "Film B (2002).mkv")
    response = _identify(client, {"kind": "films", "paths": [film], "new_name": "Nouveau Titre (2010)", "tmdb_id": 9})
    assert response.status_code == 200, response.text
    assert response.json()["renamed_path"] is None
    assert os.path.exists(film)
    assert identities.get("films", film)["name"] == "Nouveau Titre (2010)"


def test_renommage_physique_suit_l_identite_et_la_marque(library, unlocked_client, monkeypatch):
    _fake_scan(monkeypatch)
    old = str(library["films"] / "Film A (2001).mkv")
    unlocked_client.post("/api/library/category", json={"paths": [old], "adult": True})
    response = _identify(unlocked_client, {"kind": "films", "paths": [old], "new_name": "Film A Bis (2001)",
                                           "tmdb_id": 42, "rename_file": True})
    assert response.status_code == 200, response.text
    target = str(library["films"] / "Film A Bis (2001).mkv")
    assert response.json()["renamed_path"] == target and response.json()["plex_refresh"] is True
    assert os.path.exists(target) and not os.path.exists(old)
    assert identities.get("films", target)["tmdb_id"] == 42
    assert identities.get("films", old) is None
    assert adult_media.is_adult_path(target) and not adult_media.is_adult_path(old)


def test_renommage_physique_refuse_pour_plusieurs_films(library, client):
    films = [str(library["films"] / "Film A (2001).mkv"), str(library["films"] / "Film B (2002).mkv")]
    response = _identify(client, {"kind": "films", "paths": films, "new_name": "X (2000)", "rename_file": True})
    assert response.status_code == 400


def test_renommage_physique_conflit_refuse(library, client):
    old = str(library["films"] / "Film A (2001).mkv")
    response = _identify(client, {"kind": "films", "paths": [old], "new_name": "Film B (2002)", "rename_file": True})
    assert response.status_code == 409
    assert os.path.exists(old)


def test_renommage_d_un_film_adulte_refuse_sans_session(library, client):
    adult_file = str(library["adults"] / "Titre C (2003).mkv")
    response = _identify(client, {"kind": "films", "paths": [adult_file], "new_name": "Autre (2003)", "rename_file": True})
    assert response.status_code == 404
    assert os.path.exists(adult_file)


# --- Cache de recherche TMDB --------------------------------------------------

def test_recherche_tmdb_mise_en_cache(monkeypatch):
    calls = []

    async def fake_uncached(kind, query, year=None, limit=8, adult=False):
        calls.append((kind, query, year))
        return [{"id": 1, "title": query, "year": year, "poster_url": None, "overview": ""}]

    monkeypatch.setattr(tmdb, "_search_uncached", fake_uncached)
    asyncio.run(tmdb.search("films", "Matrix"))
    asyncio.run(tmdb.search("films", "  matrix "))  # même requête normalisée : cache
    assert len(calls) == 1
    asyncio.run(tmdb.search("films", "Matrix", year=1999))  # une année change la requête
    assert len(calls) == 2


def test_recherche_tmdb_erreur_jamais_mise_en_cache(monkeypatch):
    attempts = []

    async def flaky(kind, query, year=None, limit=8, adult=False):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("panne passagère")
        return [{"id": 2, "title": query, "year": None, "poster_url": None, "overview": ""}]

    monkeypatch.setattr(tmdb, "_search_uncached", flaky)
    with pytest.raises(RuntimeError):
        asyncio.run(tmdb.search("films", "Dune"))
    assert asyncio.run(tmdb.search("films", "Dune"))[0]["id"] == 2
    assert len(attempts) == 2


# --- 1fichier : dossier ADULTES à la racine ----------------------------------

def test_dossier_adulte_1fichier_range_sous_adultes(env, monkeypatch):
    calls = []
    ids = iter([500, 600])

    async def fake_call(client, method, path, payload=None):
        calls.append((path, dict(payload or {})))
        return {"folder_id": next(ids)}

    monkeypatch.setattr(onefichier, "_call", fake_call)
    folder_id = asyncio.run(onefichier._ensure_folder(
        None, onefichier.adult_folder_key("603"), "tmdb-603", parent_key=onefichier.ADULT_ROOT_KEY))
    assert folder_id == 600
    assert calls[0][1] == {"name": "ADULTES", "folder_id": 0}          # racine
    assert calls[1][1] == {"name": "tmdb-603", "folder_id": 500}        # dans ADULTES
    again = asyncio.run(onefichier._ensure_folder(
        None, onefichier.adult_folder_key("603"), "tmdb-603", parent_key=onefichier.ADULT_ROOT_KEY))
    assert again == 600 and len(calls) == 2  # déjà connu : aucun nouvel appel


def test_dossier_ordinaire_1fichier_reste_a_la_racine(env, monkeypatch):
    calls = []

    async def fake_call(client, method, path, payload=None):
        calls.append(dict(payload or {}))
        return {"folder_id": 700}

    monkeypatch.setattr(onefichier, "_call", fake_call)
    assert asyncio.run(onefichier._ensure_folder(None, "603", "tmdb-603")) == 700
    assert calls == [{"name": "tmdb-603", "folder_id": 0}]


def test_cles_adultes_et_ordinaires_separees(env):
    assert onefichier.adult_folder_key("tv:5") != "tv:5"
    assert onefichier.folder_for("603") == onefichier.folder_for("603") == "tmdb-603"
