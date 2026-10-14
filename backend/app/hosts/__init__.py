"""Registre des hébergeurs de sauvegarde distante (version 33).

Ajouter un hébergeur : écrire une classe dans ce dossier (héritée de base.Host), la déclarer dans
_REGISTRY ci-dessous, puis ajouter son type de transfert dans config.TRANSFER_KINDS si besoin.
Les vérifications de cohérence se font à l'import : une erreur de déclaration empêche le démarrage.
"""
from .. import config
from .base import Host, HostError
from .onefichier import OneFichierHost

_REGISTRY: dict[str, Host] = {host.key: host for host in (OneFichierHost(),)}


def _validate() -> None:
    seen_labels = set()
    for host in _REGISTRY.values():
        if not host.key or not host.label:
            raise RuntimeError(f"hébergeur sans clé ou sans libellé : {type(host).__name__}")
        if host.kind not in config.TRANSFER_KINDS:
            raise RuntimeError(f"{host.key} : type de transfert inconnu « {host.kind} »")
        if not host.domains:
            raise RuntimeError(f"{host.key} : aucun domaine autorisé")
        if host.label in seen_labels:
            raise RuntimeError(f"libellé en double : {host.label}")
        seen_labels.add(host.label)


_validate()


def all_hosts() -> list[Host]:
    return list(_REGISTRY.values())


def get(key: str) -> Host:
    try:
        return _REGISTRY[key]
    except KeyError:
        raise HostError(f"hébergeur inconnu : {key}")


__all__ = ["Host", "HostError", "all_hosts", "get"]
