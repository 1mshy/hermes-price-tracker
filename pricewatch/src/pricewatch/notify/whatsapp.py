"""WhatsApp through Twilio, or CallMeBot for a zero-account personal setup."""
from __future__ import annotations

from urllib.parse import quote

from ..fetch import fetcher
from ..settings import settings
from .base import Alert, Channel


class TwilioWhatsAppChannel(Channel):
    name = "whatsapp"

    @property
    def configured(self) -> bool:
        return bool(
            settings.twilio_account_sid and settings.twilio_auth_token
            and settings.twilio_whatsapp_from and settings.twilio_whatsapp_to
        )

    async def send(self, alert: Alert) -> None:
        client = await fetcher.client()
        endpoint = (f"https://api.twilio.com/2010-04-01/Accounts/"
                    f"{settings.twilio_account_sid}/Messages.json")
        recipients = [r.strip() for r in settings.twilio_whatsapp_to.split(",") if r.strip()]
        for recipient in recipients:
            response = await client.post(
                endpoint,
                data={"From": settings.twilio_whatsapp_from, "To": recipient,
                      "Body": alert.as_text()[:1500]},
                auth=(settings.twilio_account_sid, settings.twilio_auth_token),
                timeout=30,
            )
            if response.status_code >= 300:
                raise RuntimeError(f"twilio {response.status_code}: {response.text[:200]}")


class CallMeBotWhatsAppChannel(Channel):
    """Free personal WhatsApp relay. One recipient, best-effort delivery."""

    name = "whatsapp_callmebot"

    @property
    def configured(self) -> bool:
        return bool(settings.callmebot_phone and settings.callmebot_apikey)

    async def send(self, alert: Alert) -> None:
        endpoint = (
            "https://api.callmebot.com/whatsapp.php"
            f"?phone={quote(settings.callmebot_phone)}"
            f"&text={quote(alert.as_text()[:900])}"
            f"&apikey={quote(settings.callmebot_apikey)}"
        )
        page = await fetcher.get(endpoint)
        if page.status >= 300:
            raise RuntimeError(f"callmebot {page.status}: {page.text[:200]}")
