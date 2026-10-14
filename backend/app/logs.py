"""Journal des opérations : même format que log_action() du script bash
(horodatage TAB statut TAB opération TAB détail), un fichier persistant."""
import collections
import datetime
import os

from . import adult_media, config


def log_action(status: str, operation: str, detail: str) -> None:
    directory = os.path.dirname(config.LOG_FILE)
    if directory:
        os.makedirs(directory, exist_ok=True)

    # Une ligne qui cite un film adulte n'est pas écrite en clair : le journal reste lisible
    # sans session, et le nom d'un film adulte n'y figure jamais (version 42).
    if adult_media.mentions_adult(detail):
        detail = "[élément adulte]"
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(config.LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"{ts}\t{status}\t{operation}\t{detail}\n")
    except OSError:
        pass


def read_log(limit: int = 200) -> list[dict]:
    if not os.path.exists(config.LOG_FILE):
        return []
    limit = max(1, limit)  # limit=0 renvoyait tout le journal (tranche [-0:])

    # Seules les dernières lignes restent en mémoire, même si le journal est très long.
    with open(config.LOG_FILE, "r", encoding="utf-8") as fh:
        lines = list(collections.deque(fh, maxlen=limit))

    entries = []
    for line in lines:
        parts = line.rstrip("\n").split("\t")
        if len(parts) == 4:
            entries.append({
                "timestamp": parts[0],
                "status": parts[1],
                "operation": parts[2],
                "detail": parts[3],
            })
    entries.reverse()
    return entries
