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


def test_renvoi_seulement_pour_une_archive_absente(env, tmp_path):
    from app import onefichier
    source = tmp_path / "Alpha (2001).mkv"
    source.write_bytes(b"x")
    onefichier._update(lambda d: d["entries"].update({
        "501": {"kind": "film", "status": "done", "link_ok": False, "original_path": str(source), "folder": "tmdb-501"},
        "502": {"kind": "film", "status": "done", "link_ok": True, "corrompu": True, "original_path": str(source), "folder": "tmdb-502"},
        "503": {"kind": "film", "status": "done", "link_ok": False, "original_path": str(tmp_path / "absent.mkv"), "folder": "tmdb-503"},
    }))
    assert onefichier.preparer_renvoi("501") == str(source)
    with pytest.raises(onefichier.OneFichierError):
        onefichier.preparer_renvoi("502")  # archive corrompue encore présente : pas de renvoi
    with pytest.raises(onefichier.OneFichierError):
        onefichier.preparer_renvoi("503")  # copie locale disparue
    onefichier.retirer_absent("501")
    assert onefichier._get_entry("501") is None
    with pytest.raises(onefichier.OneFichierError):
        onefichier.retirer_absent("502")  # l'entrée n'est retirée que si elle est absente


def test_historique_des_controles_garde_les_derniers(env):
    from app import onefichier
    for i in range(onefichier.HISTORY_MAX_CHECKS + 3):
        onefichier._enregistrer_bilan({"at": float(i), "trigger": "manuel", "links_ok": i,
                                       "links_lost": [], "errors": [], "tests": [{"ok": True}], "tailles_differentes": []})
    historique = onefichier.checks_status()["history"]
    assert len(historique) == onefichier.HISTORY_MAX_CHECKS
    assert historique[0]["links_ok"] == onefichier.HISTORY_MAX_CHECKS + 2  # le plus récent d'abord
    assert historique[0]["tests_ok"] == 1


def test_route_renvoi_simulation_puis_envoi(env, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main, onefichier
    source = tmp_path / "Alpha (2001).mkv"
    source.write_bytes(b"x")
    onefichier._update(lambda d: d["entries"].update({
        "601": {"kind": "film", "status": "done", "link_ok": False, "original_path": str(source), "folder": "tmdb-601"},
    }))
    client = TestClient(main.app)
    monkeypatch.setattr(main.config.settings, "dry_run", True)
    assert client.post("/api/1fichier/entries/601/resend").json() == {"ok": True, "dry_run": True}
    assert onefichier._get_entry("601") is not None  # simulation : rien retiré

    monkeypatch.setattr(main.config.settings, "dry_run", False)
    envois = []

    async def faux_envoi(req, unlocked=True):
        envois.append(list(req.paths))
        return {"ok": True}

    monkeypatch.setattr(main, "onefichier_archive", faux_envoi)
    assert client.post("/api/1fichier/entries/601/resend").status_code == 200
    assert envois == [[str(source)]]
    assert onefichier._get_entry("601") is None
    assert client.post("/api/1fichier/entries/601/resend").status_code == 404  # plus rien à renvoyer


def test_erreur_reseau_pendant_le_test_ne_marque_pas_l_archive(env, monkeypatch):
    import asyncio
    import httpx
    from app import onefichier
    onefichier._update(lambda d: d["entries"].update({"401": {
        "kind": "film", "status": "done", "url": "https://1fichier.com/?a401", "password": "PW401-XXXXXXXXXX",
        "archive_name": "tmdb-401.7z", "folder": "tmdb-401", "remote_size": 1000, "size": 1000, "corrompu": False}}))

    async def panne(client, url):
        raise httpx.ConnectError("réseau coupé")

    monkeypatch.setattr(onefichier, "_download_url", panne)
    asyncio.run(onefichier.test_restore("401"))
    entry = onefichier._get_entry("401")
    assert entry["last_test_ok"] is False
    assert not entry.get("corrompu")  # une panne réseau ne dit rien de l'archive


def test_test_reussi_leve_la_marque_corrompu(env, monkeypatch):
    import asyncio
    from app import onefichier
    onefichier._update(lambda d: d["entries"].update({"402": {
        "kind": "film", "status": "done", "url": "https://1fichier.com/?a402", "password": "PW402-XXXXXXXXXX",
        "archive_name": "tmdb-402.7z", "folder": "tmdb-402", "remote_size": 1000, "size": 1000, "corrompu": True}}))

    async def lien(client, url):
        return "https://example.invalid/direct"

    def ecrit(direct, archive, holder):
        with open(archive, "wb") as fh:
            fh.write(b"x" * 1000)

    monkeypatch.setattr(onefichier, "_download_url", lien)
    monkeypatch.setattr(onefichier, "_download_blocking", ecrit)
    monkeypatch.setattr(onefichier, "_7z_test", lambda *a, **k: None)
    resultat = asyncio.run(onefichier.test_restore("402"))
    assert resultat["ok"] is True
    assert onefichier._get_entry("402")["corrompu"] is False
