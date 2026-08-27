"""ntfy.sh push channel — zero-account native push to phone or desktop.

The cheapest possible path from "no channels configured" to real push:
pick an unguessable topic name, subscribe to it in the ntfy app (or
`ntfy subscribe <topic>`), set NTFY_TOPIC in .env, restart. Anyone who knows
the topic name can read it, so treat the topic like a password.

Publishing uses the JSON endpoint (POST to the server root) rather than
per-message headers, so emoji-bearing titles survive without header-encoding
games. Self-hosted servers work via NTFY_SERVER.
"""
from __future__ import annotations

from ..fetch import fetcher
from ..settings import settings
from .base import Alert, Channel


class NtfyChannel(Channel):
    name = "ntfy"

    @property
    def configured(self) -> bool:
        return bool(settings.ntfy_topic)

    async def send(self, alert: Alert) -> None:
        server = (settings.ntfy_server or "https://ntfy.sh").rstrip("/")
        payload: dict = {
            "topic": settings.ntfy_topic,
            "title": alert.title[:200],
            "message": alert.as_text()[:3800],
            "priority": 4,               # high: it is literally money on the table
            "tags": ["moneybag"],
        }
        if alert.url:
            payload["click"] = alert.url
        client = await fetcher.client()
        response = await client.post(server, json=payload)
        if response.status_code >= 300:
            raise RuntimeError(f"ntfy {response.status_code}: {response.text[:200]}")
