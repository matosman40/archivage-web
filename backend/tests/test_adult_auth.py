"""Code d'accès aux médias adultes (version 36) : empreinte, sessions, essais, routes."""
import json
import os
import stat

import pytest
from starlette.websockets import WebSocketDisconnect

from app import adult_auth, jobs


@pytest.fixture(autouse=True)
def _isolation(env, monkeypatch):
    """Fichier de code dans un dossier temporaire, PBKDF2 allégé, horloge et registres remis à zéro."""
    adult_auth.reset()
    monkeypatch.setattr(adult_auth, "ITERATIONS", 1000)
    monkeypatch.setattr(jobs, "JOBS", {})
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(adult_auth, "_now", lambda: clock["now"])
    yield clock
    adult_auth.reset()


def _echecs(n):
    for _ in range(n):
        with pytest.raises(adult_auth.AdultAuthError):
            adult_auth.unlock("0000")


# --- Code et empreinte -------------------------------------------------------

def test_code_stocke_sous_forme_d_empreinte(env):
    adult_auth.set_code("  monCode-42 ")
    raw = open(adult_auth._path(), encoding="utf-8").read()
    assert "monCode-42" not in raw
    data = json.loads(raw)
    assert data["algo"] == "pbkdf2_sha256" and len(data["salt"]) == 32
    assert adult_auth.unlock("monCode-42")  # les espaces de bord sont ignorés


def test_fichier_du_code_reserve_au_proprietaire(env):
    adult_auth.set_code("1234")
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(adult_auth._path()).st_mode) == 0o600


def test_code_trop_court_ou_trop_long_refuse(env):
    with pytest.raises(adult_auth.AdultAuthError) as court:
        adult_auth.set_code("123")
    assert court.value.status == 400
    with pytest.raises(adult_auth.AdultAuthError):
        adult_auth.set_code("x" * (adult_auth.MAX_LENGTH + 1))
    assert not adult_auth.code_is_set()


def test_code_faux_refuse_et_bon_code_ouvre_une_session(env):
    adult_auth.set_code("1234")
    with pytest.raises(adult_auth.AdultAuthError) as exc:
        adult_auth.unlock("0000")
    assert exc.value.status == 403
    assert adult_auth.session_is_valid(adult_auth.unlock("1234"))


def test_aucun_code_defini_pas_de_session(env):
    with pytest.raises(adult_auth.AdultAuthError) as exc:
        adult_auth.unlock("1234")
    assert exc.value.status == 409


# --- Limitation des essais ----------------------------------------------------

def test_cinq_echecs_suspendent_les_essais(env):
    adult_auth.set_code("1234")
    _echecs(adult_auth.MAX_FAILURES)
    with pytest.raises(adult_auth.AdultAuthError) as exc:
        adult_auth.unlock("1234")  # même le bon code attend
    assert exc.value.status == 429
    assert adult_auth.retry_in() == adult_auth.LOCKOUT_SECONDS


def test_apres_la_pause_un_nouvel_echec_suspend_a_nouveau(env, _isolation):
    adult_auth.set_code("1234")
    _echecs(adult_auth.MAX_FAILURES)
    _isolation["now"] += adult_auth.LOCKOUT_SECONDS + 1
    _echecs(1)  # une seule tentative par pause : le compteur n'est pas remis à zéro
    assert adult_auth.retry_in() > 0


def test_bon_code_remet_le_compteur_a_zero(env):
    adult_auth.set_code("1234")
    _echecs(adult_auth.MAX_FAILURES - 1)
    adult_auth.unlock("1234")
    _echecs(adult_auth.MAX_FAILURES - 1)  # de nouveau sous la limite : pas de suspension
    assert adult_auth.retry_in() == 0


def test_changer_le_code_compte_aussi_les_echecs(env):
    adult_auth.set_code("1234")
    for _ in range(adult_auth.MAX_FAILURES):
        with pytest.raises(adult_auth.AdultAuthError) as exc:
            adult_auth.set_code("5678", current="0000")
        assert exc.value.status == 403
    with pytest.raises(adult_auth.AdultAuthError) as exc:  # la pause suit le cinquième échec
        adult_auth.set_code("5678", current="1234")
    assert exc.value.status == 429  # l'ancien code ne peut pas être deviné en boucle
    with open(adult_auth._path(), encoding="utf-8") as fh:
        stored = json.load(fh)
    assert adult_auth._matches(stored, "1234")  # le code d'origine est toujours en place


# --- Sessions -------------------------------------------------------------------

def test_changer_le_code_ferme_les_sessions(env):
    adult_auth.set_code("1234")
    token = adult_auth.unlock("1234")
    adult_auth.set_code("5678", current="1234")
    assert not adult_auth.session_is_valid(token)
    assert adult_auth.unlock("5678")


def test_session_expire_apres_la_duree_maximale(env, _isolation):
    adult_auth.set_code("1234")
    token = adult_auth.unlock("1234")
    _isolation["now"] += adult_auth.SESSION_MAX_AGE - 1
    assert adult_auth.session_is_valid(token)
    _isolation["now"] += 2
    assert not adult_auth.session_is_valid(token)


def test_suppression_du_fichier_coupe_les_sessions(env):
    adult_auth.set_code("1234")
    token = adult_auth.unlock("1234")
    os.remove(adult_auth._path())  # oubli du code : la procédure de reprise du README
    assert not adult_auth.session_is_valid(token)


def test_revocation_et_jetons_inconnus(env):
    adult_auth.set_code("1234")
    token = adult_auth.unlock("1234")
    adult_auth.revoke(token)
    assert not adult_auth.session_is_valid(token)
    assert not adult_auth.session_is_valid(None)
    assert not adult_auth.session_is_valid("jeton-inconnu")


# --- Routes ------------------------------------------------------------------

@pytest.fixture
def client(env, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.setattr(main, "log_action", lambda *args, **kwargs: None)
    return TestClient(main.app)


def test_etat_initial(client):
    assert client.get("/api/adult/state").json() == {"code_set": False, "unlocked": False, "retry_in": 0}


def test_premier_code_se_definit_sans_session(client):
    resp = client.post("/api/adult/code", json={"new": "1234"})
    assert resp.status_code == 200
    assert client.get("/api/adult/state").json()["unlocked"] is True  # l'auteur reste déverrouillé


def test_deverrouillage_pose_un_cookie_httponly_samesite(client):
    adult_auth.set_code("1234")
    resp = client.post("/api/adult/unlock", json={"code": "1234"})
    assert resp.status_code == 200
    header = resp.headers["set-cookie"]
    assert "archivage_adult=" in header
    assert "httponly" in header.lower() and "samesite=strict" in header.lower()
    assert "max-age" not in header.lower() and "expires=" not in header.lower()  # cookie de session
    assert client.get("/api/adult/state").json()["unlocked"] is True


def test_code_incorrect_puis_suspension_par_la_route(client):
    adult_auth.set_code("1234")
    codes = [client.post("/api/adult/unlock", json={"code": "0000"}).status_code
             for _ in range(adult_auth.MAX_FAILURES)]
    assert codes == [403] * adult_auth.MAX_FAILURES
    blocked = client.post("/api/adult/unlock", json={"code": "1234"})
    assert blocked.status_code == 429
    assert client.get("/api/adult/state").json()["retry_in"] > 0


def test_verrouiller_retire_la_session(client):
    adult_auth.set_code("1234")
    client.post("/api/adult/unlock", json={"code": "1234"})
    assert client.get("/api/adult/state").json()["unlocked"] is True
    resp = client.post("/api/adult/lock")
    assert resp.status_code == 200 and resp.json() == {"unlocked": False}
    assert client.get("/api/adult/state").json()["unlocked"] is False


def test_changer_le_code_exige_l_ancien(client):
    adult_auth.set_code("1234")
    client.post("/api/adult/unlock", json={"code": "1234"})
    assert client.post("/api/adult/code", json={"current": "0000", "new": "5678"}).status_code == 403
    ok = client.post("/api/adult/code", json={"current": "1234", "new": "5678"})
    assert ok.status_code == 200
    assert client.get("/api/adult/state").json()["unlocked"] is True
    client.post("/api/adult/lock")
    assert client.post("/api/adult/unlock", json={"code": "1234"}).status_code == 403
    assert client.post("/api/adult/unlock", json={"code": "5678"}).status_code == 200


def test_ancien_reglage_adult_visible_n_ouvre_rien(client):
    resp = client.put("/api/settings", json={"adult_visible": True})
    assert resp.status_code == 200  # champ inconnu : ignoré, jamais enregistré
    assert "adult_visible" not in client.get("/api/settings").json()["settings"]


def test_websocket_d_une_tache_adulte_refuse_sans_session(client):
    job = jobs.create_job("copy", "films", ["/x/Film.mkv"], adult=True)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ws/jobs/{job.id}"):
            pass
    assert exc.value.code == 4404
