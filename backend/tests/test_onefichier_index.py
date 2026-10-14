"""Index des mots de passe : copies automatiques, export chiffré, empreinte SHA-256."""
import json
import os
import shutil
import subprocess
import time

import pytest

from app import config
from app import onefichier as of
from app import scheduler

needs_7z = pytest.mark.skipif(shutil.which("7z") is None, reason="7z absent")


def _entry(key, status="done", **extra):
    base = {"status": status, "password": f"PW-{key}-XXXXXXXX", "archive_name": f"tmdb-{key}.7z",
            "folder": f"tmdb-{key}", "folder_id": 1, "created_at": 1}
    base.update(extra)
    return base


def test_une_copie_par_minute_et_etat_precedent(env):
    of._update(lambda d: d["entries"].__setitem__("1", _entry("1")))
    of._update(lambda d: d["entries"].__setitem__("2", _entry("2", status="pending")))
    of._update(lambda d: d["entries"]["2"].__setitem__("status", "done"))
    assert of.index_status()["backups"] == 1
    copy = os.path.join(of.index_backup_dir(), sorted(os.listdir(of.index_backup_dir()))[-1])
    data = json.load(open(copy, encoding="utf-8"))
    assert "1" in data["entries"] and "2" not in data["entries"], "la copie garde l'état d'avant l'écriture"


def test_droits_des_copies(env):
    of._update(lambda d: d["entries"].__setitem__("1", _entry("1")))
    of._update(lambda d: d["entries"].__setitem__("2", _entry("2")))
    bdir = of.index_backup_dir()
    assert oct(os.stat(bdir).st_mode & 0o777) == "0o700"
    copy = os.path.join(bdir, os.listdir(bdir)[0])
    assert oct(os.stat(copy).st_mode & 0o777) == "0o600"


def test_rotation_limite_les_copies(env, monkeypatch):
    of._update(lambda d: d["entries"].__setitem__("1", _entry("1")))
    bdir = of.index_backup_dir()
    os.makedirs(bdir, exist_ok=True)
    for i in range(of.INDEX_BACKUP_KEEP + 5):
        open(os.path.join(bdir, f"1999-01-01-000000-{i:03d}.json"), "w").write("{}")
    old = time.time() - 3600
    newest = sorted(os.listdir(bdir))[-1]
    os.utime(os.path.join(bdir, newest), (old, old))
    time.sleep(1.1)  # nom de copie distinct (une seconde)
    of._update(lambda d: d.__setitem__("x", 1))
    assert of.index_status()["backups"] == of.INDEX_BACKUP_KEEP


def test_export_refuse_phrase_courte(env):
    of._update(lambda d: d["entries"].__setitem__("1", _entry("1")))
    with pytest.raises(of.OneFichierError) as err:
        of.export_index("court")
    assert err.value.status == 400


@needs_7z
def test_export_dechiffrable_avec_la_phrase_seulement(env, tmp_path):
    of._update(lambda d: d["entries"].__setitem__("1", _entry("1")))
    path, name = of.export_index("phrase-de-test-longue-42")
    assert name.endswith(".7z") and os.path.isfile(path)
    assert of._snapshot()["last_export_at"] is not None
    out = tmp_path / "dec"
    ok = subprocess.run(["7z", "x", "-y", "-bd", "-pphrase-de-test-longue-42", f"-o{out}", path], capture_output=True)
    assert ok.returncode == 0
    assert json.load(open(out / "1fichier-index.json"))["entries"]["1"]["password"] == "PW-1-XXXXXXXX"
    bad = subprocess.run(["7z", "t", "-y", "-pmauvaise", path], capture_output=True)
    assert bad.returncode != 0
    assert b"1fichier-index.json" not in open(path, "rb").read(), "en-têtes chiffrés"


@needs_7z
def test_restauration_refuse_archive_alteree_et_accepte_archive_saine(env, tmp_path, monkeypatch):
    from tests.conftest import FakeJob
    src = tmp_path / "orig" / "Film (2001)"
    src.mkdir(parents=True)
    (src / "f.mkv").write_bytes(os.urandom(100000))
    archive = tmp_path / "remote.7z"
    subprocess.run(["7z", "a", "-t7z", "-y", "-bd", "-mhe=on", "-mx=0", "-pPWRESTORE1234567", str(archive), "Film (2001)"],
                   cwd=tmp_path / "orig", check=True, capture_output=True)
    sha = of._sha256_file(str(archive))

    async def url(client, link):
        return "https://1fichier.com/dl"

    def download(link, dest, holder):
        shutil.copy(archive, dest)
        holder["done"] = os.path.getsize(dest)

    monkeypatch.setattr(of, "_download_url", url)
    monkeypatch.setattr(of, "_download_blocking", download)

    def entry(origin, digest):
        return {"key": "7", "kind": "film", "status": "done", "url": "https://1fichier.com/?z",
                "password": "PWRESTORE1234567", "archive_name": "tmdb-7.7z", "folder": "tmdb-7",
                "original_name": "Film (2001)", "original_path": str(origin / "Film (2001)"), "sha256": digest,
                "remote_size": os.path.getsize(archive), "size": os.path.getsize(archive), "show_name": None, "season": None}

    import asyncio
    good_dir = tmp_path / "good"; good_dir.mkdir()
    e = entry(good_dir, sha)
    of._update(lambda d: d["entries"].__setitem__("7", dict(e)))
    asyncio.run(of._restore_one(FakeJob(), e, "origin", None))
    assert (good_dir / "Film (2001)" / "f.mkv").exists()

    bad_dir = tmp_path / "bad"; bad_dir.mkdir()
    e2 = entry(bad_dir, "0" * 64)
    with pytest.raises(of.OneFichierError, match="SHA-256"):
        asyncio.run(of._restore_one(FakeJob(), e2, "origin", None))
    assert not (bad_dir / "Film (2001)").exists()


@needs_7z
def test_archivage_enregistre_l_empreinte_et_copie_sur_disque(env, tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "active_window", lambda now=None: {"name": "test", "onefichier_mbps": 0, "disk_mbps": 0})  # fenêtre toujours ouverte : le test ne dépend pas de l'heure
    import asyncio
    from tests.conftest import FakeJob
    copy_dir = tmp_path / "disque-copie"; copy_dir.mkdir()
    monkeypatch.setattr(config.settings, "archive_copy_dir", str(copy_dir))
    film = tmp_path / "films" / "Film Test (2001)"; film.mkdir(parents=True)
    (film / "f.mkv").write_bytes(os.urandom(200000))
    remote = tmp_path / "remote"; remote.mkdir()

    async def ident(job, kind, unit, name):
        return {"key": "9", "folder": "tmdb-9", "tmdb_id": 9, "meta": {}, "title": "Film Test", "year": 2001,
                "original_name": "Film Test (2001)"}

    async def folder(client, key, name):
        return 42

    async def server(client):
        return ("https://1fichier.com", "uid")

    def upload(node, upload_id, folder_id, path, holder):
        shutil.copy(path, remote / "tmdb-9.7z")
        holder["reader"] = type("R", (), {"done": os.path.getsize(path)})()

    async def end(node, upload_id):
        return {"size": os.path.getsize(remote / "tmdb-9.7z"), "download": "https://1fichier.com/dl/9"}

    monkeypatch.setattr(of, "_identify", ident)
    monkeypatch.setattr(of, "classify", lambda p: (of.KIND_FILM, p))
    monkeypatch.setattr(of, "_ensure_folder", folder)
    monkeypatch.setattr(of, "_upload_server", server)
    monkeypatch.setattr(of, "_upload_blocking", upload)
    monkeypatch.setattr(of, "_end_upload", end)
    asyncio.run(of._archive_one(FakeJob(), str(film), "Film Test (2001)"))
    entry = of._get_entry("9")
    assert entry["status"] == "done"
    assert of._sha256_file(str(remote / "tmdb-9.7z")) == entry["sha256"]
    assert entry["local_copy"] and of._sha256_file(entry["local_copy"]) == entry["sha256"]
    assert not any(f.endswith(".part") for f in os.listdir(copy_dir))
