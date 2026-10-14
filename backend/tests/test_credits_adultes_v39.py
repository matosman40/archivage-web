"""Films pour adultes et données TMDB (version 39) : crédits et distribution refusés sans session."""
import httpx
import pytest
from fastapi.testclient import TestClient

from app import adult_auth, config, main, tmdb

NORMAL = {
    "title": "Film normal", "adult": False,
    "production_companies": [{"id": 7, "name": "Studio Alpha", "logo_path": "/alpha.png", "origin_country": "FR"}],
    "credits": {
        "cast": [{"id": 11, "name": "Actrice", "character": "Rôle A", "order": 0, "profile_path": "/a.jpg"}],
        "crew": [{"id": 21, "name": "Réalisateur", "job": "Director", "department": "Directing", "profile_path": None}],
    },
}
ADULT = {
    "title": "Titre pour adultes", "adult": True,
    "production_companies": [{"id": 8, "name": "Studio Gamma", "logo_path": None, "origin_country": "US"}],
    "credits": {
        "cast": [{"id": 12, "name": "Acteur", "character": "Rôle B", "order": 0, "profile_path": None}],
        "crew": [{"id": 22, "name": "Réalisatrice", "job": "Director", "department": "Directing", "profile_path": None}],
    },
}


class FakeTmdb:
    """Remplace _get_json : réponses par chemin, 404 pour le reste (identifiant inconnu)."""

    def __init__(self):
        self.routes = {
            "/movie/603": NORMAL, "/movie/603/credits": {"cast": NORMAL["credits"]["cast"]},
            "/movie/604": ADULT, "/movie/604/credits": {"cast": ADULT["credits"]["cast"]},
        }

    async def __call__(self, client, path, params):
        if path in self.routes:
            return self.routes[path]
        raise httpx.HTTPStatusError("404", request=httpx.Request("GET", "https://api.themoviedb.org" + path),
                                    response=httpx.Response(404))


@pytest.fixture
def routes(env, monkeypatch):
    adult_auth.reset()
    monkeypatch.setattr(adult_auth, "ITERATIONS", 1000)
    monkeypatch.setattr(main, "log_action", lambda *a, **k: None)
    monkeypatch.setattr(tmdb, "_get_json", FakeTmdb())
    monkeypatch.setattr(tmdb, "_credits_cache", {})
    monkeypatch.setattr(tmdb, "_cast_cache", {})
    monkeypatch.setattr(type(config.settings), "tmdb_enabled", property(lambda self: True))
    monkeypatch.setattr(config.settings, "tmdb_language", "fr-FR")
    yield TestClient(main.app)
    adult_auth.reset()


def _unlock(client):
    adult_auth.set_code("1234")
    assert client.post("/api/adult/unlock", json={"code": "1234"}).status_code == 200


def test_fiche_technique_adulte_sans_session_est_vide(routes):
    data = routes.get("/api/tmdb/credits?tmdb_id=604").json()
    assert data == tmdb.empty_credits()
    assert "adult" not in data


def test_film_adulte_sans_session_ne_se_distingue_pas_d_un_identifiant_inconnu(routes):
    adult = routes.get("/api/tmdb/credits?tmdb_id=604")
    unknown = routes.get("/api/tmdb/credits?tmdb_id=999")
    assert adult.status_code == unknown.status_code == 200
    assert adult.json() == unknown.json()


def test_fiche_technique_adulte_avec_session(routes):
    _unlock(routes)
    data = routes.get("/api/tmdb/credits?tmdb_id=604").json()
    assert [d["name"] for d in data["directors"]] == ["Réalisatrice"]
    assert [s["name"] for s in data["studios"]] == ["Studio Gamma"]
    assert "adult" not in data


def test_film_normal_sans_session(routes):
    data = routes.get("/api/tmdb/credits?tmdb_id=603").json()
    assert [d["name"] for d in data["directors"]] == ["Réalisateur"]
    assert [s["name"] for s in data["studios"]] == ["Studio Alpha"]


def test_cache_garde_le_drapeau_et_la_route_ne_le_modifie_pas(routes):
    assert routes.get("/api/tmdb/credits?tmdb_id=604").json() == tmdb.empty_credits()  # rempli le cache
    cached = tmdb._credits_cache[(604, "fr-FR")]
    assert cached["adult"] is True, "le résultat en cache garde le drapeau, la route en fait une copie"
    _unlock(routes)
    assert routes.get("/api/tmdb/credits?tmdb_id=604").json()["studios"][0]["name"] == "Studio Gamma"
    adult_auth.reset()
    assert routes.get("/api/tmdb/credits?tmdb_id=604").json() == tmdb.empty_credits(), \
        "après un cache rempli par une session ouverte, la fermeture masque encore le film"


def test_distribution_adulte_sans_session_vide(routes):
    assert routes.get("/api/tmdb/cast?tmdb_id=604").json() == {"cast": []}
    cast = routes.get("/api/tmdb/cast?tmdb_id=603").json()["cast"]
    assert [c["name"] for c in cast] == ["Actrice"]


def test_distribution_adulte_avec_session(routes):
    _unlock(routes)
    cast = routes.get("/api/tmdb/cast?tmdb_id=604").json()["cast"]
    assert [c["name"] for c in cast] == ["Acteur"]


def test_tmdb_desactive_donne_une_fiche_vide(routes, monkeypatch):
    monkeypatch.setattr(type(config.settings), "tmdb_enabled", property(lambda self: False))
    assert routes.get("/api/tmdb/credits?tmdb_id=603").json() == tmdb.empty_credits()
    assert routes.get("/api/tmdb/cast?tmdb_id=603").json() == {"cast": []}
