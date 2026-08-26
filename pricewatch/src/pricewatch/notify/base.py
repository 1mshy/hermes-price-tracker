"""Notification channel contract."""
from __future__ import annotations

from dataclasses import dataclass


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
