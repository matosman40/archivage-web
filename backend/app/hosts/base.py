"""Socle commun des hébergeurs de sauvegarde distante (version 33).

Un hébergeur (1fichier aujourd'hui ; gofile, pixeldrain... demain) se décrit par :
- une clé stable (« 1fichier »), gravée dans l'index et l'API ;
- un type de transfert (config.TRANSFER_KINDS) : vitesse dans les plages et emplacement dans la file ;
- ses domaines autorisés : seule une adresse de ces domaines peut recevoir un jeton ou être téléchargée ;
- ses actions. Une action n'existe que si la classe la REDÉFINIT : `actions()` ne liste que celles-là,
  pour que l'interface n'annonce jamais une fonction qui n'existe pas.

Ce socle ne change rien au fonctionnement actuel : 1fichier garde ses appels directs dans
onefichier.py. Les actions suivantes sont prêtes à être branchées hébergeur par hébergeur.
"""
from typing import Optional

from .. import config, urlguard

# Actions qu'un hébergeur peut implémenter. Ordre d'affichage.
ACTIONS = ("test_connection", "upload", "download", "check", "delete")


class HostError(Exception):
    pass


class Host:
    key: str = ""                    # identifiant stable : « 1fichier »
    label: str = ""                  # nom affiché : « 1fichier »
    kind: str = ""                   # clé de config.TRANSFER_KINDS
    domains: tuple = ()              # domaines autorisés (sous-domaines compris)
    max_file_bytes: Optional[int] = None  # plafond d'un fichier chez l'hébergeur, None si inconnu

    # --- Etat -----------------------------------------------------------

    def is_configured(self) -> bool:
        """Vrai si un identifiant ou une clé est renseigné et que l'hébergeur est activé."""
        return False

    def check_url(self, url: str) -> None:
        """À appeler sur TOUTE URL renvoyée par l'hébergeur avant d'y envoyer un jeton ou de la lire.
        Exige https et un domaine autorisé."""
        try:
            urlguard.check_host(url, self.domains, https_only=True)
        except urlguard.UrlRejected as exc:
            raise HostError(f"{self.label} : adresse refusée ({exc})")

    def actions(self) -> list[str]:
        """Actions réellement implémentées par cette classe, dans l'ordre de ACTIONS."""
        return [name for name in ACTIONS if getattr(type(self), name) is not getattr(Host, name)]

    def describe(self) -> dict:
        """Description publique (sans identifiant ni clé) pour l'API et l'interface."""
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "kind_label": config.TRANSFER_KINDS.get(self.kind, self.kind),
            "domains": list(self.domains),
            "max_file_bytes": self.max_file_bytes,
            "configured": self.is_configured(),
            "actions": self.actions(),
        }

    # --- Actions (à implémenter par hébergeur) ---------------------------

    async def test_connection(self) -> dict:
        raise HostError(f"{self.label} : test de connexion non disponible")

    async def upload(self, *args, **kwargs):
        raise HostError(f"{self.label} : envoi non implémenté")

    async def download(self, *args, **kwargs):
        raise HostError(f"{self.label} : téléchargement non implémenté")

    async def check(self, *args, **kwargs):
        raise HostError(f"{self.label} : vérification de lien non implémentée")

    async def delete(self, *args, **kwargs):
        raise HostError(f"{self.label} : suppression distante non implémentée")
