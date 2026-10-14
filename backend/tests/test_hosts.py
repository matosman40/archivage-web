"""Socle des hébergeurs (version 33) : registre, adresses autorisées, plages et emplacements génériques."""
import json

import pytest

from app import config, hosts, onefichier, scheduler, urlguard
from app.hosts import HostError
from app.hosts.base import Host
from app.hosts.onefichier import OneFichierHost


@pytest.fixture
def clean_slots():
    scheduler._slots.clear()
    yield
    scheduler._slots.clear()


def test_registre_contient_1fichier_avec_un_type_connu():
    keys = [h.key for h in hosts.all_hosts()]
    assert keys == ["1fichier"]
    assert hosts.get("1fichier").kind in config.TRANSFER_KINDS


def test_hote_inconnu_leve_une_erreur_claire():
    with pytest.raises(HostError, match="hébergeur inconnu : gofile"):
        hosts.get("gofile")


def test_seules_les_actions_redefinies_sont_annoncees():
    # 1fichier n'implémente pour l'instant que le test de connexion : l'interface ne doit rien annoncer de plus.
    assert OneFichierHost().actions() == ["test_connection"]
    assert Host().actions() == []


def test_description_publique_sans_cle(env):
    description = OneFichierHost().describe()
    assert set(description) == {"key", "label", "kind", "kind_label", "domains", "max_file_bytes", "configured", "actions"}
    assert description["configured"] is True  # la clé de test est posée par la fixture env
    assert description["kind_label"] == "1fichier"
    assert "CLE-DE-TEST" not in json.dumps(description)


def test_adresses_1fichier_identiques_a_avant(env):
    accepted = [
        "https://1fichier.com/a",
        "http://cdn.1fichier.com/a",
        "https://up3.1fichier.com:443/x",
    ]
    rejected = [
        "https://evil1fichier.com/",
        "https://1fichier.com.evil.org/",
        "https://evil.com/?x=.1fichier.com",
        "not a url",
        "",
    ]
    for url in accepted:
        onefichier._check_host(url)
    for url in rejected:
        with pytest.raises(onefichier.OneFichierError):
            onefichier._check_host(url)


def test_check_url_exige_https_et_domaine_autorise():
    host = OneFichierHost()
    host.check_url("https://up3.1fichier.com/get/abc")
    with pytest.raises(HostError):
        host.check_url("http://1fichier.com/get/abc")  # http refusé pour le socle
    with pytest.raises(HostError):
        host.check_url("https://evil.com/get/abc")


def test_domaine_voisin_jamais_accepte():
    assert urlguard.domain_allowed("1fichier.com", ("1fichier.com",))
    assert urlguard.domain_allowed("a.1fichier.com", ("1fichier.com",))
    assert not urlguard.domain_allowed("evil1fichier.com", ("1fichier.com",))
    assert not urlguard.domain_allowed("", ("1fichier.com",))


def test_validation_du_registre_refuse_un_type_inconnu(monkeypatch):
    class Broken(Host):
        key = "casse"
        label = "Casse"
        kind = "inexistant"
        domains = ("exemple.org",)

    monkeypatch.setattr(hosts, "_REGISTRY", {"casse": Broken()})
    with pytest.raises(RuntimeError, match="type de transfert inconnu"):
        hosts._validate()


def test_plages_acceptent_un_nouveau_type_sans_changer_le_code(monkeypatch):
    monkeypatch.setitem(config.TRANSFER_KINDS, "gofile", "gofile")
    windows = config._as_windows([{"name": "Nuit", "start": "01:00", "end": "07:00",
                                   "onefichier_mbps": 3, "gofile_mbps": 5}])
    assert windows[0]["gofile_mbps"] == 5
    assert windows[0]["disk_mbps"] == 0  # absent : illimité, comme avant


def test_plages_gardent_leur_forme_pour_1fichier_et_disque():
    windows = config._as_windows([{"name": "Jour", "start": "09:00", "end": "22:00",
                                   "onefichier_mbps": 2, "disk_mbps": 30}])
    assert list(windows[0]) == ["name", "start", "end", "enabled", "onefichier_mbps", "disk_mbps"]
    with pytest.raises(ValueError, match="plage « Jour », disque"):
        config._as_windows([{"name": "Jour", "start": "09:00", "end": "22:00", "disk_mbps": -1}])


def test_types_du_planificateur_viennent_de_la_config():
    assert scheduler.KINDS == tuple(config.TRANSFER_KINDS)
    assert scheduler._RATE_KEY["onefichier"] == "onefichier_mbps"


def test_libelles_des_emplacements(clean_slots):
    assert scheduler._get_slot("onefichier", None).label == "1fichier"
    assert scheduler._get_slot("disk", None).label == "disque"
    assert scheduler._get_slot("disk", "/media/usb1").label == "disque usb1"


def test_api_liste_les_hebergeurs(env):
    from fastapi.testclient import TestClient

    from app.main import app

    response = TestClient(app).get("/api/hosts")
    assert response.status_code == 200
    body = response.json()
    assert [h["key"] for h in body["hosts"]] == ["1fichier"]
    assert body["hosts"][0]["actions"] == ["test_connection"]
