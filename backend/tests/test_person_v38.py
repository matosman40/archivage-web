"""Fiche technique, fiche personne et filmographie (version 38) : parsing, cache, verrouillage, présence."""
import asyncio

import httpx
import pytest

from app import adult_auth, config, jobs, main, tmdb


@pytest.fixture(autouse=True)
def _isolation(env, monkeypatch):  # « env » : réglages et code adulte dans un dossier temporaire
    adult_auth.reset()
    monkeypatch.setattr(adult_auth, "ITERATIONS", 1000)
    monkeypatch.setattr(jobs, "JOBS", {})
    monkeypatch.setattr(tmdb, "_credits_cache", {})
    monkeypatch.setattr(tmdb, "_person_cache", {})
    monkeypatch.setattr(type(config.settings), "tmdb_enabled", property(lambda self: True))
    monkeypatch.setattr(config.settings, "tmdb_language", "fr-FR")
    yield
    adult_auth.reset()


class FakeTmdb:
    """Remplace _get_json : réponses par chemin (ou par chemin et langue), 404 pour le reste."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.exc = None

    async def __call__(self, client, path, params):
        self.calls.append((path, dict(params)))
        if self.exc is not None:
            raise self.exc
        lang = params.get("language")
        if (path, lang) in self.routes:
            return self.routes[(path, lang)]
        if path in self.routes:
            return self.routes[path]
        raise httpx.HTTPStatusError("404", request=httpx.Request("GET", "https://api.themoviedb.org" + path),
                                    response=httpx.Response(404))


@pytest.fixture
def fake(monkeypatch):
    f = FakeTmdb({})
    monkeypatch.setattr(tmdb, "_get_json", f)
    return f


MOVIE_603 = {
    "title": "Film", "release_date": "2001-05-01",
    "production_companies": [
        {"id": 7, "name": "Studio Alpha", "logo_path": "/alpha.png", "origin_country": "FR"},
        {"id": 8, "name": "   ", "logo_path": None},
        {"id": 9, "name": "Studio Beta", "logo_path": None, "origin_country": None},
    ],
    "credits": {
        "cast": [
            {"id": 11, "name": "Actrice", "character": "Rôle A", "order": 0, "profile_path": "/a.jpg"},
            {"id": 12, "name": "Acteur", "character": None, "order": 1, "profile_path": None},
        ],
        "crew": [
            {"id": 21, "name": "Réalisateur", "job": "Director", "department": "Directing", "profile_path": "/r.jpg"},
            {"id": 21, "name": "Réalisateur", "job": "Director", "department": "Directing"},  # doublon
            {"id": 22, "name": "Scénariste", "job": "Screenplay", "department": "Writing", "profile_path": None},
            {"id": 23, "name": "Monteur", "job": "Editor", "department": "Editing", "profile_path": None},
        ],
    },
}

PERSON_501 = {
    "name": "Actrice", "known_for_department": "Acting", "biography": "", "birthday": "1970-03-04",
    "deathday": None, "place_of_birth": "Paris, France", "profile_path": "/p.jpg",
    "movie_credits": {
        "cast": [
            {"id": 1, "title": "Ancien", "release_date": "1990-01-01", "character": "Rôle 1",
             "poster_path": None, "vote_average": 6.04, "adult": False},
            {"id": 2, "title": "Récent", "release_date": "2020-06-01", "character": "Rôle 2",
             "poster_path": "/p2.jpg", "vote_average": 0, "adult": False},
            {"id": 3, "title": "Pour adultes", "release_date": "2015-01-01", "character": "Rôle 3", "adult": True},
            {"id": 4, "title": "Sans date", "release_date": "", "character": None, "adult": False},
        ],
        "crew": [
            {"id": 2, "title": "Récent", "release_date": "2020-06-01", "job": "Producer",
             "department": "Production", "adult": False},
            {"id": 5, "title": "Réalisé", "release_date": "2005-01-01", "job": "Director", "adult": False},
        ],
    },
}


# ---------------------------------------------------------------- fiche technique

def test_realisateurs_scenaristes_studios_et_distribution(fake):
    fake.routes["/movie/603"] = MOVIE_603
    c = asyncio.run(tmdb.credits(603))
    assert [d["name"] for d in c["directors"]] == ["Réalisateur"]          # doublon et monteur écartés
    assert c["directors"][0]["photo_url"] == "https://image.tmdb.org/t/p/w185/r.jpg"
    assert [w["name"] for w in c["writers"]] == ["Scénariste"]
    assert [s["name"] for s in c["studios"]] == ["Studio Alpha", "Studio Beta"]  # nom vide ignoré
    assert c["studios"][0]["logo_url"] == "https://image.tmdb.org/t/p/w185/alpha.png"
    assert c["studios"][1]["logo_url"] is None and c["studios"][0]["country"] == "FR"
    assert [a["id"] for a in c["cast"]] == [11, 12]
    assert c["cast"][1]["character"] is None and c["cast"][1]["photo_url"] is None


def test_une_seule_requete_avec_les_credits_dans_la_langue(fake):
    fake.routes["/movie/603"] = MOVIE_603
    asyncio.run(tmdb.credits(603))
    asyncio.run(tmdb.credits(603))  # cache : pas de second appel
    assert fake.calls == [("/movie/603", {"language": "fr-FR", "append_to_response": "credits"})]


def test_echec_de_credits_vide_et_non_mis_en_cache(fake):
    fake.exc = httpx.ConnectError("réseau coupé")
    assert asyncio.run(tmdb.credits(603)) == {"cast": [], "directors": [], "writers": [], "studios": []}
    fake.exc = None
    fake.routes["/movie/603"] = MOVIE_603
    assert [d["name"] for d in asyncio.run(tmdb.credits(603))["directors"]] == ["Réalisateur"]


def test_tmdb_desactive_donne_une_fiche_vide(fake, monkeypatch):
    monkeypatch.setattr(type(config.settings), "tmdb_enabled", property(lambda self: False))
    assert asyncio.run(tmdb.credits(603))["studios"] == []
    assert fake.calls == []


# ---------------------------------------------------------------- fiche personne

def test_fiche_personne_et_filmographie_sans_session(fake):
    fake.routes["/person/501"] = PERSON_501
    p = asyncio.run(tmdb.person(501, unlocked=False))
    assert p["name"] == "Actrice" and p["known_for"] == "Acting" and p["birthday"] == "1970-03-04"
    assert p["place_of_birth"] == "Paris, France" and p["photo_url"] == "https://image.tmdb.org/t/p/w342/p.jpg"
    assert p["tmdb_url"] == "https://www.themoviedb.org/person/501"
    titles = [f["title"] for f in p["films"]]
    assert titles == ["Récent", "Réalisé", "Ancien", "Sans date"]          # adulte absent, sans date en dernier


def test_roles_regroupes_par_film_avec_intitules_francais(fake):
    fake.routes["/person/501"] = PERSON_501
    films = {f["title"]: f for f in asyncio.run(tmdb.person(501, unlocked=True))["films"]}
    recent = films["Récent"]
    assert recent["roles"] == [{"group": "jeu", "label": "Rôle 2"}, {"group": "autres", "label": "Production"}]
    assert recent["rating"] is None                       # note 0 : pas de note
    assert films["Réalisé"]["roles"] == [{"group": "realisation", "label": "Réalisation"}]
    assert films["Ancien"]["rating"] == 6.0 and films["Ancien"]["year"] == 1990
    assert films["Sans date"]["year"] is None


def test_session_deverrouillee_garde_les_adultes_marques(fake):
    fake.routes["/person/501"] = PERSON_501
    films = asyncio.run(tmdb.person(501, unlocked=True))["films"]
    assert [f["title"] for f in films] == ["Récent", "Pour adultes", "Réalisé", "Ancien", "Sans date"]
    assert [f["adult"] for f in films if f["title"] == "Pour adultes"] == [True]


def test_biographie_en_repli_si_la_langue_est_vide(fake):
    fake.routes[("/person/501", "fr-FR")] = {**PERSON_501, "biography": ""}
    fake.routes[("/person/501", "en-US")] = {**PERSON_501, "biography": "English bio"}
    p = asyncio.run(tmdb.person(501, unlocked=False))
    assert p["biography"] == "English bio"
    assert ("/person/501", {"language": "en-US"}) in fake.calls


def test_personne_inconnue_renvoie_none(fake):
    assert asyncio.run(tmdb.person(999, unlocked=False)) is None


def test_erreur_passagere_relevee_et_jamais_mise_en_cache(fake):
    fake.exc = httpx.ConnectError("réseau coupé")
    with pytest.raises(httpx.ConnectError):
        asyncio.run(tmdb.person(501, unlocked=False))
    fake.exc = None
    fake.routes["/person/501"] = PERSON_501
    assert asyncio.run(tmdb.person(501, unlocked=False))["name"] == "Actrice"


def test_fiche_personne_mise_en_cache(fake):
    fake.routes["/person/501"] = PERSON_501
    asyncio.run(tmdb.person(501, unlocked=False))  # fr-FR, puis en-US car la biographie française est vide
    apres_le_premier = len(fake.calls)
    asyncio.run(tmdb.person(501, unlocked=True))   # le cache garde tout ; le filtre se fait à la lecture
    assert apres_le_premier == 2 and len(fake.calls) == apres_le_premier


# ---------------------------------------------------------------- routes et présence

@pytest.fixture
def routes(fake, monkeypatch):
    from fastapi.testclient import TestClient

    fake.routes["/person/501"] = PERSON_501
    fake.routes["/movie/603"] = MOVIE_603
    b_serveur = {"tmdb_id": "2", "where": "serveur", "path": "/s/Film B (2010).mkv", "title": "Film B"}
    b_disque = {"tmdb_id": 2, "where": "disque", "path": "/d/MEDIA/FILM/Film B (2010).mkv", "title": "Film B"}
    adulte = {"tmdb_id": 3, "where": "serveur", "path": "/s/Titre Adulte.mkv", "title": "Titre"}
    owned = {"locked": [b_disque, b_serveur, {"tmdb_id": None, "where": "1fichier", "path": None, "title": "X"}],
             "unlocked": [b_disque, b_serveur, adulte, {"tmdb_id": None, "where": "1fichier", "path": None, "title": "X"}]}

    async def fake_sources(unlocked):
        return owned["unlocked" if unlocked else "locked"]

    monkeypatch.setattr(main, "_film_sources", fake_sources)
    monkeypatch.setattr(main, "log_action", lambda *a, **k: None)
    return TestClient(main.app)


def _unlock(client):
    adult_auth.set_code("1234")
    assert client.post("/api/adult/unlock", json={"code": "1234"}).status_code == 200


def test_route_personne_sans_session_masque_adultes_et_presence(routes):
    data = routes.get("/api/tmdb/person?person_id=501").json()
    assert "Pour adultes" not in [f["title"] for f in data["films"]]
    recent = next(f for f in data["films"] if f["title"] == "Récent")
    assert recent["owned"] == [{"where": "serveur", "term": "Film B (2010).mkv"},   # serveur d'abord ; texte accepté
                               {"where": "disque", "term": "Film B (2010).mkv"}]
    assert data["owned_count"] == 1


def test_route_personne_deverrouillee_montre_adulte_possede(routes):
    _unlock(routes)
    data = routes.get("/api/tmdb/person?person_id=501").json()
    adult = next(f for f in data["films"] if f["title"] == "Pour adultes")
    assert adult["adult"] is True and adult["owned"] == [{"where": "serveur", "term": "Titre Adulte.mkv"}]
    assert data["owned_count"] == 2                          # Récent (disque + serveur) et l'adulte (serveur)


def test_route_personne_inconnue_et_tmdb_desactive(routes, monkeypatch):
    assert routes.get("/api/tmdb/person?person_id=999").status_code == 404
    monkeypatch.setattr(type(config.settings), "tmdb_enabled", property(lambda self: False))
    assert routes.get("/api/tmdb/person?person_id=501").status_code == 409


def test_route_personne_erreur_tmdb_donne_502(routes, fake):
    fake.exc = httpx.ConnectError("réseau coupé")
    assert routes.get("/api/tmdb/person?person_id=501").status_code == 502


def test_route_credits(routes):
    data = routes.get("/api/tmdb/credits?tmdb_id=603").json()
    assert [d["name"] for d in data["directors"]] == ["Réalisateur"]
    assert [s["name"] for s in data["studios"]] == ["Studio Alpha", "Studio Beta"]
    assert routes.get("/api/tmdb/credits?tmdb_id=0").status_code == 422
