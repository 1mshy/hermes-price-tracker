"""Notification channel contract."""
from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
from email.utils import parsedate_to_datetime


class DeliveryError(RuntimeError):
    """A provider response with enough metadata to retry temporary failures."""

    def __init__(self, channel: str, status: int, text: str, retry_after: str = ""):
        super().__init__(f"{channel} {status}: {text[:200]}")
        self.status = status
        self.retry_after = 0.0
        if retry_after:
            try:
                self.retry_after = max(0, float(retry_after))
            except ValueError:
                try:
                    until = parsedate_to_datetime(retry_after)
                    if until.tzinfo is None:
                        until = until.replace(tzinfo=dt.timezone.utc)
                    self.retry_after = max(0, (until - dt.datetime.now(dt.timezone.utc)).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    pass


def check_response(response, channel: str) -> None:
    if response.status_code >= 300:
        headers = getattr(response, "headers", {})
        raise DeliveryError(channel, response.status_code, response.text,
                            headers.get("Retry-After", ""))


@dataclass
class Alert:
    title: str
    body: str
    url: str | None = None
    image_url: str | None = None
    price: float | None = None
    old_price: float | None = None
    currency: str = "USD"
    store: str | None = None

    def as_text(self) -> str:
        lines = [self.title, "", self.body]
        if self.url:
            lines += ["", self.url]
        return "\n".join(lines)


class Channel:
    name: str = "base"

    @property
    def configured(self) -> bool:      # pragma: no cover - interface
        raise NotImplementedError

    async def send(self, alert: Alert) -> None:   # pragma: no cover - interface
        raise NotImplementedError
