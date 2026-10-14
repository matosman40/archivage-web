"""Exécution asynchrone des opérations copier/déplacer/supprimer.

Chaque job tourne en tâche de fond (asyncio) et diffuse sa progression aux
clients WebSocket abonnés (un ou plusieurs onglets), pour ne jamais bloquer
une requête HTTP le temps d'un rsync de plusieurs dizaines de Go.

Règle centrale : un élément n'est marqué ✓ (ou ne voit sa source supprimée)
que si le résultat est VÉRIFIÉ. Aucun échec ne doit plus passer en silence :
- la suppression d'un dossier ne ignore plus les erreurs (rmtree ignore_errors
  reportait un faux succès quand un fichier ne pouvait pas être effacé) ;
- un déplacement dont la copie existe déjà à l'identique retire bien la source
  (avant, il était simplement ignoré et la source restait en place) ;
- avant de retirer une source, on vérifie que chaque fichier est présent à
  destination avec la même taille.
"""
import asyncio
import hashlib
import json
import os
import re
import signal
import time
import uuid
from typing import Optional

from . import config, disks, onefichier, scheduler, security
from .logs import log_action

PROGRESS_RE = re.compile(r"(\d+)%")

# -a serait trop strict pour des disques exFAT/NTFS/FAT : rsync tenterait de
# chown/chmod et renverrait l'exit code 23 alors que les fichiers sont copiés.
# On garde contenu, liens et dates (les dates servent à ne pas recopier).
# Transferts partiels (version 45) : rsync écrit un fichier interrompu DANS ce dossier caché, pas sous
# son nom final. Une coupure ou un disque plein ne laisse donc jamais un film tronqué que la bibliothèque
# prendrait pour complet. À la relance, rsync reprend depuis ce dossier, puis le supprime.
PARTIAL_DIR = ".archivage-partiel"

RSYNC_OPTS = [
    "-rlt",
    "--no-perms", "--no-owner", "--no-group",
    "--modify-window=2",   # FAT/exFAT : dates à 2 s près
    f"--partial-dir={PARTIAL_DIR}",
    # Un transfert depuis un disque ne doit jamais ramener les restes partiels d'une copie interrompue.
    f"--exclude={PARTIAL_DIR}/",
    "--info=progress2",
]


def _walk(path: str):
    """os.walk qui ignore le dossier des transferts partiels (voir PARTIAL_DIR)."""
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d != PARTIAL_DIR]
        yield root, dirs, files


def _taille(path: str) -> int:
    """Taille d'une entrée sans suivre les liens : un lien (même cassé) a sa propre taille."""
    return os.lstat(path).st_size


def _readonly_hint(exc: BaseException) -> str:
    """Explication lisible quand l'échec vient d'un montage en lecture seule
    ou de droits insuffisants — cause la plus fréquente de suppression qui
    ne marche pas."""
    is_readonly = isinstance(exc, PermissionError) or (
        isinstance(exc, OSError) and getattr(exc, "errno", None) == 30  # EROFS
    )
    if not is_readonly:
        return ""
    return (
        " Cause probable : le dossier source est monté en lecture seule (:ro) "
        "ou tes droits ne permettent pas d'y supprimer. Passe-le en :rw dans "
        "docker-compose.yml puis `docker compose up -d`."
    )


class Job:
    def __init__(self, job_id: str, operation: str, media_type: str,
                 items: list[str], destination: Optional[str], merge: bool,
                 dest_names: Optional[dict] = None, dest_dirs: Optional[dict] = None,
                 priority: str = scheduler.DEFAULT_PRIORITY, adult: bool = False):
        self.id = job_id
        # Tâche touchant un film adulte : masquée des listes tant que la session est verrouillée.
        self.adult = bool(adult)
        # Élément (chemin source) -> nom de la COPIE à destination (films et séries identifiés).
        self.dest_names: dict[str, str] = dict(dest_names or {})
        # Élément -> dossier de destination propre (une saison va dans le dossier de sa série).
        self.dest_dirs: dict[str, str] = dict(dest_dirs or {})
        self.operation = operation
        self.media_type = media_type
        self.items = items
        self.destination = destination
        self.merge = merge
        self.status = "pending"
        # Priorité dans la file d'attente (urgent, normal, bas) : relue à chaque élément.
        self.priority = priority if priority in scheduler.PRIORITIES else scheduler.DEFAULT_PRIORITY
        self.created_at = time.time()
        self.subscribers: list[asyncio.Queue] = []
        self.events: list[dict] = []
        # Tâche reprise après un redémarrage du serveur (version 45) : les éléments déjà traités sont passés.
        self.resumed = False
        # Espace réservé sur le disque de destination, par élément (version 45).
        self.reservation: dict[str, int] = {}
        self.reservation_dev: Optional[int] = None

    def emit(self, event: dict) -> None:
        event = {**event, "ts": time.time()}
        self.events.append(event)
        for q in list(self.subscribers):
            q.put_nowait(event)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        for e in self.events:
            q.put_nowait(e)
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self.subscribers:
            self.subscribers.remove(q)


JOBS: dict[str, Job] = {}


def get_job(job_id: str) -> Optional[Job]:
    return JOBS.get(job_id)


def adult_ids() -> frozenset:
    """Identifiants des tâches qui touchent un film adulte (masqués sans session déverrouillée)."""
    return frozenset(j.id for j in JOBS.values() if j.adult)


def list_jobs(hidden: frozenset = frozenset()) -> list[dict]:
    return [
        {
            "id": j.id,
            "operation": j.operation,
            "media_type": j.media_type,
            "status": j.status,
            "priority": j.priority,
            "created_at": j.created_at,
            "item_count": len(j.items),
        }
        for j in sorted(JOBS.values(), key=lambda x: x.created_at, reverse=True)
        if j.id not in hidden
    ]


JOB_KEEP_FINISHED = 300  # tâches terminées conservées (les plus récentes) pour l'historique


def _purge_finished_jobs() -> None:
    """Retire les tâches terminées au-delà de JOB_KEEP_FINISHED, les plus anciennes d'abord.
    Sans cela, JOBS grossissait sans fin, et list_jobs / adult_ids parcouraient tout l'historique."""
    finished = [j for j in JOBS.values() if j.status == "done"]
    excess = len(finished) - JOB_KEEP_FINISHED
    if excess > 0:
        for j in sorted(finished, key=lambda x: x.created_at)[:excess]:
            JOBS.pop(j.id, None)


def create_job(operation: str, media_type: str, items: list[str],
               destination: Optional[str] = None, merge: bool = False,
               dest_names: Optional[dict] = None, dest_dirs: Optional[dict] = None,
               priority: str = scheduler.DEFAULT_PRIORITY, adult: bool = False) -> Job:
    job_id = uuid.uuid4().hex[:12]
    job = Job(job_id, operation, media_type, items, destination, merge, dest_names, dest_dirs, priority,
              adult=adult)
    _purge_finished_jobs()
    JOBS[job_id] = job
    persist_pending()
    return job


# ---------------------------------------------------------------------
# Espace réservé (version 45)
# ---------------------------------------------------------------------
# Le contrôle d'espace fait à la création d'une tâche ne suffit pas : deux tâches créées l'une après
# l'autre voyaient le même espace libre, et la seconde échouait en pleine copie. Chaque tâche réserve
# donc la taille de ses éléments sur le disque (identifié par son périphérique) jusqu'à leur traitement.

_RESERVED: dict[int, int] = {}  # périphérique -> octets réservés par les tâches en cours


def device_of(path: str) -> Optional[int]:
    """Périphérique qui porte `path` (ou son plus proche ancêtre existant)."""
    courant = os.path.normpath(path)
    while not os.path.exists(courant) and os.path.dirname(courant) != courant:
        courant = os.path.dirname(courant)
    try:
        return os.stat(courant).st_dev
    except OSError:
        return None


def reserved_bytes(dev: Optional[int]) -> int:
    return _RESERVED.get(dev, 0) if dev is not None else 0


def reserve_for_job(job: Job) -> None:
    """Réserve l'espace nécessaire aux éléments d'une copie ou d'un déplacement."""
    if config.settings.dry_run or job.operation not in ("copy", "move") or not job.destination:
        return
    dev = device_of(job.destination)
    if dev is None:
        return
    sizes = {}
    for item in job.items:
        try:
            sizes[item] = sum(_files_under(item).values())
        except OSError:
            sizes[item] = 0
    job.reservation = sizes
    job.reservation_dev = dev
    _RESERVED[dev] = _RESERVED.get(dev, 0) + sum(sizes.values())


def _liberer_reservation(job: Job, item: str) -> None:
    size = job.reservation.pop(item, 0)
    dev = job.reservation_dev
    if size and dev is not None:
        restant = _RESERVED.get(dev, 0) - size
        if restant > 0:
            _RESERVED[dev] = restant
        else:
            _RESERVED.pop(dev, None)


def _liberer_toutes_reservations(job: Job) -> None:
    for item in list(job.reservation):
        _liberer_reservation(job, item)


# ---------------------------------------------------------------------
# Tâches en attente, persistées (version 45)
# ---------------------------------------------------------------------
# Les tâches non terminées sont écrites dans un fichier à côté des réglages (volume /data). Après un
# redémarrage du serveur (par ex. `docker compose up -d --build`), elles reprennent : une copie reprend
# sa partie déjà transférée, un déplacement ne retire la source qu'après vérification.

PENDING_FILE_NAME = "jobs-pending.json"
# Envois 1fichier compris (version 46) : un envoi interrompu est relancé au redémarrage.
PERSISTED_OPERATIONS = ("copy", "move", "delete", "archive_1fichier")


def pending_file() -> str:
    return os.path.join(os.path.dirname(config.SETTINGS_FILE) or ".", PENDING_FILE_NAME)


def persist_pending() -> None:
    """Écrit les tâches copie, déplacement et suppression non terminées (écriture atomique)."""
    entries = [
        {
            "id": j.id,
            "operation": j.operation,
            "media_type": j.media_type,
            "items": list(j.items),
            "destination": j.destination,
            "merge": j.merge,
            "dest_names": dict(j.dest_names),
            "dest_dirs": dict(j.dest_dirs),
            "priority": j.priority,
            "adult": j.adult,
            "after": getattr(j, "after", "keep"),
        }
        for j in JOBS.values()
        if j.status != "done" and j.operation in PERSISTED_OPERATIONS
    ]
    path = pending_file()
    tmp = f"{path}.tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(entries, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        log_action("ECHEC", "FILE", f"sauvegarde des tâches en attente impossible ({exc})")


# Fichier temporaire de rsync : « .nom.XXXXXX » (six caractères aléatoires) dans le dossier de destination.
_TEMPORAIRE_RE = re.compile(r"^\.(?P<nom>.+)\.[A-Za-z0-9]{6}$")


def _recuperer_temporaires(job: Job) -> None:
    """Après un arrêt BRUTAL (coupure, SIGKILL), rsync laisse à destination un fichier caché « .nom.XXXXXX »
    au lieu de le ranger dans le dossier partiel. Il contient le début de la copie, qui n'a donc pas à être
    refaite : s'il n'existe pas encore de fichier final, il est rangé dans le dossier partiel, où la relance
    le prend comme point de départ (voir « --no-whole-file » dans _transfert_verifie). Si le fichier final
    existe déjà (fusion), le temporaire est retiré. Un fichier caché sans rapport n'est jamais touché."""
    for item in job.items:
        dest_dir = job.dest_dirs.get(item, job.destination)
        if not dest_dir:
            continue
        nom = job.dest_names.get(item) or _name(item)
        dest_path = os.path.join(dest_dir, nom)
        if os.path.isdir(dest_path) and not os.path.islink(dest_path):
            # Dossier : les temporaires sont dans ses sous-dossiers, chacun avec son propre dossier partiel.
            for root, _dirs, files in _walk(dest_path):
                for entree in files:
                    _traiter_temporaire(job, root, entree)
        else:
            try:
                entrees = os.listdir(dest_dir)
            except OSError:
                continue
            for entree in entrees:
                m = _TEMPORAIRE_RE.match(entree)
                if m and m.group("nom") == nom:
                    _traiter_temporaire(job, dest_dir, entree)


def _traiter_temporaire(job: Job, dossier: str, entree: str) -> None:
    m = _TEMPORAIRE_RE.match(entree)
    if not m:
        return
    chemin = os.path.join(dossier, entree)
    if os.path.islink(chemin) or not os.path.isfile(chemin):
        return
    final = m.group("nom")
    try:
        if os.path.lexists(os.path.join(dossier, final)):
            os.remove(chemin)
            log_action("NETTOYAGE", job.operation.upper(), f"{chemin} (fichier final déjà présent)")
            return
        partiel = os.path.join(dossier, PARTIAL_DIR)
        os.makedirs(partiel, exist_ok=True)
        os.replace(chemin, os.path.join(partiel, final))
        log_action("REPRISE", job.operation.upper(), f"{chemin} rangé pour reprise (arrêt brutal)")
    except OSError as exc:
        log_action("ECHEC", "REPRISE", f"{chemin} ({exc})")


def restore_pending() -> list[Job]:
    """Recrée les tâches non terminées au démarrage. Renvoie celles qu'il faut lancer."""
    try:
        with open(pending_file(), "r", encoding="utf-8") as fh:
            entries = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(entries, list):
        return []
    restored: list[Job] = []
    for e in entries:
        try:
            job = Job(str(e["id"]), str(e["operation"]), str(e["media_type"]), [str(i) for i in e["items"]],
                      e.get("destination"), bool(e.get("merge")), e.get("dest_names") or {},
                      e.get("dest_dirs") or {}, e.get("priority", scheduler.DEFAULT_PRIORITY),
                      adult=bool(e.get("adult")))
        except (KeyError, TypeError, ValueError):
            continue
        job.resumed = True
        if job.operation == "archive_1fichier":
            job.after = str(e.get("after") or "keep")
        JOBS[job.id] = job
        if job.operation in ("copy", "move"):
            _recuperer_temporaires(job)
        reserve_for_job(job)
        restored.append(job)
        log_action("REPRISE", job.operation.upper(),
                   f"tâche {job.id} reprise après redémarrage ({len(job.items)} élément(s))")
    persist_pending()
    return restored


# ---------------------------------------------------------------------
# Helpers fichiers
# ---------------------------------------------------------------------

def _name(item: str) -> str:
    return os.path.basename(item.rstrip("/"))


def _files_under(path: str) -> dict[str, int]:
    """Chemin relatif -> taille, pour un fichier seul ou un dossier entier."""
    if os.path.isfile(path):
        return {os.path.basename(path): os.path.getsize(path)}
    result: dict[str, int] = {}
    for root, _dirs, files in _walk(path):
        for f in files:
            full = os.path.join(root, f)
            result[os.path.relpath(full, path)] = _taille(full)
    return result


def _covered(src: str, dst: str) -> bool:
    """True si TOUT ce qui se trouve dans `src` existe dans `dst` avec la même
    taille. Sert de vérification avant de supprimer une source."""
    try:
        if os.path.isdir(src) != os.path.isdir(dst):
            return False
        if os.path.isfile(src):
            # Un fichier seul se compare à la taille, même si la copie porte un autre nom (renommage).
            return os.path.getsize(src) == os.path.getsize(dst)
        wanted = _files_under(src)
        if not wanted:
            return os.path.isdir(dst)
        have = _files_under(dst)
    except OSError:
        return False
    return all(have.get(rel) == size for rel, size in wanted.items())


def _chemins_sous(path: str) -> dict[str, str]:
    """Chemin relatif -> chemin complet, pour un fichier seul ou un dossier entier."""
    if os.path.isfile(path):
        return {os.path.basename(path): path}
    return {os.path.relpath(os.path.join(root, f), path): os.path.join(root, f)
            for root, _dirs, files in _walk(path) for f in files}


def _meme_debut(a: str, b: str, octets: int) -> bool:
    """Les `octets` premiers octets de a et de b sont identiques (lecture par morceaux)."""
    with open(a, "rb") as fa, open(b, "rb") as fb:
        restant = octets
        while restant > 0:
            morceau = min(restant, 8 << 20)
            if fa.read(morceau) != fb.read(morceau):
                return False
            restant -= morceau
    return True


def _copie_partielle(src: str, dst: str) -> bool:
    """True si `dst` est une copie INTERROMPUE de `src` (version 42) : chaque fichier présent à
    destination est soit de même taille, soit plus court et commence par les mêmes octets que la
    source. Un fichier différent n'est jamais pris pour une copie partielle : il n'est pas écrasé."""
    try:
        if os.path.isdir(src) != os.path.isdir(dst):
            return False
        if os.path.isfile(src):
            # Un fichier seul : on compare les deux fichiers, quel que soit leur nom (renommage).
            paires, manquants = [(src, dst)], False
        else:
            wanted, have = _chemins_sous(src), _chemins_sous(dst)
            if any(rel not in wanted for rel in have):
                return False  # un fichier en trop : ce n'est pas notre copie
            paires = [(wanted[rel], chemin_dst) for rel, chemin_dst in have.items()]
            manquants = len(have) < len(wanted)
        tronques = 0
        for chemin_src, chemin_dst in paires:
            taille_src, taille_dst = os.path.getsize(chemin_src), os.path.getsize(chemin_dst)
            if taille_dst > taille_src:
                return False
            if taille_dst == taille_src:
                # Même taille : rsync ne le réécrit que si la date diffère. Pour ne jamais
                # écraser un fichier différent, on exige la même date (à la seconde près).
                if abs(os.path.getmtime(chemin_src) - os.path.getmtime(chemin_dst)) >= 1:
                    return False
                continue
            if not _meme_debut(chemin_src, chemin_dst, taille_dst):
                return False
            tronques += 1
        return tronques > 0 or manquants
    except OSError:
        return False


def _remove_path(path: str) -> None:
    """Supprime un fichier ou un dossier entier.

    Lève OSError si QUOI QUE CE SOIT n'a pas pu être supprimé — contrairement
    à shutil.rmtree(ignore_errors=True), qui laissait croire à un succès."""
    if os.path.islink(path) or os.path.isfile(path):
        os.remove(path)
        return
    if not os.path.isdir(path):
        return  # déjà absent

    errors: list[OSError] = []

    def _on_walk_error(exc: OSError) -> None:
        errors.append(exc)

    for root, dirs, files in os.walk(path, topdown=False, onerror=_on_walk_error):
        for f in files:
            full = os.path.join(root, f)
            try:
                os.remove(full)
            except OSError as exc:
                errors.append(exc)
        for d in dirs:
            full = os.path.join(root, d)
            try:
                if os.path.islink(full):
                    os.remove(full)
                else:
                    os.rmdir(full)
            except OSError as exc:
                errors.append(exc)

    try:
        os.rmdir(path)
    except OSError as exc:
        errors.append(exc)

    if errors:
        first = errors[0]
        more = f" (+{len(errors) - 1} autre(s))" if len(errors) > 1 else ""
        raise OSError(first.errno, f"{first.strerror or first}{more} — ex. {first.filename or ''}".rstrip())


def _suppression_autorisee(item: str) -> bool:
    """Garde à l'exécution (défense en profondeur) : un élément ne se supprime que s'il est
    strictement à l'intérieur d'un dossier source, ou s'il est un élément d'un disque CONNECTÉ
    (bibliothèque des disques). La création de la tâche le vérifie déjà ; cette garde couvre une
    tâche enregistrée avec un chemin hors de ces zones, ou une racine de montage."""
    if security.is_inside_source_dirs(item):
        return True
    from . import disk_library  # import tardif : disk_library dépend déjà de disks
    return disk_library.locate(os.path.normpath(item)) is not None


def _fail(job: Job, item: str, reason: str, exc: Optional[BaseException] = None) -> None:
    name = _name(item)
    hint = _readonly_hint(exc) if exc is not None else ""
    log_action("ECHEC", job.operation.upper(), f"{item} ({reason})")
    job.emit({"type": "item_error", "item": name, "error": f"{reason}.{hint}"})


# ---------------------------------------------------------------------
# Exécution
# ---------------------------------------------------------------------

def archive_queue() -> list[dict]:
    """Éléments des envois 1fichier EN ATTENTE (pas encore commencés), dans l'ordre :
    de quoi afficher la file d'attente à côté de l'envoi en cours."""
    out = []
    for job in JOBS.values():
        if job.operation != "archive_1fichier" or job.status != "running":
            continue
        labels: list[str] = []
        started: set[str] = set()
        for e in job.events:
            if e["type"] == "job_start":
                labels = list(e.get("items") or [])
            elif e["type"] == "item_start":
                started.add(e["item"])
        if len(labels) != len(job.items):
            continue  # libellés indisponibles : on ne sait pas les montrer
        for pos, (label, path) in enumerate(zip(labels, job.items), start=1):
            if label in started:
                continue
            out.append({"job_id": job.id, "label": label, "source": path,
                        "position": pos, "total": len(job.items), "priority": job.priority})
    return out


def _queue_labels(job: Job) -> list[str]:
    """Libellés de tous les éléments, annoncés dès le départ : la liste d'attente
    s'affiche entière, pas seulement l'élément en cours. Les libellés sont ceux
    des événements item_* (même règle de nommage)."""
    try:
        if job.operation == "archive_1fichier":
            return [onefichier.unit_label(i.rstrip("/")) for i in job.items]
        if job.operation == "restore_1fichier":
            return onefichier.restore_labels(job.items)
        return [_name(i) for i in job.items]
    except Exception:  # la liste d'attente est un confort : jamais bloquante
        return []


async def run_job(job: Job) -> None:
    job.status = "running"
    labels = await asyncio.to_thread(_queue_labels, job)  # lit l'index 1fichier : hors boucle
    job.emit({"type": "job_start", "total_items": len(job.items), "items": labels})

    interrompue = False
    try:
        if job.operation == "delete":
            await _run_delete(job)
        elif job.operation == "archive_1fichier":
            await onefichier.run_archive(job)
        elif job.operation == "restore_1fichier":
            await onefichier.run_restore(job)
        else:
            await _run_copy_or_move(job)
    except asyncio.CancelledError:
        # Arrêt du serveur en cours de tâche : elle reste dans le fichier d'attente et reprendra au démarrage.
        interrompue = True
        raise
    except Exception as exc:  # filet de sécurité : ne jamais finir en silence
        log_action("ECHEC", job.operation.upper(), f"job {job.id} : {exc}")
        job.emit({"type": "job_error", "error": f"Erreur inattendue : {exc}"})
    finally:
        _liberer_toutes_reservations(job)
        if not interrompue:
            job.status = "done"
            counts = {"ok": 0, "error": 0, "partial": 0, "skipped": 0}
            for e in job.events:
                t = e["type"]
                if t == "item_done":
                    counts["ok"] += 1
                elif t == "item_error":
                    counts["error"] += 1
                elif t == "item_partial":
                    counts["partial"] += 1
                elif t == "item_skipped":
                    counts["skipped"] += 1
            job.emit({"type": "job_done", "summary": counts})
            persist_pending()


async def _run_delete(job: Job) -> None:
    for item in job.items:
        name = _name(item)
        job.emit({"type": "item_start", "item": name})

        if not os.path.lexists(item):
            job.emit({"type": "item_skipped", "item": name, "reason": "missing",
                      "message": "Déjà absent."})
            continue

        if not config.ALLOW_DELETE:  # garde à l'exécution (version 45), en plus de la création de la tâche
            _fail(job, item, "suppression désactivée sur ce serveur (ALLOW_DELETE=false)")
            continue

        if not _suppression_autorisee(item):
            _fail(job, item, "hors des dossiers sources et des disques connectés : suppression refusée")
            continue

        if config.settings.dry_run:
            log_action("SIMULE", "DELETE", item)
            job.emit({"type": "item_log", "item": name, "line": "[dry-run] suppression simulée"})
            job.emit({"type": "item_done", "item": name})
            continue

        try:
            await asyncio.to_thread(_remove_path, item)
        except OSError as exc:
            _fail(job, item, f"suppression impossible ({exc})", exc)
            continue

        log_action("OK", "DELETE", item)
        job.emit({"type": "item_done", "item": name})


async def _run_copy_or_move(job: Job) -> None:
    dest_dir = job.destination
    if not config.settings.dry_run:  # en simulation, aucun dossier n'est créé (version 42)
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as exc:
            for item in job.items:
                _fail(job, item, f"destination inaccessible ({exc})", exc)
            return

    # Une seule copie à la fois PAR disque. L'emplacement est pris par élément, dans _copy_or_move_one,
    # APRÈS l'attente d'une plage horaire : une tâche hors plage ne bloque plus les tâches urgentes.
    target = disk_target(job)
    for item in job.items:
        if ARRETE:  # arrêt du serveur : la tâche reste dans le fichier d'attente
            raise asyncio.CancelledError()
        item_dir = job.dest_dirs.get(item, dest_dir)
        try:
            if item_dir != dest_dir and not config.settings.dry_run:
                os.makedirs(item_dir, exist_ok=True)
            await _copy_or_move_one(job, item, item_dir, target=target)
        except Exception as exc:  # un élément défaillant ne bloque pas les autres
            _fail(job, item, f"erreur inattendue ({exc})", exc)
        finally:
            _liberer_reservation(job, item)  # l'espace réservé pour cet élément est rendu


def bwlimit_args() -> list[str]:
    """Option rsync --bwlimit (en Kio/s) selon la vitesse de la plage active ; vide = illimité."""
    mbps = scheduler.rate_mbps("disk")
    return [f"--bwlimit={max(1, int(mbps * 1024))}"] if mbps > 0 else []


def disk_target(job: Job) -> Optional[str]:
    """Racine du disque externe concerné par la tâche : celui de la destination, sinon celui
    d'un élément source (disque -> serveur). None si aucun disque connu n'est concerné."""
    try:
        roots = [os.path.normpath(d["path"]) for d in disks.list_destination_disks()]
    except Exception:  # la détection est un confort : jamais bloquante
        return None

    def match(path: str) -> Optional[str]:
        best = None
        for root in roots:
            if path == root or path.startswith(root + os.sep):
                if best is None or len(root) > len(best):
                    best = root
        return best

    if job.destination:
        found = match(os.path.normpath(job.destination))
        if found:
            return found
    for item in job.items:
        found = match(os.path.normpath(item))
        if found:
            return found
    return None


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _digest_entree(path: str) -> str:
    """Empreinte d'une entrée. Un lien symbolique est comparé à sa CIBLE (rsync -l le recrée à
    l'identique) : un lien cassé ne fait plus échouer la vérification de tout un dossier."""
    if os.path.islink(path):
        return "lien:" + os.readlink(path)
    return _sha256_file(path)


def _tree_digests(path: str) -> dict[str, str]:
    """Chemin relatif -> empreinte, pour un fichier seul (clé vide) ou un dossier entier."""
    if os.path.islink(path) or os.path.isfile(path):
        return {"": _digest_entree(path)}
    out: dict[str, str] = {}
    for root, _dirs, files in _walk(path):
        for f in files:
            full = os.path.join(root, f)
            out[os.path.relpath(full, path)] = _digest_entree(full)
    return out


def _verify_copy(src: str, dst: str) -> tuple[bool, str]:
    """Vérifie le CONTENU de la copie : chaque fichier de la source doit exister dans `dst`
    avec la même empreinte. Les fichiers en plus à destination ne sont pas concernés.
    Avec VERIFY_SHA256=false (version 45), seules les tailles sont comparées : bien plus rapide
    sur un disque lent, mais une altération de même taille passerait inaperçue."""
    if not os.path.lexists(dst):
        return False, "copie absente"
    if os.path.isdir(src) != os.path.isdir(dst):
        return False, "type différent (fichier ou dossier)"
    if not config.VERIFY_SHA256:
        if _covered(src, dst):
            return True, "tailles identiques (SHA-256 désactivé)"
        return False, "tailles ou fichiers différents (SHA-256 désactivé)"
    wanted = _tree_digests(src)
    if not wanted:
        return True, "rien à vérifier"
    have = _tree_digests(dst)
    bad = [rel for rel, digest in wanted.items() if have.get(rel) != digest]
    if bad:
        return False, f"{len(bad)} fichier(s) différent(s), ex. {bad[0] or os.path.basename(src)}"
    return True, f"{len(wanted)} fichier(s)"


async def _verify_copy_safe(src: str, dst: str) -> tuple[bool, str]:
    try:
        return await asyncio.to_thread(_verify_copy, src, dst)
    except OSError as exc:
        return False, f"lecture impossible ({exc})"


# Processus rsync en cours, et drapeau d'arrêt du serveur (version 45).
_RSYNC_PROCS: set = set()
ARRETE = False


async def arreter_transferts(delai: float = 5.0) -> None:
    """Arrêt propre du serveur : chaque rsync en cours reçoit SIGTERM et range sa copie partielle dans le
    dossier dédié (PARTIAL_DIR), pour que la relance reprenne au lieu de recommencer. Aucun nouvel élément
    n'est commencé ensuite : les tâches restent dans le fichier d'attente."""
    global ARRETE
    ARRETE = True
    procs = [p for p in _RSYNC_PROCS if p.returncode is None]
    for p in procs:
        try:
            p.send_signal(signal.SIGCONT)  # un rsync suspendu hors plage ne traite le SIGTERM qu'une fois repris
            p.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass
    if procs:
        log_action("ARRET", "TRANSFERT", f"{len(procs)} transfert(s) arrêté(s) proprement, reprise à la relance")
        try:
            await asyncio.wait_for(asyncio.gather(*(p.wait() for p in procs), return_exceptions=True), delai)
        except asyncio.TimeoutError:
            pass


async def _run_rsync(job: Job, name: str, base_cmd: list[str], control: bool) -> int:
    """Lance rsync pour un élément. Suspendu hors plage, relancé si la vitesse change
    (voir scheduler.control_process) : --partial reprend là où il s'était arrêté."""
    while True:
        started_rate = scheduler.rate_mbps("disk")
        proc = await asyncio.create_subprocess_exec(
            *base_cmd, *bwlimit_args(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        _RSYNC_PROCS.add(proc)
        watcher = None
        if control:
            watcher = asyncio.ensure_future(scheduler.control_process(job, proc, "disk", started_rate))
        try:
            await _stream_rsync_output(job, name, proc)
            returncode = await proc.wait()
        finally:
            _RSYNC_PROCS.discard(proc)
            if watcher is not None and not watcher.done():
                watcher.cancel()
        restart = (watcher is not None and watcher.done() and not watcher.cancelled()
                   and watcher.result() == "restart")
        if not restart:
            return returncode
        job.emit({"type": "item_log", "item": name,
                  "line": "Vitesse changée : copie relancée avec la nouvelle limite (la partie déjà copiée est reprise)."})


async def _copy_or_move_one(job: Job, item: str, dest_dir: str, move: Optional[bool] = None,
                            target: Optional[str] = None) -> None:
    name = _name(item)
    dest_name = job.dest_names.get(item) or name
    renamed = dest_name != name
    dest_path = os.path.join(dest_dir, dest_name)
    is_move = (job.operation == "move") if move is None else move
    dry = config.settings.dry_run

    if not os.path.lexists(item):
        if job.resumed and os.path.lexists(dest_path):
            # Tâche reprise après un redémarrage : cet élément était déjà traité avant l'arrêt.
            job.emit({"type": "item_skipped", "item": name, "reason": "already_done",
                      "message": "Déjà traité avant le redémarrage du serveur."})
            return
        _fail(job, item, "source introuvable (déjà déplacée ou supprimée ?)")
        return

    if os.path.lexists(dest_path) and not job.merge:
        if _covered(item, dest_path):
            # Déjà copié (même nom, même taille). Pour un déplacement, la source ne part
            # qu'après vérification du CONTENU : une taille identique ne suffit pas.
            if not is_move:
                job.emit({"type": "item_skipped", "item": name, "reason": "already_copied",
                          "message": "Déjà présent à l'identique à destination."})
                log_action("IGNORE", "COPY", f"{item} -> {dest_dir}/ (déjà présent à l'identique)")
                return
            job.emit({"type": "item_start", "item": name})
            if dry:
                job.emit({"type": "item_log", "item": name, "line": "[dry-run] source supprimée (simulé)"})
                job.emit({"type": "item_done", "item": name})
                return
            job.emit({"type": "item_log", "item": name, "line": "Vérification des empreintes SHA-256…"})
            ok, detail = await _verify_copy_safe(item, dest_path)
            if not ok:
                _fail(job, item, f"présent à destination mais vérification en échec ({detail}) : source conservée")
                return
            job.emit({"type": "item_log", "item": name,
                      "line": f"Déjà présent, vérification réussie ({detail}) : retrait de la source."})
            await _remove_source_after_move(job, item, dest_path)
            return

        if not _copie_partielle(item, dest_path):
            # Présent mais différent : on n'écrase RIEN.
            job.emit({"type": "item_skipped", "item": name, "reason": "already_exists",
                      "message": "Existe déjà à destination avec un contenu différent : rien n'a été écrasé, source conservée."})
            log_action("IGNORE", job.operation.upper(),
                       f"{item} -> {dest_dir}/ (existe déjà avec un contenu différent)")
            return
        # Copie interrompue (début identique, fichier tronqué) : le transfert la termine.
        job.emit({"type": "item_log", "item": name, "line": "Copie interrompue à destination : reprise du transfert."})

    # Hors plage, rien ne tient l'emplacement du disque : une tâche plus prioritaire passe pendant l'attente.
    await scheduler.wait_for_window(job)
    if target is None:
        target = disk_target(job)
    async with scheduler.slot(job, "disk", target):
        await _transfert_verifie(job, item, dest_dir, dest_path, renamed, is_move)


async def _transfert_verifie(job: Job, item: str, dest_dir: str, dest_path: str,
                             renamed: bool, is_move: bool) -> None:
    """Transfert rsync puis vérification SHA-256, puis retrait de la source si déplacement.
    Appelé avec l'emplacement du disque déjà tenu."""
    name = _name(item)
    dry = config.settings.dry_run
    existed_before = os.path.lexists(dest_path)  # vrai seulement en fusion : la copie existait déjà
    job.emit({"type": "item_start", "item": name})

    base_cmd = ["rsync", *RSYNC_OPTS]
    if dry:
        base_cmd.append("--dry-run")
    if job.merge and existed_before:
        # Fusion (version 45) : une version différente est remplacée, mais l'ancienne est gardée.
        remplaces = _dossier_remplaces(job, dest_dir)
        if remplaces:
            base_cmd += ["--backup", f"--backup-dir={remplaces}"]
    if job.resumed:
        # Reprise après redémarrage (version 46) : rsync réutilise la partie déjà copiée (fichier partiel).
        # En copie locale il faut --no-whole-file : sans lui, rsync renvoie le fichier entier (mesuré :
        # 2 Go renvoyés pour 120 Mo réutilisables). Les copies normales gardent leurs options d'origine.
        base_cmd.append("--no-whole-file")
        job.emit({"type": "item_log", "item": name,
                  "line": "Reprise après redémarrage : la partie déjà copiée est réutilisée si elle existe."})
    if not renamed:
        base_cmd += [item, dest_dir.rstrip("/") + "/"]
    elif os.path.isdir(item):
        # Dossier renommé : son CONTENU va dans dest/<nouveau nom> (créé ou fusionné).
        base_cmd += [item.rstrip("/") + "/", dest_path + "/"]
    else:
        base_cmd += [item, dest_path]

    try:
        returncode = await _run_rsync(job, name, base_cmd, control=not dry)
    except Exception as exc:
        _fail(job, item, f"rsync : {exc}", exc)
        return

    if returncode != 0:
        if ARRETE:  # transfert arrêté par l'arrêt du serveur : pas une erreur, la relance reprendra
            raise asyncio.CancelledError()
        # Le fichier interrompu reste dans le dossier partiel : la relance reprend là où elle s'était arrêtée.
        _fail(job, item, f"rsync exit code {returncode} ; la partie copiée est conservée et reprendra à la relance")
        return

    if dry:
        log_action("SIMULE", job.operation.upper(), f"{item} -> {dest_dir}/")
        job.emit({"type": "item_done", "item": name})
        return

    # Vérification du CONTENU (empreintes SHA-256), avant d'annoncer ✓ et avant de retirer une source.
    job.emit({"type": "item_log", "item": name, "line": "Vérification des empreintes SHA-256…"})
    ok, detail = await _verify_copy_safe(item, dest_path)
    if not ok:
        note = ""
        if not existed_before:  # copie créée par ce passage : elle est retirée, la source reste
            try:
                await asyncio.to_thread(_remove_path, dest_path)
                note = " ; copie incomplète retirée"
            except OSError as exc:
                note = f" ; copie non retirée ({exc})"
        _fail(job, item, f"vérification en échec ({detail}){note} : source conservée")
        return
    job.emit({"type": "item_log", "item": name, "line": f"Vérification réussie ({detail})."})

    if job.merge and existed_before:
        remplaces = _dossier_remplaces(job, dest_dir)
        anciennes = _compte_remplaces(remplaces, name) if remplaces else 0
        if anciennes:
            job.emit({"type": "item_log", "item": name,
                      "line": f"{anciennes} ancienne(s) version(s) remplacée(s), conservée(s) dans {remplaces}."})

    if not is_move:
        log_action("OK", "COPY", f"{item} -> {dest_dir}/")
        job.emit({"type": "item_done", "item": name})
        return

    # Déplacement : la source ne part QU'APRÈS vérification du contenu.
    await _remove_source_after_move(job, item, dest_path)


REPLACED_DIR = ".archivage-remplaces"


def _dossier_remplaces(job: Job, dest_dir: str) -> Optional[str]:
    """Dossier où une fusion range les anciennes versions qu'elle remplace (version 45).
    Il est à la racine de la destination de la tâche, sous un nom caché que la bibliothèque ignore."""
    if not job.destination:
        return None
    rel = os.path.relpath(dest_dir, job.destination)
    if rel.startswith(os.pardir):
        rel = os.path.basename(dest_dir.rstrip("/"))
    return os.path.normpath(os.path.join(job.destination, REPLACED_DIR, job.id, rel))


def _compte_remplaces(remplaces: str, name: str) -> int:
    """Nombre de fichiers gardés dans le dossier des remplacements pour cet élément."""
    chemin = os.path.join(remplaces, name)
    if not os.path.lexists(chemin):
        return 0
    try:
        return len(_files_under(chemin))
    except OSError:
        return 0


async def _remove_source_after_move(job: Job, item: str, dest_path: str) -> None:
    name = _name(item)
    if not config.ALLOW_DELETE:
        log_action("PARTIEL", "MOVE", f"{item} -> {dest_path} (copié, suppression de la source désactivée)")
        job.emit({"type": "item_partial", "item": name,
                  "error": "Copié avec succès, mais la suppression de la source est désactivée (ALLOW_DELETE=false)."})
        return
    if not _suppression_autorisee(item):
        log_action("PARTIEL", "MOVE", f"{item} -> {dest_path} (copié, mais source hors zone gérée : non supprimée)")
        job.emit({"type": "item_partial", "item": name,
                  "error": "Copié avec succès, mais la source est hors des zones gérées : elle n'a pas été supprimée."})
        return
    try:
        await asyncio.to_thread(_remove_path, item)
    except OSError as exc:
        log_action("PARTIEL", "MOVE",
                   f"{item} -> {dest_path} (copié, mais source NON supprimée : {exc})")
        job.emit({
            "type": "item_partial",
            "item": name,
            "error": (
                f"Copié avec succès, mais impossible de supprimer la source : "
                f"{exc}.{_readonly_hint(exc)}"
            ),
        })
        return

    if os.path.lexists(item):  # filet de sécurité : ne jamais annoncer ✓ à tort
        log_action("PARTIEL", "MOVE", f"{item} -> {dest_path} (source encore présente après suppression)")
        job.emit({"type": "item_partial", "item": name,
                  "error": "Copié avec succès, mais la source est encore présente après suppression."})
        return

    log_action("OK", "MOVE", f"{item} -> {dest_path}")
    job.emit({"type": "item_done", "item": name})


async def _stream_rsync_output(job: Job, name: str, proc: asyncio.subprocess.Process) -> None:
    buf = b""
    assert proc.stdout is not None
    while True:
        chunk = await proc.stdout.read(256)
        if not chunk:
            break
        buf += chunk
        while b"\r" in buf or b"\n" in buf:
            sep = b"\r" if b"\r" in buf else b"\n"
            line, buf = buf.split(sep, 1)
            text = line.decode(errors="replace").strip()
            if not text:
                continue
            m = PROGRESS_RE.search(text)
            if m:
                job.emit({"type": "item_progress", "item": name, "percent": int(m.group(1))})
            else:
                job.emit({"type": "item_log", "item": name, "line": text})
