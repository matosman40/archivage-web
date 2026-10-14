"""Contrôle des adresses renvoyées par un hébergeur (version 33).

Une URL renvoyée par une API ne doit recevoir le jeton de l'utilisateur (ou être téléchargée) que si
son hôte appartient à la liste blanche de l'hébergeur. Ce module n'importe aucun autre module de
l'application : il peut être utilisé depuis n'importe où sans créer de cycle d'imports.
"""
import httpx


class UrlRejected(ValueError):
    """Adresse refusée. `host` garde l'hôte trouvé (vide si l'adresse est illisible)."""

    def __init__(self, host: str, reason: str):
        super().__init__(reason)
        self.host = host


def host_of(url: str) -> tuple[str, str]:
    """(schéma, hôte) en minuscules. Une adresse illisible donne ('', '')."""
    try:
        parsed = httpx.URL(url)
        return (parsed.scheme or "").lower(), (parsed.host or "").lower()
    except Exception:
        return "", ""


def domain_allowed(host: str, domains) -> bool:
    """Vrai pour le domaine lui-même et ses sous-domaines, jamais pour un domaine qui se termine
    seulement par la même suite de lettres (« evil1fichier.com » n'est pas « 1fichier.com »)."""
    return any(host == d or host.endswith("." + d) for d in domains)


def check_host(url: str, domains, *, https_only: bool = False) -> None:
    """Lève UrlRejected si l'adresse n'est pas autorisée pour ces domaines."""
    scheme, host = host_of(url)
    if https_only and scheme != "https":
        raise UrlRejected(host, f"schéma non sécurisé ({scheme or 'vide'})")
    if not host or not domain_allowed(host, domains):
        raise UrlRejected(host, host or "vide")
