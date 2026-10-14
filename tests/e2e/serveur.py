"""Serveur de test de bout en bout (version 50).

Tout vit dans E2E_BASE, un dossier temporaire fourni par lancer.sh : un disque simulé, des sources de films,
un index 1fichier d'exemple (une archive présente, une absente, une corrompue). Aucun vrai disque, aucun compte
1fichier, aucun appel réseau sortant. Radarr est joint à E2E_RADARR_URL (faux Radarr de faux_radarr.py).
"""
import json
import os
import sys

BASE = os.environ["E2E_BASE"]
PORT = int(os.environ.get("E2E_PORT", "8765"))
RADARR_URL = os.environ.get("E2E_RADARR_URL", "")
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

os.environ["SETTINGS_FILE"] = os.path.join(BASE, "settings.json")
os.environ["LOG_FILE"] = os.path.join(BASE, "archivage.log")
os.environ["ONEFICHIER_INDEX_FILE"] = os.path.join(BASE, "1fichier-index.json")
os.environ["FRONTEND_DIR"] = os.path.join(REPO, "frontend")
sys.path.insert(0, os.path.join(REPO, "backend"))

from app import config, disks, main  # noqa: E402


def preparer():
    films = os.path.join(BASE, "sources", "films")
    os.makedirs(films, exist_ok=True)
    for nom in ("Alpha (2001).mkv", "Bravo (2002).mkv"):
        with open(os.path.join(films, nom), "wb") as fh:
            fh.write(b"f" * 2048)
    racine = os.path.join(BASE, "disque")
    for sous in ("MEDIA/FILM", "MEDIA/SERIES", "MEDIA/ADULTE"):
        os.makedirs(os.path.join(racine, sous), exist_ok=True)
    for nom, octet in (("Charlie (2003).mkv", b"c"), ("Delta (2004).mkv", b"d")):
        with open(os.path.join(racine, "MEDIA/FILM", nom), "wb") as fh:
            fh.write(octet * 4096)
    index = {"version": 1, "folders": {}, "entries": {
        "101": {"status": "done", "kind": "film", "original_name": "Alpha (2001).mkv", "archive_name": "a.7z",
                "folder": "tmdb-101", "password": "x", "size": 1000, "remote_size": 1000, "link_ok": True,
                "link_checked_at": 1790000000, "archived_at": 1790000000, "created_at": 1790000000, "tmdb_id": 101},
        "102": {"status": "done", "kind": "film", "original_name": "Bravo (2002).mkv", "archive_name": "b.7z",
                "folder": "tmdb-102", "password": "x", "size": 1000, "remote_size": 1000, "link_ok": False,
                "link_checked_at": 1790000000, "archived_at": 1790000000, "created_at": 1790000000, "tmdb_id": 102,
                "original_path": os.path.join(films, "Bravo (2002).mkv")},
        "103": {"status": "done", "kind": "film", "original_name": "Charlie (2003).mkv", "archive_name": "c.7z",
                "folder": "tmdb-103", "password": "x", "size": 1000, "remote_size": 1000, "link_ok": True,
                "corrompu": True, "last_test_at": 1790000000, "last_test_ok": False,
                "last_test_error": "vérification de l'archive échouée : CRC", "archived_at": 1790000000,
                "created_at": 1790000000, "tmdb_id": 103},
    }}
    with open(os.environ["ONEFICHIER_INDEX_FILE"], "w", encoding="utf-8") as fh:
        json.dump(index, fh)
    config.update_settings({
        "source_dirs_films": [films], "source_dirs_series": [], "source_dirs_ebooks": [],
        "source_dirs_adultes": [], "dest_subdir_films": "MEDIA/FILM", "dest_subdir_series": "MEDIA/SERIES",
        "dest_subdir_ebooks": "MEDIA/EBOOKS", "dest_subdir_adultes": "MEDIA/ADULTE", "dry_run": False,
        "safety_margin_bytes": 0, "tmdb_api_key": "", "notify_url": "", "disk_folders": {},
        "radarr_url": RADARR_URL, "radarr_api_key": "CLE" if RADARR_URL else "",
        # Clé factice : active la vue 1fichier sans joindre 1fichier (le contrôle hebdomadaire n'arrive pas pendant le test).
        "onefichier_api_key": "CLE-FACTICE-E2E",
    })
    disks.list_destination_disks = lambda: [{"path": racine, "free_bytes": 10 ** 12}]


if __name__ == "__main__":
    preparer()
    import uvicorn
    uvicorn.run(main.app, host="127.0.0.1", port=PORT, log_level="warning")
