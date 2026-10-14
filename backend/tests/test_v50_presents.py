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


def test_etat_des_envois_1fichier():
    from app import onefichier
    assert onefichier.etat_fichier({"status": "done", "link_ok": True}) == "present"
    assert onefichier.etat_fichier({"status": "done", "link_ok": False}) == "absent"
    assert onefichier.etat_fichier({"status": "done", "link_ok": True, "corrompu": True}) == "corrompu"
    assert onefichier.etat_fichier({"status": "done"}) == "non_verifie"
    assert onefichier.etat_fichier({"status": "error"}) is None


def test_controle_hebdomadaire_tombe_le_jour_choisi(monkeypatch):
    import time
    from app import onefichier
    monkeypatch.setattr(onefichier, "CHECK_HOUR", 3)
    monkeypatch.setattr(onefichier, "CHECK_WEEKDAY", 6)  # dimanche
    lundi_10h = time.mktime((2026, 10, 12, 10, 0, 0, 0, 0, -1))  # 12 octobre 2026 : lundi
    suivant = time.localtime(onefichier._next_run_at(lundi_10h))
    assert suivant.tm_wday == 6 and suivant.tm_hour == 3
    assert (suivant.tm_mday, suivant.tm_mon) == (18, 10)
    dimanche_1h = time.mktime((2026, 10, 18, 1, 0, 0, 0, 0, -1))
    assert time.localtime(onefichier._next_run_at(dimanche_1h)).tm_mday == 18
    dimanche_4h = time.mktime((2026, 10, 18, 4, 0, 0, 0, 0, -1))
    assert time.localtime(onefichier._next_run_at(dimanche_4h)).tm_mday == 25


def test_archive_abimee_compte_comme_erreur_1fichier(monkeypatch):
    from types import SimpleNamespace
    from app import onefichier
    assert issubclass(onefichier.ArchiveCorrompue, onefichier.OneFichierError)
    monkeypatch.setattr(onefichier, "_run_7z", lambda *a, **k: SimpleNamespace(returncode=2, stdout="", stderr="CRC error"))
    with pytest.raises(onefichier.ArchiveCorrompue):
        onefichier._7z_test("/nonexistent.7z", "x")


def test_taille_distante_differente_marque_l_archive(env, monkeypatch):
    import asyncio
    from app import onefichier
    onefichier._update(lambda d: d["entries"].update({"301": {
        "kind": "film", "status": "done", "url": "https://1fichier.com/?a301", "password": "PW301-XXXXXXXXXX",
        "archive_name": "tmdb-301.7z", "folder": "tmdb-301", "folder_id": 9, "remote_size": 1000, "size": 1000}}))

    async def faux_call(client, method, path, payload=None):
        return {"status": "OK", "files": [{"filename": "tmdb-301.7z", "size": 999, "date": "2026-10-01 10:00:00"}]}

    monkeypatch.setattr(onefichier, "_call", faux_call)
    resultat = asyncio.run(onefichier.check_entry("301"))
    assert resultat["link_ok"] is True and resultat["taille_ok"] is False
    assert onefichier.etat_fichier(onefichier._get_entry("301")) == "corrompu"


def test_un_seul_test_par_archive_dans_un_passage(env):
    import asyncio
    from app import onefichier
    onefichier._update(lambda d: d["entries"].update({
        "401": {"kind": "film", "status": "done", "remote_size": 1000, "size": 1000, "folder": "a", "archive_name": "a.7z"},
        "402": {"kind": "film", "status": "done", "remote_size": 1000, "size": 1000, "folder": "b", "archive_name": "b.7z"},
    }))
    premier = asyncio.run(onefichier._pick_restore_test())
    autre = asyncio.run(onefichier._pick_restore_test(frozenset({premier})))
    assert premier in {"401", "402"} and autre in {"401", "402"} and autre != premier
    assert asyncio.run(onefichier._pick_restore_test(frozenset({"401", "402"}))) is None
