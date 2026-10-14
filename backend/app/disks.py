"""Détection des disques de destination montés.

Plutôt que d'appeler `findmnt` (ce qui nécessiterait un accès étendu à
l'espace de noms de montage de l'hôte), on lit /proc/mounts, toujours
accessible dans un conteneur, et on applique un filtrage large sous les
racines de scan connues.

On accepte 1 OU 2 niveaux sous chaque racine pour couvrir les différents
gestionnaires de montage rencontrés en pratique :
  - /mnt/<disque>                      (1 niveau)
  - /media/<disque>                    (1 niveau)
  - /media/devmon/<disque>             (2 niveaux — devmon)
  - /media/<utilisateur>/<disque>      (2 niveaux — udisks2/gvfs)
  - /run/media/<utilisateur>/<disque>  (2 niveaux)

Pour que les disques branchés APRÈS le démarrage du conteneur apparaissent,
les points de montage hôte doivent être bind-montés avec une propagation
partagée (voir docker-compose.yml : `:rslave`) et l'hôte doit lui-même avoir
ces points en propagation partagée (`mount --make-rshared /media`).
"""
import shutil

from . import config, security

MAX_DEPTH_UNDER_ROOT = 2


def _load_mounts() -> list[str]:
    mounts = []
    try:
        with open("/proc/mounts", "r") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2:
                    mounts.append(parts[1])
    except OSError:
        pass
    return mounts


def list_destination_disks() -> list[dict]:
    mounts = _load_mounts()
    candidates: list[str] = []

    for m in mounts:
        for root in config.settings.dest_scan_roots:
            root = root.rstrip("/")
            if not root or m == root:
                continue
            if m.startswith(root + "/"):
                rel = m[len(root) + 1:].strip("/")
                depth = len(rel.split("/")) if rel else 0
                if 1 <= depth <= MAX_DEPTH_UNDER_ROOT:
                    candidates.append(m)
                break

    disks = []
    seen = set()
    for m in candidates:
        if m in seen or security.is_under_source_dirs(m):
            continue
        seen.add(m)
        try:
            free = shutil.disk_usage(m).free
        except OSError:
            free = None
        disks.append({"path": m, "free_bytes": free})

    disks.sort(key=lambda d: d["path"])
    return disks
