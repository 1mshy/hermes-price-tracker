"""Discord via an incoming webhook — no bot token or gateway connection needed."""
from __future__ import annotations

from ..fetch import fetcher
from ..settings import settings
from .base import Alert, Channel

_GREEN = 0x2ECC71


class DiscordChannel(Channel):
    name = "discord"

    @property
    def configured(self) -> bool:
        return bool(settings.discord_webhook_url)

    async def send(self, alert: Alert) -> None:
        fields = []
        if alert.price is not None:
            fields.append({"name": "Price", "value": f"{alert.currency} {alert.price:,.2f}", "inline": True})
        # Only worth showing when the reference price is actually higher.
        if alert.old_price and alert.price and alert.old_price > alert.price:
            drop = (1 - alert.price / alert.old_price) * 100
            fields.append({"name": "Was", "value": f"{alert.currency} {alert.old_price:,.2f} (−{drop:.1f}%)",
                           "inline": True})
        if alert.store:
            fields.append({"name": "Store", "value": alert.store, "inline": True})

        embed = {
            "title": alert.title[:250],
            "description": alert.body[:3800],
            "color": _GREEN,
            "fields": fields,
        }
        if alert.url:
            embed["url"] = alert.url
        if alert.image_url:
            embed["thumbnail"] = {"url": alert.image_url}

        client = await fetcher.client()
        response = await client.post(
            settings.discord_webhook_url,
            json={"username": "Hermes Shopping", "embeds": [embed]},
        )
        if response.status_code >= 300:
            raise RuntimeError(f"discord webhook {response.status_code}: {response.text[:200]}")
