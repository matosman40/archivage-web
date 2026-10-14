"""1fichier, premier hébergeur branché sur le socle (version 33).

Seul le test de connexion passe par cette classe. L'envoi, le téléchargement, la vérification de
lien et la suppression restent dans onefichier.py : ils sont liés à l'index, aux jobs et aux
emplacements de file, et les déplacer maintenant prendrait plus de risque que de gain.
"""
from .. import onefichier as impl
from .base import Host


class OneFichierHost(Host):
    key = "1fichier"
    label = "1fichier"
    kind = "onefichier"
    domains = impl.ALLOWED_DOMAINS
    max_file_bytes = impl.MAX_FILE_BYTES

    def is_configured(self) -> bool:
        return impl.is_configured()

    async def test_connection(self) -> dict:
        return await impl.test_connection()
