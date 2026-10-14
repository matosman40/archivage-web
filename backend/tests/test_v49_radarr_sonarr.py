"""Version 49 : retirer un film de Radarr ou une série de Sonarr lors d'une suppression.

Radarr et Sonarr sont simulés par un transport HTTP factice : on vérifie les appels envoyés
(clé API, paramètres de suppression), les correspondances, et qu'une erreur n'entraîne aucune
suppression de fichier.
"""
import asyncio
import json
import re
from types import SimpleNamespace

import httpx
import pytest

from app import adult_auth, arr, config, disk_library, disks, identities, jobs, main, onefichier

RADARR = "http://radarr.test:7878"
SONARR = "http://sonarr.test:8989"
ORIGINAL_CLIENT = httpx.AsyncClient


class Faux:
    """Radarr (films) et Sonarr (séries) en mémoire ; chaque appel est noté."""

    def __init__(self):
        self.films = [{"id": 3, "title": "Alpha", "year": 2001, "tmdbId": 111}]
        self.series = [{"id": 7, "title": "Breaking Bad", "year": 2008, "tmdbId": 1396}]
        self.appels = []
        self.code_delete = None      # code forcé pour les suppressions (ex. 500)
        self.injoignable = False
        self.puts = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.injoignable:
            raise httpx.ConnectError("connexion refusée", request=request)
        app = "radarr" if request.url.host == "radarr.test" else "sonarr"
        cle_attendue = "CLE-RADARR" if app == "radarr" else "CLE-SONARR"
        if request.headers.get("X-Api-Key") != cle_attendue:
            return httpx.Response(401, json={"message": "Unauthorized"})
        path = request.url.path
        self.appels.append((request.method, path, dict(request.url.params)))
        if path == "/api/v3/system/status":
            return httpx.Response(200, json={"appName": "Radarr" if app == "radarr" else "Sonarr", "version": "5.1.0"})
        if path == "/api/v3/movie" and request.method == "GET":
            return httpx.Response(200, json=self.films)
        if path == "/api/v3/series" and request.method == "GET":
            return httpx.Response(200, json=self.series)
        trouve = re.match(r"/api/v3/(movie|series)/(\d+)$", path)
        if trouve and request.method == "GET":
            ident = int(trouve.group(2))
            liste = self.films if trouve.group(1) == "movie" else self.series
            for e in liste:
                if e["id"] == ident:
                    return httpx.Response(200, json=e)
            return httpx.Response(404, json={})
        if trouve and request.method == "PUT":
            ident = int(trouve.group(2))
            self.puts.append(json.loads(request.content))
            liste = self.films if trouve.group(1) == "movie" else self.series
            for i, e in enumerate(liste):
                if e["id"] == ident:
                    liste[i] = json.loads(request.content)
                    return httpx.Response(200, json=liste[i])
            return httpx.Response(404, json={})
        if trouve and request.method == "DELETE":
            if self.code_delete:
                return httpx.Response(self.code_delete, json={})
            ident = int(trouve.group(2))
            liste = self.films if trouve.group(1) == "movie" else self.series
            liste[:] = [e for e in liste if e["id"] != ident]
            return httpx.Response(200, json={})
        return httpx.Response(404, json={})


@pytest.fixture(autouse=True)
def _isolation(monkeypatch):
    adult_auth.reset()
    monkeypatch.setattr(jobs, "JOBS", {})
    monkeypatch.setattr(config, "ALLOW_DELETE", True)
    monkeypatch.setattr(main, "log_action", lambda *a, **k: None)
    yield
    adult_auth.reset()


@pytest.fixture
def faux(monkeypatch):
    fake = Faux()

    def fabrique(*args, **kwargs):
        kwargs.pop("timeout", None)
        return ORIGINAL_CLIENT(transport=httpx.MockTransport(fake), timeout=20)

    monkeypatch.setattr(arr.httpx, "AsyncClient", fabrique)
    monkeypatch.setattr(config.settings, "radarr_url", RADARR)
    monkeypatch.setattr(config.settings, "radarr_api_key", "CLE-RADARR")
    monkeypatch.setattr(config.settings, "sonarr_url", SONARR)
    monkeypatch.setattr(config.settings, "sonarr_api_key", "CLE-SONARR")
    return fake


@pytest.fixture
def serveur(env, monkeypatch, tmp_path):
    """Films et séries du serveur (dossiers sources), prêts à être supprimés."""
    films = tmp_path / "films"
    series = tmp_path / "series"
    films.mkdir()
    series.mkdir()
    film = films / "Alpha (2001).mkv"
    film.write_bytes(b"A" * 500)
    serie = series / "Breaking Bad"
    serie.mkdir()
    (serie / "S01E01.mkv").write_bytes(b"S" * 500)
    monkeypatch.setattr(config.settings, "source_dirs_films", [str(films)])
    monkeypatch.setattr(config.settings, "source_dirs_series", [str(series)])
    monkeypatch.setattr(config.settings, "source_dirs_ebooks", [])
    monkeypatch.setattr(config.settings, "source_dirs_adultes", [])
    monkeypatch.setattr(config.settings, "dry_run", False)
    monkeypatch.setattr(main, "_launch", lambda job: None)  # on contrôle la requête, pas la suppression
    identities.set_many("films", [str(film)], {"title": "Alpha", "year": 2001, "tmdb_id": 111})
    identities.set_many("series", [str(serie)], {"title": "Breaking Bad", "year": 2008, "tmdb_id": 1396})
    return {"film": str(film), "serie": str(serie)}


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    return TestClient(main.app)


def _supprimer(client, media, items, retirer=True):
    return client.post("/api/jobs", json={
        "operation": "delete", "media_type": media, "items": items,
        "confirm_delete_text": config.settings.delete_confirm_text, "retirer_arr": retirer,
    })


# --- Réglages -----------------------------------------------------------------------

@pytest.mark.parametrize("valeur", ["ftp://radarr", "radarr:7878", "javascript:alert(1)"])
def test_adresse_d_application_refusee_si_pas_http(valeur):
    with pytest.raises(ValueError):
        config._as_http_url(valeur)


def test_adresse_normalisee():
    assert config._as_http_url(" http://192.168.1.5:7878/ ") == "http://192.168.1.5:7878"
    assert config._as_http_url("") == ""


def test_reglages_ne_renvoient_pas_la_cle(client, monkeypatch):
    monkeypatch.setattr(config.settings, "radarr_api_key", "CLE-SECRETE-RADARR")
    monkeypatch.setattr(config.settings, "sonarr_api_key", "CLE-SECRETE-SONARR")
    texte = client.get("/api/settings").text
    assert "CLE-SECRETE-RADARR" not in texte and "CLE-SECRETE-SONARR" not in texte


def test_etat_sans_secret(client, faux):
    response = client.get("/api/arr/status")
    assert response.json() == {"radarr": {"label": "Radarr", "configured": True, "action": "retirer"},
                               "sonarr": {"label": "Sonarr", "configured": True, "action": "retirer"}}
    assert "CLE-RADARR" not in response.text


def test_test_de_connexion(client, faux):
    response = client.post("/api/arr/test", json={"kind": "radarr"})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "app": "Radarr", "version": "5.1.0"}


def test_test_de_connexion_cle_refusee(client, faux, monkeypatch):
    monkeypatch.setattr(config.settings, "sonarr_api_key", "MAUVAISE")
    response = client.post("/api/arr/test", json={"kind": "sonarr"})
    assert response.status_code == 502
    assert "clé API" in response.json()["detail"] and "MAUVAISE" not in response.text


def test_test_de_connexion_application_injoignable(client, faux):
    faux.injoignable = True
    response = client.post("/api/arr/test", json={"kind": "radarr"})
    assert response.status_code == 502 and "injoignable" in response.json()["detail"]
    assert RADARR not in response.json()["detail"]  # l'adresse n'est pas renvoyée telle quelle


def test_route_de_test_protegee_par_le_jeton(client, faux, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_TOKEN", "JETON-TEST")
    assert client.post("/api/arr/test", json={"kind": "radarr"}).status_code == 401


# --- Correspondances ----------------------------------------------------------------

def test_correspondance_par_identifiant_tmdb(faux):
    plan = _plan("films", [{"title": "Alpha", "year": 2001, "tmdb_id": 111}])
    assert plan[0]["status"] == "a_retirer" and plan[0]["id"] == 3


def test_correspondance_par_titre_et_annee_sans_tmdb(faux):
    plan = _plan("series", [{"title": "breaking  bad!", "year": 2008, "tmdb_id": None}])
    assert plan[0]["status"] == "a_retirer" and plan[0]["id"] == 7


def test_annee_differente_pas_de_correspondance(faux):
    plan = _plan("series", [{"title": "Breaking Bad", "year": 1999, "tmdb_id": None}])
    assert plan[0]["status"] == "absent"


def test_absent_de_l_application(faux):
    plan = _plan("films", [{"title": "Inconnu", "year": 1990, "tmdb_id": 42}])
    assert plan[0]["status"] == "absent" and plan[0]["id"] is None


def test_titre_sans_annee_refuse_plutot_que_de_deviner(faux):
    # « Breaking Bad » sans année : Sonarr n'a qu'une fiche de ce titre, on ne la retire pas.
    with pytest.raises(arr.ArrError) as erreur:
        _plan("series", [{"title": "Breaking Bad", "year": None, "tmdb_id": None}])
    assert "Année inconnue" in str(erreur.value) and erreur.value.status == 409


def test_titre_sans_annee_sans_fiche_homonyme_reste_absent(faux):
    plan = _plan("series", [{"title": "Serie Inconnue", "year": None, "tmdb_id": None}])
    assert plan[0]["status"] == "absent"


def test_plusieurs_fiches_refusees(faux):
    faux.films.append({"id": 4, "title": "Alpha", "year": 2001, "tmdbId": 999})
    with pytest.raises(arr.ArrError) as erreur:
        _plan("films", [{"title": "Alpha", "year": 2001, "tmdb_id": None}])
    assert "Plusieurs fiches" in str(erreur.value)


def test_application_non_configuree_ne_fait_rien(monkeypatch, faux):
    monkeypatch.setattr(config.settings, "radarr_url", "")
    assert _plan("films", [{"title": "Alpha", "year": 2001, "tmdb_id": 111}]) == []


def test_ebooks_jamais_concernes(faux):
    assert _plan("ebooks", [{"title": "Livre", "year": None, "tmdb_id": None}]) == []


def _plan(media, metas):
    """Plan de retrait pour des éléments fictifs (leur fiche vient du paramètre metas)."""
    import asyncio
    items = [f"/x/{m['title']}" for m in metas]
    dico = {it: m for it, m in zip(items, metas)}
    return asyncio.run(arr.preparer(media, items, dico))


# --- Retrait ------------------------------------------------------------------------

def test_retrait_demande_sans_suppression_de_fichiers(faux):
    import asyncio
    plan = _plan("films", [{"title": "Alpha", "year": 2001, "tmdb_id": 111}])
    resultats = asyncio.run(arr.retirer(plan))
    assert resultats[0]["result"] == "retiré"
    suppression = [a for a in faux.appels if a[0] == "DELETE"]
    assert suppression == [("DELETE", "/api/v3/movie/3", {"deleteFiles": "false", "addImportListExclusion": "true"})]
    assert faux.films == []


def test_fiche_deja_absente_n_est_pas_une_erreur(faux):
    import asyncio
    faux.code_delete = 404
    plan = _plan("films", [{"title": "Alpha", "year": 2001, "tmdb_id": 111}])
    assert asyncio.run(arr.retirer(plan))[0]["result"] == "absent"


def test_erreur_d_application_arrete_le_retrait(faux):
    import asyncio
    faux.code_delete = 500
    plan = _plan("films", [{"title": "Alpha", "year": 2001, "tmdb_id": 111}])
    with pytest.raises(arr.ArrError) as erreur:
        asyncio.run(arr.retirer(plan))
    assert "La suppression des fichiers n'a pas été lancée" in str(erreur.value)


# --- Suppression du serveur avec retrait -------------------------------------------

def test_suppression_serveur_retire_la_fiche_puis_lance_la_tache(client, faux, serveur, monkeypatch):
    lances = []
    monkeypatch.setattr(main, "_launch", lambda job: lances.append(job))
    response = _supprimer(client, "films", [serveur["film"]])
    assert response.status_code == 200, response.text
    assert faux.films == []  # fiche retirée
    assert len(lances) == 1
    assert lances[0].operation == "delete"


def test_suppression_serveur_sans_retrait_ne_touche_pas_l_application(client, faux, serveur):
    response = _supprimer(client, "films", [serveur["film"]], retirer=False)
    assert response.status_code == 200
    assert faux.appels == []


def test_echec_du_retrait_bloque_la_suppression(client, faux, serveur, monkeypatch):
    lances = []
    monkeypatch.setattr(main, "_launch", lambda job: lances.append(job))
    faux.code_delete = 500
    response = _supprimer(client, "films", [serveur["film"]])
    assert response.status_code == 502
    assert lances == [] and jobs.JOBS == {}
    assert __import__("os").path.exists(serveur["film"])


def test_application_injoignable_bloque_la_suppression(client, faux, serveur, monkeypatch):
    lances = []
    monkeypatch.setattr(main, "_launch", lambda job: lances.append(job))
    faux.injoignable = True
    response = _supprimer(client, "films", [serveur["film"]])
    assert response.status_code == 502 and lances == []
    assert __import__("os").path.exists(serveur["film"])


def test_serie_retiree_de_sonarr(client, faux, serveur, monkeypatch):
    monkeypatch.setattr(main, "_launch", lambda job: None)
    response = _supprimer(client, "series", [serveur["serie"]])
    assert response.status_code == 200, response.text
    assert faux.series == []
    assert any(a[1] == "/api/v3/series/7" and a[0] == "DELETE" for a in faux.appels)


def test_simulation_ne_retire_rien(client, faux, serveur, monkeypatch):
    monkeypatch.setattr(config.settings, "dry_run", True)
    response = _supprimer(client, "films", [serveur["film"]])
    assert response.status_code == 200
    assert faux.films != []
    assert not any(a[0] == "DELETE" for a in faux.appels)


def test_suppression_serveur_sans_radarr_configure_fonctionne_comme_avant(client, faux, serveur, monkeypatch):
    monkeypatch.setattr(config.settings, "radarr_api_key", "")
    response = _supprimer(client, "films", [serveur["film"]])
    assert response.status_code == 200
    assert faux.films != []


def test_suppression_serveur_exige_toujours_la_confirmation(client, faux, serveur):
    response = client.post("/api/jobs", json={"operation": "delete", "media_type": "films",
                                              "items": [serveur["film"]], "confirm_delete_text": "NON",
                                              "retirer_arr": True})
    assert response.status_code == 403
    assert faux.appels == []


# --- Suppression sur disque avec retrait -------------------------------------------

def test_suppression_disque_retire_la_fiche_grace_a_la_fiche_du_disque(client, faux, env, monkeypatch, tmp_path):
    root = tmp_path / "disque"
    (root / "MEDIA" / "FILM").mkdir(parents=True)
    film = root / "MEDIA" / "FILM" / "Zeta (2010).mkv"
    film.write_bytes(b"Z" * 300)
    monkeypatch.setattr(disks, "list_destination_disks", lambda: [{"path": str(root), "free_bytes": 10**12}])
    monkeypatch.setattr(config.settings, "dest_subdir_films", "MEDIA/FILM")
    monkeypatch.setattr(config.settings, "dest_subdir_adultes", "")
    monkeypatch.setattr(config.settings, "source_dirs_films", [str(tmp_path / "vide")])
    monkeypatch.setattr(config.settings, "source_dirs_adultes", [])
    monkeypatch.setattr(config.settings, "dry_run", False)
    monkeypatch.setattr(disk_library, "meta_for", lambda p: {"title": "Alpha", "year": 2001, "tmdb_id": 111})
    monkeypatch.setattr(main, "_launch", lambda job: None)
    response = client.post("/api/disk-library/delete", json={
        "items": [str(film)], "confirm_delete_text": config.settings.delete_confirm_text, "retirer_arr": True})
    assert response.status_code == 200, response.text
    assert faux.films == []  # Alpha (tmdb 111) est retiré, même si le nom du fichier est différent


def test_suppression_disque_sans_retrait_par_defaut(client, faux, env, monkeypatch, tmp_path):
    root = tmp_path / "disque"
    (root / "MEDIA" / "FILM").mkdir(parents=True)
    film = root / "MEDIA" / "FILM" / "Alpha (2001).mkv"
    film.write_bytes(b"A" * 300)
    monkeypatch.setattr(disks, "list_destination_disks", lambda: [{"path": str(root), "free_bytes": 10**12}])
    monkeypatch.setattr(config.settings, "dest_subdir_films", "MEDIA/FILM")
    monkeypatch.setattr(config.settings, "dest_subdir_adultes", "")
    monkeypatch.setattr(config.settings, "dry_run", False)
    monkeypatch.setattr(main, "_launch", lambda job: None)
    response = client.post("/api/disk-library/delete", json={
        "items": [str(film)], "confirm_delete_text": config.settings.delete_confirm_text})
    assert response.status_code == 200
    assert faux.appels == []


def test_reglages_enregistrent_les_adresses_et_gardent_les_cles_masquees(client, monkeypatch):
    monkeypatch.setattr(config.settings, "radarr_api_key", "CLE-RADARR-COMPLETE")
    response = client.put("/api/settings", json={"radarr_url": "http://radarr.local:7878/",
                                                 "radarr_api_key": "••••••••OMPLETE"})
    assert response.status_code == 200, response.text
    assert config.settings.radarr_url == "http://radarr.local:7878"
    assert config.settings.radarr_api_key == "CLE-RADARR-COMPLETE"  # la valeur masquée est ignorée


# Libération de la copie locale après 1fichier (version 49) : la fiche est retirée seulement si demandé.

def _liberation(faux, monkeypatch, *, retirer, kind="film", show=None, dry=False):
    monkeypatch.setattr(config.settings, "dry_run", dry)
    job = SimpleNamespace(retirer_arr=retirer)
    return asyncio.run(onefichier._retirer_fiche_arr(job, "/x/Alpha (2001).mkv", kind, show))


def test_liberation_sans_demande_ne_touche_pas_l_application(faux, monkeypatch):
    assert _liberation(faux, monkeypatch, retirer=False) is None
    assert faux.appels == []


def test_liberation_demandee_retire_la_fiche(faux, monkeypatch):
    assert _liberation(faux, monkeypatch, retirer=True) is None
    assert ("DELETE", "/api/v3/movie/3", {"deleteFiles": "false", "addImportListExclusion": "true"}) in faux.appels


def test_liberation_par_saison_ne_retire_rien(faux, monkeypatch):
    assert _liberation(faux, monkeypatch, retirer=True, kind=onefichier.KIND_SERIE, show="/x/Serie") is None
    assert not any(m == "DELETE" for m, _p, _q in faux.appels)


def test_liberation_erreur_renvoie_un_message_sans_supprimer(faux, monkeypatch):
    faux.code_delete = 500
    message = _liberation(faux, monkeypatch, retirer=True)
    assert message and "Radarr a répondu 500" in message
    assert faux.films  # la fiche est toujours là


def test_liberation_simulation_ne_retire_rien(faux, monkeypatch):
    assert _liberation(faux, monkeypatch, retirer=True, dry=True) is None
    assert not any(m == "DELETE" for m, _p, _q in faux.appels)


def test_liberation_ebook_jamais_concerne(faux, monkeypatch):
    assert _liberation(faux, monkeypatch, retirer=True, kind=onefichier.KIND_EBOOK) is None
    assert faux.appels == []


def test_automatisation_liberer_garde_le_choix_radarr(monkeypatch):
    from app import workflows
    coche = workflows._normaliser_action({"type": "envoyer_1fichier", "apres": "liberer", "confirme": True, "retirer_arr": True})
    decoche = workflows._normaliser_action({"type": "envoyer_1fichier", "apres": "liberer", "confirme": True})
    garde = workflows._normaliser_action({"type": "envoyer_1fichier", "apres": "garder", "retirer_arr": True})
    assert coche["retirer_arr"] is True and decoche["retirer_arr"] is False
    assert "retirer_arr" not in garde


def test_desactiver_garde_la_fiche_mais_ne_la_cherche_plus(faux, monkeypatch):
    monkeypatch.setattr(config.settings, "arr_action", "desactiver")
    plan = _plan("films", [{"title": "Alpha", "year": 2001, "tmdb_id": None}])
    resultats = asyncio.run(arr.retirer(plan))
    assert [r["result"] for r in resultats] == ["désactivé"]
    assert faux.films and faux.films[0]["monitored"] is False  # toujours dans Radarr
    assert not any(m == "DELETE" for m, _p, _q in faux.appels)


def test_desactiver_ne_touche_pas_une_fiche_deja_desactivee(faux, monkeypatch):
    monkeypatch.setattr(config.settings, "arr_action", "desactiver")
    faux.films[0]["monitored"] = False
    plan = _plan("films", [{"title": "Alpha", "year": 2001, "tmdb_id": None}])
    assert [r["result"] for r in asyncio.run(arr.retirer(plan))] == ["désactivé"]
    assert faux.puts == []


def test_reglage_arr_action_refuse_une_valeur_inconnue():
    from app import config as cfg
    with pytest.raises(ValueError):
        cfg._as_arr_action("supprimer")
    assert cfg._as_arr_action(" Desactiver ") == "desactiver"


def test_cle_sonarr_masquee_dans_les_erreurs(monkeypatch):
    from app import main
    monkeypatch.setattr(config.settings, "sonarr_api_key", "CLE-SECRETE-SONARR")
    assert "CLE-SECRETE-SONARR" not in main._sans_secret("refus de Sonarr avec CLE-SECRETE-SONARR")
