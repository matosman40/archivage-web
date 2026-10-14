"""Plages de transfert, vitesse, file d'attente par priorité, attente de plage.

Aucun transfert réel : l'heure, les pauses et le débit sont simulés."""
import asyncio
import json
import os
import signal
import time
from datetime import datetime, timedelta

import shutil

import pytest

from app import config, jobs, onefichier, scheduler

MIB = 1024 * 1024


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 12, hour, minute)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Réinitialise l'état partagé du module (vitesses connues, emplacements)."""
    monkeypatch.setattr(config.settings, "dry_run", False)
    monkeypatch.setattr(config.settings, "transfer_windows", config._as_windows(config.DEFAULT_TRANSFER_WINDOWS))
    for kind in scheduler.KINDS:
        monkeypatch.setitem(scheduler._last_rate, kind, 0.0)
    scheduler._slots.clear()
    yield
    scheduler._slots.clear()


class QJob:
    """Tâche minimale : identifiant, priorité, événements."""

    def __init__(self, job_id: str, priority: str = "normal"):
        self.id = job_id
        self.priority = priority
        self.events: list[dict] = []

    def emit(self, event: dict) -> None:
        self.events.append(event)


async def settle() -> None:
    for _ in range(4):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------
# Plages horaires
# ---------------------------------------------------------------------

def test_default_windows_jour_et_nuit():
    assert scheduler.active_window(at(10))["name"] == "Jour"
    assert scheduler.active_window(at(9, 0))["name"] == "Jour"       # début inclus
    assert scheduler.active_window(at(21, 59))["name"] == "Jour"
    assert scheduler.active_window(at(22, 0)) is None                # fin exclue
    assert scheduler.active_window(at(8, 30)) is None
    assert scheduler.active_window(at(1, 0))["name"] == "Nuit"
    assert scheduler.active_window(at(6, 59))["name"] == "Nuit"
    assert scheduler.active_window(at(7, 0)) is None
    assert scheduler.active_window(at(23)) is None


def test_plage_a_cheval_sur_minuit(monkeypatch):
    monkeypatch.setattr(config.settings, "transfer_windows", config._as_windows([
        {"name": "Soir", "start": "22:00", "end": "06:00", "enabled": True},
    ]))
    assert scheduler.active_window(at(23))["name"] == "Soir"
    assert scheduler.active_window(at(0, 30))["name"] == "Soir"
    assert scheduler.active_window(at(5, 59))["name"] == "Soir"
    assert scheduler.active_window(at(6)) is None
    assert scheduler.active_window(at(12)) is None


def test_plage_24_heures_et_plage_desactivee(monkeypatch):
    monkeypatch.setattr(config.settings, "transfer_windows", config._as_windows([
        {"name": "Toujours", "start": "00:00", "end": "24:00", "enabled": True, "disk_mbps": 5},
        {"name": "Off", "start": "10:00", "end": "11:00", "enabled": False},
    ]))
    assert scheduler.active_window(at(0))["name"] == "Toujours"
    assert scheduler.active_window(at(23, 59))["name"] == "Toujours"
    assert scheduler.active_window(at(10, 30))["name"] == "Toujours"  # la plage désactivée n'est pas comptée


def test_aucune_plage_activee_means_pause(monkeypatch):
    monkeypatch.setattr(config.settings, "transfer_windows", config._as_windows([
        {"name": "Jour", "start": "09:00", "end": "22:00", "enabled": False},
    ]))
    assert scheduler.active_window(at(10)) is None
    assert scheduler.next_window(at(10)) is None


def test_prochaine_ouverture():
    seconds, window = scheduler.next_window(at(23))
    assert window["name"] == "Nuit"
    assert seconds == 2 * 3600                     # 23:00 -> 01:00
    seconds, window = scheduler.next_window(at(8))
    assert window["name"] == "Jour" and seconds == 3600


def test_plages_invalides_refusees():
    bad = [
        [{"start": "10:00", "end": "10:00"}],          # début = fin
        [{"start": "25:00", "end": "10:00"}],          # heure impossible
        [{"start": "24:00", "end": "10:00"}],          # 24:00 seulement en fin de plage
        [{"start": "9h", "end": "10:00"}],             # format
        [{"start": "09:00", "end": "10:00", "disk_mbps": -1}],
        [{"start": "09:00", "end": "10:00", "onefichier_mbps": "beaucoup"}],
        [{"start": "09:00", "end": "10:00"}] * 13,     # trop de plages
        "pas une liste",
        ["09:00-10:00"],
    ]
    for value in bad:
        with pytest.raises(ValueError):
            config._as_windows(value)


def test_plages_normalisees():
    out = config._as_windows([{"name": "  ", "start": "9:05", "end": "24:00", "onefichier_mbps": "1.5"}])
    assert out == [{"name": "Plage 1", "start": "09:05", "end": "24:00", "enabled": True,
                    "onefichier_mbps": 1.5, "disk_mbps": 0.0}]


def test_fichier_settings_invalide_ne_perd_pas_les_autres_reglages(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"dry_run": True, "transfer_windows": "n'importe quoi"}), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_FILE", str(path))
    loaded = config._load()
    assert loaded.dry_run is True                                  # les autres réglages sont gardés
    assert loaded.transfer_windows == config._as_windows(config.DEFAULT_TRANSFER_WINDOWS)


# ---------------------------------------------------------------------
# Vitesse
# ---------------------------------------------------------------------

def test_vitesse_de_la_plage_active(monkeypatch):
    assert scheduler.rate_mbps("disk", at(10)) == 30.0
    assert scheduler.rate_mbps("onefichier", at(10)) == 2.0
    assert scheduler.rate_mbps("onefichier", at(2)) == 0.0          # nuit : illimité


def test_hors_plage_garde_la_derniere_vitesse():
    scheduler.rate_mbps("onefichier", at(10))                        # plage Jour : 2 Mo/s
    assert scheduler.rate_mbps("onefichier", at(23)) == 2.0          # un envoi commencé continue à 2 Mo/s
    scheduler.rate_mbps("onefichier", at(2))                         # nuit : illimité
    assert scheduler.rate_mbps("onefichier", at(23)) == 0.0


def test_throttle_respecte_le_debit_sans_rafale():
    clock = [0.0]
    slept = []

    def fake_sleep(delay):
        slept.append(delay)
        clock[0] += delay

    throttle = scheduler.Throttle("onefichier", rate_fn=lambda: 1 * MIB,
                                  clock=lambda: clock[0], sleep=fake_sleep)
    for _ in range(4):
        throttle.wait_for(MIB)
    assert round(clock[0], 6) == 4.0                 # 4 Mio à 1 Mio/s = 4 s

    clock[0] += 100.0                                # longue pause : pas de crédit accumulé
    slept.clear()
    throttle.wait_for(MIB)
    throttle.wait_for(MIB)
    assert slept == [1.0, 1.0]


def test_throttle_illimite_ne_dort_jamais():
    slept = []
    throttle = scheduler.Throttle("disk", rate_fn=lambda: 0.0, clock=lambda: 0.0, sleep=slept.append)
    throttle.wait_for(10 * MIB)
    assert slept == []


def test_bwlimit_rsync_selon_la_plage(monkeypatch):
    monkeypatch.setattr(scheduler, "rate_mbps", lambda resource, now=None: 2.5)
    assert jobs.bwlimit_args() == ["--bwlimit=2560"]          # en Kio/s
    monkeypatch.setattr(scheduler, "rate_mbps", lambda resource, now=None: 0.0)
    assert jobs.bwlimit_args() == []
    monkeypatch.setattr(scheduler, "rate_mbps", lambda resource, now=None: 0.0001)
    assert jobs.bwlimit_args() == ["--bwlimit=1"]


def test_copie_par_blocs_passe_par_le_debit(tmp_path):
    src = tmp_path / "a.7z"
    src.write_bytes(b"x" * (3 * MIB + 5))
    dst = tmp_path / "b.7z.part"
    seen = []

    class Recorder(scheduler.Throttle):
        def __init__(self):
            pass

        def wait_for(self, nbytes):
            seen.append(nbytes)
            return 0.0

    onefichier._copy_file_throttled(str(src), str(dst), Recorder())
    assert dst.read_bytes() == src.read_bytes()
    assert sum(seen) == src.stat().st_size


# ---------------------------------------------------------------------
# File d'attente par emplacement
# ---------------------------------------------------------------------

def test_emplacement_unique_priorite_puis_ordre_d_arrivee():
    async def scenario():
        a = QJob("a")
        await scheduler.acquire(a, "onefichier")                     # a tient l'emplacement
        b, c, d = QJob("b"), QJob("c", "urgent"), QJob("d")
        tasks = {j.id: asyncio.create_task(scheduler.acquire(j, "onefichier")) for j in (b, c, d)}
        await settle()
        assert not any(t.done() for t in tasks.values())

        scheduler.release(a, "onefichier")
        await settle()
        assert [tasks[k].done() for k in "bcd"] == [False, True, False]   # l'urgente passe devant

        scheduler.release(c, "onefichier")
        await settle()
        assert [tasks[k].done() for k in "bcd"] == [True, True, False]    # puis la plus ancienne normale

        scheduler.release(b, "onefichier")
        await settle()
        assert all(t.done() for t in tasks.values())
        assert scheduler._slots["onefichier"].holders == {"d"}
        scheduler.release(d, "onefichier")
        assert scheduler._slots["onefichier"].holders == set()
        return b, c, d

    b, c, d = asyncio.run(scenario())
    # c est arrivée après b, mais sa priorité la place en tête : position 1 sur 2.
    assert c.events[0] == {"type": "job_queued", "resource": "onefichier", "label": "1fichier",
                           "position": 1, "waiting": 2, "priority": "urgent"}
    last_for_d = [e for e in d.events if e["type"] == "job_queued"][-1]
    assert last_for_d["position"] == 1 and last_for_d["waiting"] == 1


def test_changer_la_priorite_reordonne_la_file():
    async def scenario():
        a = QJob("a")
        await scheduler.acquire(a, "disk")
        b, c = QJob("b"), QJob("c")
        tb = asyncio.create_task(scheduler.acquire(b, "disk"))
        tc = asyncio.create_task(scheduler.acquire(c, "disk"))
        await settle()
        scheduler.set_priority(c, "urgent")                          # c passe devant b
        assert {"type": "job_priority", "priority": "urgent"} in c.events
        assert [e["position"] for e in c.events if e["type"] == "job_queued"][-1] == 1
        scheduler.release(a, "disk")
        await settle()
        assert tc.done() and not tb.done()
        scheduler.release(c, "disk")
        await settle()
        assert tb.done()
        scheduler.release(b, "disk")
        return scheduler._slots["disk"].holders

    assert asyncio.run(scenario()) == set()


def test_bas_passe_apres_normal():
    async def scenario():
        a = QJob("a")
        await scheduler.acquire(a, "disk")
        low, normal = QJob("low", "bas"), QJob("normal")
        tl = asyncio.create_task(scheduler.acquire(low, "disk"))
        tn = asyncio.create_task(scheduler.acquire(normal, "disk"))
        await settle()
        scheduler.release(a, "disk")
        await settle()
        assert tn.done() and not tl.done()
        scheduler.release(normal, "disk")
        await settle()
        scheduler.release(low, "disk")

    asyncio.run(scenario())


def test_annulation_en_attente_retire_de_la_file():
    async def scenario():
        a = QJob("a")
        await scheduler.acquire(a, "onefichier")
        b, c = QJob("b"), QJob("c")
        tb = asyncio.create_task(scheduler.acquire(b, "onefichier"))
        tc = asyncio.create_task(scheduler.acquire(c, "onefichier"))
        await settle()
        tb.cancel()
        await settle()
        assert tb.cancelled()
        scheduler.release(a, "onefichier")
        await settle()
        assert tc.done()                                             # c obtient l'emplacement, pas b
        scheduler.release(c, "onefichier")
        assert scheduler._slots["onefichier"].holders == set()
        assert scheduler._slots["onefichier"].waiters == []

    asyncio.run(scenario())


def test_annulation_juste_apres_attribution_rend_l_emplacement():
    async def scenario():
        a, b = QJob("a"), QJob("b")
        await scheduler.acquire(a, "onefichier")
        tb = asyncio.create_task(scheduler.acquire(b, "onefichier"))
        await settle()
        scheduler.release(a, "onefichier")                           # b reçoit l'emplacement...
        tb.cancel()                                                   # ...puis est annulée avant de reprendre
        await settle()
        assert scheduler._slots["onefichier"].holders == set()       # l'emplacement n'est pas perdu

    asyncio.run(scenario())


def test_slot_libere_meme_en_cas_d_erreur():
    async def scenario():
        job = QJob("x")
        with pytest.raises(RuntimeError):
            async with scheduler.slot(job, "disk"):
                raise RuntimeError("échec simulé")
        assert scheduler._slots["disk"].holders == set()

    asyncio.run(scenario())


def test_pas_d_evenement_quand_l_emplacement_est_libre():
    job = QJob("libre")
    asyncio.run(scheduler.acquire(job, "onefichier"))
    scheduler.release(job, "onefichier")
    assert job.events == []


def test_etat_des_files():
    async def scenario():
        a, b = QJob("a"), QJob("b", "urgent")
        await scheduler.acquire(a, "onefichier")
        tb = asyncio.create_task(scheduler.acquire(b, "onefichier"))
        await settle()
        snap = scheduler.queues_snapshot()
        scheduler.release(a, "onefichier")
        await settle()
        tb.result()
        scheduler.release(b, "onefichier")
        return snap

    snap = asyncio.run(scenario())
    assert snap["onefichier"]["running"] == 1
    assert snap["onefichier"]["waiting"] == [{"job_id": "b", "position": 1, "priority": "urgent"}]


# ---------------------------------------------------------------------
# Attente d'une plage
# ---------------------------------------------------------------------

def test_attente_jusqu_a_la_plage_puis_reprise(monkeypatch):
    clock = [at(8, 0)]
    monkeypatch.setattr(scheduler, "_now", lambda: clock[0])

    async def fake_sleep(delay):
        clock[0] = clock[0] + timedelta(seconds=delay)

    monkeypatch.setattr(scheduler, "_sleep", fake_sleep)
    job = QJob("attente")
    asyncio.run(scheduler.wait_for_window(job))
    assert clock[0] >= at(9, 0)
    assert [e["type"] for e in job.events] == ["job_wait", "job_resume"]
    assert "09:00" in job.events[0]["message"]


def test_pas_d_attente_dans_une_plage_ou_en_simulation(monkeypatch):
    monkeypatch.setattr(scheduler, "_now", lambda: at(10))
    job = QJob("dans-plage")
    asyncio.run(scheduler.wait_for_window(job))
    assert job.events == []

    monkeypatch.setattr(scheduler, "_now", lambda: at(3))
    monkeypatch.setattr(config.settings, "dry_run", True)
    asyncio.run(scheduler.wait_for_window(job))
    assert job.events == []


def test_pause_message_quand_aucune_plage(monkeypatch):
    monkeypatch.setattr(config.settings, "transfer_windows", config._as_windows([
        {"name": "Jour", "start": "09:00", "end": "22:00", "enabled": False},
    ]))
    monkeypatch.setattr(scheduler, "_now", lambda: at(10))
    assert "transferts en pause" in scheduler._wait_message()


def test_statut_pour_l_interface(monkeypatch):
    monkeypatch.setattr(scheduler, "_now", lambda: at(23))
    status = scheduler.status()
    assert status["paused"] is True and status["active"] is None
    assert status["next"]["name"] == "Nuit" and status["next"]["start"] == "01:00"
    assert isinstance(status["queues"], dict)


# ---------------------------------------------------------------------
# Tâches et file 1fichier
# ---------------------------------------------------------------------

def test_priorite_par_defaut_et_invalide():
    job = jobs.create_job("copy", "films", ["/x/a"], destination="/d", priority="urgent")
    other = jobs.create_job("copy", "films", ["/x/b"])
    try:
        assert job.priority == "urgent"
        assert other.priority == "normal"
        assert jobs.Job("id", "copy", "films", [], None, False, priority="zzz").priority == "normal"
    finally:
        jobs.JOBS.pop(job.id, None)
        jobs.JOBS.pop(other.id, None)


def test_file_1fichier_affiche_les_elements_en_attente_avec_priorite():
    job = jobs.Job("q1", "archive_1fichier", "films", ["/s/A", "/s/B"], None, False, priority="bas")
    job.status = "running"
    job.events = [{"type": "job_start", "items": ["Film A", "Film B"]},
                  {"type": "item_start", "item": "Film A"}]
    jobs.JOBS["q1"] = job
    try:
        queue = jobs.archive_queue()
        assert [q["label"] for q in queue] == ["Film B"]
        assert queue[0]["priority"] == "bas"
    finally:
        jobs.JOBS.pop("q1", None)


# ---------------------------------------------------------------------
# Intégration : tâches complètes (rsync réel, envois 1fichier simulés)
# ---------------------------------------------------------------------



@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync requis")
def test_copie_disque_passe_par_la_file_et_respecte_la_plage(tmp_path, monkeypatch):
    src = tmp_path / "src" / "Film.mkv"
    src.parent.mkdir()
    src.write_bytes(b"0123456789" * 5000)
    dest = tmp_path / "disque" / "MEDIA"
    monkeypatch.setattr(scheduler, "_now", lambda: at(10))           # plage Jour : 30 Mo/s
    monkeypatch.setattr(config, "LOG_FILE", str(tmp_path / "log.txt"))

    job = jobs.Job("copie-1", "copy", "films", [str(src)], str(dest), False)
    asyncio.run(jobs.run_job(job))

    assert (dest / "Film.mkv").read_bytes() == src.read_bytes()
    assert job.status == "done"
    assert job.events[-1]["summary"]["ok"] == 1
    assert jobs.bwlimit_args() == ["--bwlimit=30720"]


def test_envois_1fichier_un_a_la_fois_et_priorite_urgente(env, monkeypatch):
    monkeypatch.setattr(scheduler, "_now", lambda: at(10))
    order: list[str] = []

    async def fake_archive(job, item, name):
        order.append(f"{job.id}:{name}")
        await asyncio.sleep(0.01)

    monkeypatch.setattr(onefichier, "_archive_one", fake_archive)
    first = jobs.Job("j1", "archive_1fichier", "films", ["/f/A", "/f/B", "/f/C"], None, False)
    urgent = jobs.Job("j2", "archive_1fichier", "films", ["/f/X"], None, False, priority="urgent")

    async def scenario():
        t1 = asyncio.create_task(jobs.run_job(first))
        await asyncio.sleep(0.001)                                    # j1 tient l'emplacement
        t2 = asyncio.create_task(jobs.run_job(urgent))
        await asyncio.gather(t1, t2)

    asyncio.run(scenario())
    # La tâche urgente prend l'emplacement dès que l'élément en cours est fini, avant les suivants de j1.
    assert order == ["j1:A", "j2:X", "j1:B", "j1:C"]
    assert [e["type"] for e in urgent.events if e["type"] == "job_queued"][:1] == ["job_queued"]


# ---------------------------------------------------------------------
# v32 : un emplacement par disque, pause et relance des copies, vérification par empreinte
# ---------------------------------------------------------------------

def test_un_emplacement_par_disque():
    async def scenario():
        a, b, c = QJob("a"), QJob("b"), QJob("c")
        await scheduler.acquire(a, "disk", "/media/u/Disque1")
        await scheduler.acquire(b, "disk", "/media/u/Disque2")   # un autre disque : pas d'attente
        assert scheduler._slots["disk:/media/u/Disque1"].holders == {"a"}
        assert scheduler._slots["disk:/media/u/Disque2"].holders == {"b"}
        tc = asyncio.create_task(scheduler.acquire(c, "disk", "/media/u/Disque1"))
        await settle()
        assert not tc.done()                                     # même disque que a : elle attend
        scheduler.release(a, "disk", "/media/u/Disque1")
        await settle()
        assert tc.done()
        return c

    c = asyncio.run(scenario())
    assert c.events[0]["label"] == "disque Disque1"


class FakeProc:
    """Processus factice : enregistre les signaux, se termine après `finish_at` secondes simulées."""

    def __init__(self):
        self.returncode = None
        self.signals: list[int] = []

    def send_signal(self, sig):
        self.signals.append(sig)
        if sig == signal.SIGTERM:
            self.returncode = -15


def _schedule(monkeypatch, timeline, finish_at=10.0):
    """timeline : fonction(t) -> plage active ou None ; le temps simulé avance avec les attentes.
    Le processus factice se termine à `finish_at` secondes simulées."""
    clock = [0.0]
    proc = FakeProc()

    def fake_active(now=None):
        return timeline(clock[0])

    async def fake_sleep(delay):
        clock[0] += delay
        if clock[0] >= finish_at:
            proc.returncode = 0

    monkeypatch.setattr(scheduler, "active_window", fake_active)
    monkeypatch.setattr(scheduler, "_sleep", fake_sleep)
    monkeypatch.setattr(scheduler, "_now", lambda: at(12))
    return clock, proc


def test_copie_suspendue_hors_plage_puis_reprise_a_meme_vitesse(monkeypatch):
    jour = {"name": "Jour", "onefichier_mbps": 2, "disk_mbps": 1.0}
    clock, proc = _schedule(monkeypatch, lambda t: jour if t < 5 or 8 <= t < 20 else None, finish_at=10.0)
    monkeypatch.setattr(scheduler, "_last_rate", {"onefichier": 0.0, "disk": 1.0})
    job = QJob("pause")
    result = asyncio.run(scheduler.control_process(job, proc, "disk", started_rate=1.0))
    # Hors plage de 5 à 8 s : SIGSTOP puis SIGCONT. Même vitesse à la reprise : pas de relance.
    assert result == "done"
    assert proc.signals[:2] == [signal.SIGSTOP, signal.SIGCONT]
    assert [e["type"] for e in job.events] == ["job_wait", "job_resume"]
    assert job.events[0]["message"].startswith("Copie suspendue.")


def test_copie_relancee_quand_la_vitesse_change(monkeypatch):
    jour = {"name": "Jour", "onefichier_mbps": 2, "disk_mbps": 30.0}
    nuit = {"name": "Nuit", "onefichier_mbps": 0, "disk_mbps": 0.0}
    clock, proc = _schedule(monkeypatch, lambda t: jour if t < 3 else (None if t < 5 else nuit), finish_at=60.0)
    monkeypatch.setattr(scheduler, "_last_rate", {"onefichier": 0.0, "disk": 30.0})
    job = QJob("relance")
    result = asyncio.run(scheduler.control_process(job, proc, "disk", started_rate=30.0))
    assert result == "restart"
    assert proc.signals == [signal.SIGSTOP, signal.SIGCONT, signal.SIGTERM]
    assert clock[0] == pytest.approx(5.0, abs=1.5)


def test_copie_terminee_sans_intervention(monkeypatch):
    jour = {"name": "Jour", "onefichier_mbps": 2, "disk_mbps": 30.0}
    clock, proc = _schedule(monkeypatch, lambda t: jour, finish_at=2.0)
    job = QJob("fin")
    assert asyncio.run(scheduler.control_process(job, proc, "disk", started_rate=30.0)) == "done"
    assert proc.signals == []


def _tiny_tree(root):
    os.makedirs(os.path.join(root, "sub"))
    with open(os.path.join(root, "a.bin"), "wb") as fh:
        fh.write(b"A" * 5000)
    with open(os.path.join(root, "sub", "b.bin"), "wb") as fh:
        fh.write(b"B" * 3000)


def test_verification_par_empreinte_detecte_une_copie_alteree(tmp_path):
    from app import jobs as jobs_mod
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _tiny_tree(str(src))
    import shutil
    shutil.copytree(src, dst)
    ok, _ = jobs_mod._verify_copy(str(src), str(dst))
    assert ok
    # Même taille, contenu différent : la vérification par taille ne le verrait pas.
    with open(dst / "sub" / "b.bin", "r+b") as fh:
        fh.write(b"X")
    ok, detail = jobs_mod._verify_copy(str(src), str(dst))
    assert not ok and "b.bin" in detail
    assert jobs_mod._covered(str(src), str(dst))  # la seule taille reste identique
    assert jobs_mod._verify_copy(str(src), str(tmp_path / "absent"))[0] is False


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync requis")
def test_deplacement_retire_la_source_seulement_apres_verification(tmp_path, monkeypatch):
    src = tmp_path / "src" / "Film.mkv"
    src.parent.mkdir()
    src.write_bytes(b"0123456789" * 3000)
    dest = tmp_path / "disque"
    dest.mkdir()
    monkeypatch.setattr(scheduler, "_now", lambda: at(10))
    from app import jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "_verify_copy", lambda s, d: (False, "essai : empreintes différentes"))

    job = jobs_mod.Job("mv-1", "move", "films", [str(src)], str(dest), False)
    asyncio.run(jobs_mod.run_job(job))
    assert src.exists()                                              # source conservée
    assert not (dest / "Film.mkv").exists()                          # copie incomplète retirée
    assert any(e["type"] == "item_error" for e in job.events)


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync requis")
def test_deplacement_verifie_puis_retire_la_source(tmp_path, monkeypatch):
    src = tmp_path / "src" / "Film.mkv"
    src.parent.mkdir()
    src.write_bytes(b"0123456789" * 3000)
    dest = tmp_path / "disque"
    dest.mkdir()
    monkeypatch.setattr(scheduler, "_now", lambda: at(10))
    # Un déplacement ne porte que sur un dossier source (et la suppression de la source en est gardée).
    monkeypatch.setattr(config.settings, "source_dirs_films", [str(src.parent)])
    from app import jobs as jobs_mod
    job = jobs_mod.Job("mv-2", "move", "films", [str(src)], str(dest), False)
    asyncio.run(jobs_mod.run_job(job))
    assert not src.exists()
    assert (dest / "Film.mkv").read_bytes() == b"0123456789" * 3000
    assert job.events[-1]["summary"]["ok"] == 1


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync requis")
def test_copie_disque_suspendue_puis_relancee_avec_rsync(tmp_path, monkeypatch):
    """Vrai rsync : 1 Mo/s en journée, plage fermée de 1,5 s à 3 s, puis plage sans limite :
    la copie est suspendue, puis relancée, et le fichier final est identique à la source."""
    src = tmp_path / "src" / "Gros.mkv"
    src.parent.mkdir()
    data = os.urandom(6 * MIB)
    src.write_bytes(data)
    dest = tmp_path / "disque"
    dest.mkdir()
    t0 = time.monotonic()

    def timeline(*_args, **_kw):
        t = time.monotonic() - t0
        if t < 1.5:
            return {"name": "Jour", "onefichier_mbps": 2, "disk_mbps": 1.0, "end": "22:00"}
        if t < 3.0:
            return None
        return {"name": "Nuit", "onefichier_mbps": 0, "disk_mbps": 0.0, "end": "07:00"}

    monkeypatch.setattr(scheduler, "active_window", timeline)
    monkeypatch.setattr(scheduler, "PAUSE_TICK_SECONDS", 0.05)
    monkeypatch.setattr(scheduler, "_now", lambda: at(12))
    from app import jobs as jobs_mod
    job = jobs_mod.Job("pause-rsync", "copy", "films", [str(src)], str(dest), False)
    asyncio.run(jobs_mod.run_job(job))

    assert (dest / "Gros.mkv").read_bytes() == data
    types = [e["type"] for e in job.events]
    assert "job_wait" in types and "job_resume" in types
    assert any("relancée" in e.get("line", "") for e in job.events if e["type"] == "item_log")
    assert job.events[-1]["summary"]["ok"] == 1
