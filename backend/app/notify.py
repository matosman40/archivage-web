"""Alertes par notification : ntfy (URL du sujet) ou webhook Discord.

Facultatif : sans URL configurée (Réglages › 1fichier, ou variable NOTIFY_URL),
rien n'est envoyé. Un échec d'envoi n'interrompt jamais l'opération qui alerte :
il est seulement consigné dans le journal.
"""
import httpx

from . import config
from .logs import log_action

DISCORD_HOSTS = ("discord.com/api/webhooks", "discordapp.com/api/webhooks")


async def send(title: str, message: str) -> None:
    url = (config.settings.notify_url or "").strip()
    if not url:
        return
    text = f"{title}\n{message}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if any(host in url for host in DISCORD_HOSTS):
                resp = await client.post(url, json={"content": f"**{title}**\n{message}"[:1900]})
            else:
                # ntfy : le corps est le message ; le titre est en première ligne
                resp = await client.post(url, content=text.encode("utf-8")[:4000])
        if resp.status_code >= 400:
            log_action("ALERTE", "NOTIFY", f"alerte non envoyée (HTTP {resp.status_code})")
    except httpx.HTTPError as exc:
        log_action("ALERTE", "NOTIFY", f"alerte non envoyée ({type(exc).__name__})")
