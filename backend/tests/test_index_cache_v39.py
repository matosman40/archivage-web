"""Cache de lecture de l'index 1fichier (version 39) : une relecture seulement quand le fichier change."""
import json
import os

import pytest

from app import onefichier


@pytest.fixture
def cache(env, monkeypatch):
    """Cache vide au départ, comptage des lectures disque."""
    monkeypatch.setattr(onefichier, "_SNAP", {"key": None, "data": None}, raising=False)
    reads = {"n": 0}
    real = onefichier._read

    def counting():
        reads["n"] += 1
        return real()

    monkeypatch.setattr(onefichier, "_read", counting)
    return reads


def _entry(name, **extra):
    return {"kind": "film", "name": name, "status": "done", "tmdb_id": 1, "password": "secret-mdp",
            "url": "https://1fichier.com/?abc", "archived_at": 1700000000, **extra}


def _index(**entries):
    return {"version": 1, "folders": {}, "entries": entries}


def test_une_seule_lecture_tant_que_le_fichier_ne_change_pas(env, cache):
    onefichier._write(_index(k1=_entry("Un")))
    cache["n"] = 0
    for _ in range(3):
        assert onefichier._snapshot()["entries"]["k1"]["name"] == "Un"
    assert cache["n"] == 1


def test_list_entries_ne_relit_pas_le_fichier(env, cache):
    onefichier._write(_index(k1=_entry("Un"), k2=_entry("Deux")))
    cache["n"] = 0
    assert len(onefichier.list_entries()) == 2
    assert len(onefichier.list_entries()) == 2
    assert cache["n"] == 1


def test_ecriture_de_l_application_visible_tout_de_suite(env, cache):
    onefichier._write(_index(k1=_entry("Un")))
    assert len(onefichier._snapshot()["entries"]) == 1
    onefichier._update(lambda d: d["entries"].__setitem__("k2", _entry("Deux")))
    assert sorted(onefichier._snapshot()["entries"]) == ["k1", "k2"]


def test_remplacement_externe_du_fichier_visible(env, cache):
    onefichier._write(_index(k1=_entry("Un")))
    assert len(onefichier._snapshot()["entries"]) == 1
    path = onefichier.index_path()
    tmp = path + ".externe"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(_index(k1=_entry("Un"), k9=_entry("Neuf")), fh)
    os.replace(tmp, path)  # nouvel inode, comme une écriture atomique
    assert sorted(onefichier._snapshot()["entries"]) == ["k1", "k9"]


def test_cache_sans_mot_de_passe_ni_lien(env, cache):
    onefichier._write(_index(k1=_entry("Un")))
    shown = onefichier._snapshot()["entries"]["k1"]
    assert shown["name"] == "Un"
    assert "password" not in shown and "url" not in shown
    # Le chemin de lecture complet (contrôle d'un droit, envoi) garde les secrets.
    full = onefichier.get_entry("k1")
    assert full["password"] == "secret-mdp" and full["url"] == "https://1fichier.com/?abc"


def test_mise_a_jour_relit_le_fichier_et_ne_modifie_pas_le_cache(env, cache):
    onefichier._write(_index(k1=_entry("Un")))
    before = onefichier._snapshot()
    onefichier._update(lambda d: d["entries"].__setitem__("k2", _entry("Deux")))
    assert sorted(before["entries"]) == ["k1"], "l'objet partagé reste intact"
    assert sorted(onefichier._snapshot()["entries"]) == ["k1", "k2"]


def test_index_absent_puis_cree(env, cache):
    assert onefichier._snapshot() == {"version": 1, "folders": {}, "entries": {}}
    onefichier._write(_index(k1=_entry("Un")))
    assert list(onefichier._snapshot()["entries"]) == ["k1"]


def test_index_illisible_leve_l_erreur_et_se_relit_apres_reparation(env, cache):
    path = onefichier.index_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{ pas du json")
    with pytest.raises(onefichier.OneFichierError):
        onefichier._snapshot()
    onefichier._write(_index(k1=_entry("Un")))
    assert list(onefichier._snapshot()["entries"]) == ["k1"]
