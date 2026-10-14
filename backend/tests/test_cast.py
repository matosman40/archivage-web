"""Distribution d'un film (version 35) : ordre, photos, limite, cache, erreurs, route."""
import asyncio

import httpx
import pytest

from app import config, tmdb


CREDITS = {"cast": [
    {"name": "Acteur B", "character": "Rôle B", "order": 1, "profile_path": "/b.jpg"},
    {"name": "Acteur A", "character": "Rôle A", "order": 0, "profile_path": None},
    {"name": "", "character": "Sans nom", "order": 2, "profile_path": "/x.jpg"},   # ignoré
]}


class _Recorder:
    def __init__(self, payload):
        self.calls = []
        self.payload = payload
        self.exc = None

    async def __call__(self, client, path, params):
        self.calls.append((path, dict(params)))
        if self.exc is not None:
            raise self.exc
        return self.payload


@pytest.fixture
def cast_calls(monkeypatch):
    monkeypatch.setattr(type(config.settings), "tmdb_enabled", property(lambda self: True))
    tmdb._cast_cache.clear()
    recorder = _Recorder(CREDITS)
    monkeypatch.setattr(tmdb, "_get_json", recorder)
    yield recorder
    tmdb._cast_cache.clear()


def test_ordre_de_facturation_photos_et_noms_vides_ignores(cast_calls):
    cast = asyncio.run(tmdb.cast(603))
    assert [c["name"] for c in cast] == ["Acteur A", "Acteur B"]
    assert cast[0]["photo_url"] is None
    assert cast[1]["photo_url"] == "https://image.tmdb.org/t/p/w185/b.jpg"
    assert cast[1]["character"] == "Rôle B"


def test_requete_credits_dans_la_langue_des_reglages(cast_calls, monkeypatch):
    monkeypatch.setattr(config.settings, "tmdb_language", "fr-FR")
    asyncio.run(tmdb.cast(603))
    assert cast_calls.calls == [("/movie/603/credits", {"language": "fr-FR"})]


def test_limite_de_douze_acteurs(cast_calls):
    cast_calls.payload = {"cast": [{"name": f"Acteur {i:02d}", "order": i} for i in range(30)]}
    cast = asyncio.run(tmdb.cast(603))
    assert len(cast) == tmdb.CAST_LIMIT == 12
    assert cast[0]["name"] == "Acteur 00" and cast[-1]["name"] == "Acteur 11"


def test_un_film_n_est_demande_qu_une_fois(cast_calls):
    asyncio.run(tmdb.cast(603))
    asyncio.run(tmdb.cast(603))
    assert len(cast_calls.calls) == 1


def test_echec_passager_non_mis_en_cache(cast_calls):
    cast_calls.exc = httpx.ConnectError("panne")
    assert asyncio.run(tmdb.cast(603)) == []
    cast_calls.exc = None
    cast = asyncio.run(tmdb.cast(603))
    assert [c["name"] for c in cast] == ["Acteur A", "Acteur B"]
    assert len(cast_calls.calls) == 2  # la deuxième ouverture réessaie


def test_tmdb_desactive_ou_identifiant_absent_aucun_appel(cast_calls, monkeypatch):
    assert asyncio.run(tmdb.cast(None)) == []
    monkeypatch.setattr(type(config.settings), "tmdb_enabled", property(lambda self: False))
    assert asyncio.run(tmdb.cast(603)) == []
    assert cast_calls.calls == []


@pytest.fixture
def client(cast_calls, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.setattr(main, "log_action", lambda *args, **kwargs: None)
    return TestClient(main.app)


def test_route_renvoie_la_distribution(client):
    resp = client.get("/api/tmdb/cast", params={"tmdb_id": 603})
    assert resp.status_code == 200
    assert [c["name"] for c in resp.json()["cast"]] == ["Acteur A", "Acteur B"]


def test_route_refuse_un_identifiant_invalide(client):
    assert client.get("/api/tmdb/cast", params={"tmdb_id": 0}).status_code == 422
    assert client.get("/api/tmdb/cast", params={"tmdb_id": "abc"}).status_code == 422
    assert client.get("/api/tmdb/cast").status_code == 422


def test_route_sans_tmdb_renvoie_une_liste_vide(client, monkeypatch):
    monkeypatch.setattr(type(config.settings), "tmdb_enabled", property(lambda self: False))
    assert client.get("/api/tmdb/cast", params={"tmdb_id": 603}).json() == {"cast": []}
