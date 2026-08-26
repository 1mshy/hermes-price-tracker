"""Signal via a local signal-cli-rest-api container (bbernhard/signal-cli-rest-api).

Register the sending number once:
  docker compose --profile signal up -d signal-cli
  # then follow the container's register/verify endpoints
"""
from __future__ import annotations

from ..fetch import fetcher
from ..settings import settings
from .base import Alert, Channel


class SignalChannel(Channel):
    name = "signal"

    @property
    def configured(self) -> bool:
        return bool(settings.signal_api_url and settings.signal_from and settings.signal_to)

    async def send(self, alert: Alert) -> None:
        recipients = [r.strip() for r in settings.signal_to.split(",") if r.strip()]
        client = await fetcher.client()
        response = await client.post(
            f"{settings.signal_api_url.rstrip('/')}/v2/send",
            json={
                "message": alert.as_text(),
                "number": settings.signal_from,
                "recipients": recipients,
            },
            timeout=30,
        )
        if response.status_code >= 300:
            raise RuntimeError(f"signal-cli {response.status_code}: {response.text[:200]}")
