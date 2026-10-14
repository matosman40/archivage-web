"""Configuration commune des tests : chaque test travaille dans son propre dossier temporaire
et ne touche ni à /data ni à 1fichier (les appels réseau sont remplacés)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import config  # noqa: E402


class FakeJob:
    def __init__(self):
        self.events = []
        self.after = "keep"
        self.destination = None
        self.dest_names = {}

    def emit(self, event):
        self.events.append(event)


@pytest.fixture(autouse=True)
def journal_isole(tmp_path, monkeypatch):
    """Journal, réglages, index, codes adultes et caches vont dans le dossier temporaire du test,
    jamais dans /data (version 42 : un test sans fixture env écrivait /data/adult-code.json).
    Les autres fichiers se déduisent du dossier de settings.json."""
    monkeypatch.setattr(config, "LOG_FILE", str(tmp_path / "journal" / "archivage.log"))
    monkeypatch.setattr(config, "SETTINGS_FILE", str(tmp_path / "settings.json"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Réglages isolés : index, dossier de travail, copie disque, alertes désactivées."""
    monkeypatch.setenv("ONEFICHIER_INDEX_FILE", str(tmp_path / "idx" / "1fichier-index.json"))
    monkeypatch.setattr(config, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(config.settings, "onefichier_staging_dir", str(staging))
    monkeypatch.setattr(config.settings, "archive_copy_dir", "")
    monkeypatch.setattr(config.settings, "notify_url", "")
    monkeypatch.setattr(config.settings, "dry_run", False)
    monkeypatch.setattr(config.settings, "onefichier_api_key", "CLE-DE-TEST")
    monkeypatch.setattr(type(config.settings), "tmdb_enabled", property(lambda self: True))
    return tmp_path
