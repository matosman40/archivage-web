"""Listes à jour (version 50) : on n'affiche que les fichiers encore présents.

Disques branchés, recherche du serveur et envois 1fichier : un élément supprimé ou dont le lien a
été perdu ne doit pas rester affiché, même si une copie en mémoire ou sur disque est plus ancienne.
"""
import pytest

from app import disk_library, disks, main, scan_cache


@pytest.fixture(autouse=True)
def _isolation(monkeypatch):
    monkeypatch.setattr(main, "log_action", lambda *a, **k: None)


def test_disque_ne_montre_plus_un_film_supprime(env, monkeypatch, tmp_path):
    root = tmp_path / "disque"
    film = root / "MEDIA" / "FILM" / "Film A (2001).mkv"
    film.parent.mkdir(parents=True)
    film.write_bytes(b"A" * 100)
    monkeypatch.setattr(disks, "list_destination_disks", lambda: [{"path": str(root), "free_bytes": 1}])
    avant = disk_library.refresh(force=True, attente=10)
    assert [it["name"] for it in avant[0]["items"]["films"]] == ["Film A (2001).mkv"]
    film.unlink()
    # Même sans nouveau scan (copie récente), le fichier disparu n'est plus affiché.
    apres = disk_library.refresh(force=False, attente=10)
    assert apres[0]["items"].get("films", []) == []


def test_disque_deconnecte_garde_ses_elements_connus(env, monkeypatch, tmp_path):
    """Un disque débranché ne peut pas être vérifié : ses éléments connus restent en lecture seule."""
    root = tmp_path / "disque"
    film = root / "MEDIA" / "FILM" / "Film B (2002).mkv"
    film.parent.mkdir(parents=True)
    film.write_bytes(b"B" * 100)
    monkeypatch.setattr(disks, "list_destination_disks", lambda: [{"path": str(root), "free_bytes": 1}])
    disk_library.refresh(force=True, attente=10)
    monkeypatch.setattr(disks, "list_destination_disks", lambda: [])
    hors_ligne = disk_library.refresh(force=False, attente=10)
    assert not hors_ligne[0]["connected"]
    assert [it["name"] for it in hors_ligne[0]["items"]["films"]] == ["Film B (2002).mkv"]


def test_recherche_serveur_masque_les_fichiers_disparus(env, monkeypatch, tmp_path):
    present = tmp_path / "Alpha (2001).mkv"
    present.write_bytes(b"x")
    items = [{"name": "Alpha (2001).mkv", "path": str(present)},
             {"name": "Bravo (2002).mkv", "path": str(tmp_path / "Bravo (2002).mkv")}]

    async def faux_search(kind, term):
        return items, {"built_at": 0, "refreshing": False}

    monkeypatch.setattr(scan_cache, "search", faux_search)
    from fastapi.testclient import TestClient
    reponse = TestClient(main.app).post("/api/search", json={"media_type": "films", "term": ""}).json()
    assert [it["name"] for it in reponse["items"]] == ["Alpha (2001).mkv"]


def test_envois_1fichier_ne_gardent_que_les_presents():
    entrees = [
        {"key": "a", "status": "done", "link_ok": True},
        {"key": "b", "status": "done", "link_ok": False},      # lien perdu chez 1fichier
        {"key": "c", "status": "done"},                         # pas encore constaté : présent
        {"key": "d", "status": "error"},                        # envoi raté : pas sur 1fichier
        {"key": "e", "status": "running", "active": True},      # en cours : reste visible
    ]
    assert [e["key"] for e in main._envois_presents(entrees)] == ["a", "c", "e"]
