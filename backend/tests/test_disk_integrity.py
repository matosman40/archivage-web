"""Empreintes et vérification d'intégrité des disques."""
import asyncio
import os

import pytest

from app import disk_integrity as di
from app import disks, jobs


@pytest.fixture
def disk(env, monkeypatch, tmp_path):
    root = tmp_path / "disque"
    films = root / "MEDIA" / "FILM"
    (films / "A (2001)").mkdir(parents=True)
    (films / "B (2002)").mkdir(parents=True)
    (root / "MEDIA" / "SERIES").mkdir(parents=True)
    (films / "A (2001)" / "a.mkv").write_bytes(b"A" * 100000)
    (films / "B (2002)" / "b.mkv").write_bytes(b"B" * 100000)
    (root / "MEDIA" / "SERIES" / "c.mkv").write_bytes(b"C" * 5000)
    monkeypatch.setattr(disks, "list_destination_disks", lambda: [{"path": str(root)}])
    return root


def _wait():
    async def loop():
        while di._state["running"]:
            await asyncio.sleep(0.02)
    asyncio.run(loop())


def test_empreintes_puis_verification_sans_probleme(disk):
    assert asyncio.run(di.start(str(disk), "build")) is True
    _wait()
    manifest = di._read_manifest(str(disk))
    assert len(manifest["files"]) == 3
    assert oct(os.stat(di._manifest_path(str(disk))).st_mode & 0o777) == "0o600"
    report = di.verify_blocking(str(disk))
    assert report["ok"] == 3 and report["corrupt"] == 0 and report["missing"] == 0


def test_corruption_silencieuse_distinguee_des_modifications(disk):
    root = str(disk)
    di.build_blocking(root)
    b = disk / "MEDIA" / "FILM" / "B (2002)" / "b.mkv"
    c = disk / "MEDIA" / "SERIES" / "c.mkv"
    a = disk / "MEDIA" / "FILM" / "A (2001)" / "a.mkv"
    st = os.stat(b)
    data = bytearray(b.read_bytes()); data[1000] ^= 0x01; b.write_bytes(bytes(data))
    os.utime(b, (st.st_atime, st.st_mtime))          # même taille, même date : corruption
    c.write_bytes(b"C" * 6000)                        # modifié
    a.unlink()                                        # absent
    (disk / "MEDIA" / "FILM" / "new.mkv").write_bytes(b"N")  # nouveau
    report = di.verify_blocking(root)
    assert report["corrupt_examples"] == ["MEDIA/FILM/B (2002)/b.mkv"]
    assert report["modified_examples"] == ["MEDIA/SERIES/c.mkv"]
    assert report["missing"] == 1 and report["new"] == 1
    assert di._read_manifest(root)["last_verify"]["corrupt"] == 1


def test_reconstruction_retire_absents_et_ajoute_nouveaux(disk):
    root = str(disk)
    di.build_blocking(root)
    (disk / "MEDIA" / "FILM" / "A (2001)" / "a.mkv").unlink()
    (disk / "MEDIA" / "FILM" / "new.mkv").write_bytes(b"N")
    di.build_blocking(root)
    files = di._read_manifest(root)["files"]
    assert "MEDIA/FILM/A (2001)/a.mkv" not in files and "MEDIA/FILM/new.mkv" in files


def test_refus_disque_non_connecte_ou_copie_en_cours(disk, monkeypatch):
    monkeypatch.setattr(disks, "list_destination_disks", lambda: [])
    with pytest.raises(di.IntegrityError, match="non détecté"):
        asyncio.run(di.start(str(disk), "verify"))
    monkeypatch.setattr(disks, "list_destination_disks", lambda: [{"path": str(disk)}])
    monkeypatch.setitem(jobs.JOBS, "x", type("J", (), {"status": "running"})())
    try:
        with pytest.raises(di.IntegrityError, match="en cours"):
            asyncio.run(di.start(str(disk), "verify"))
    finally:
        jobs.JOBS.pop("x", None)


def test_verification_sans_empreintes_demande_un_calcul_d_abord(disk):
    with pytest.raises(di.IntegrityError, match="Empreintes"):
        di.verify_blocking(str(disk))
