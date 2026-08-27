"""Store adapter contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from urllib.parse import urlsplit


@dataclass
class StoreResult:
    store: str
    url: str
    title: str | None = None
    price: Decimal | None = None
    currency: str = "USD"
    in_stock: bool | None = None
    sku: str | None = None
    method: str = "unknown"
    error: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.price is not None and self.error is None

    def as_dict(self) -> dict:
        return {
            "store": self.store,
            "url": self.url,
            "title": self.title,
            "price": float(self.price) if self.price is not None else None,
            "currency": self.currency,
            "in_stock": self.in_stock,
            "sku": self.sku,
            "method": self.method,
            "error": self.error,
        }


def host_of(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


class StoreAdapter:
    """Subclasses implement `fetch_offer` and, where possible, `search`."""

    name: str = "generic"
    domains: tuple[str, ...] = ()
    #: set when the store needs an API key we may not have
    requires_key: str | None = None
    #: False for search-only marketplaces: compare works, but fetch_offer
    #: cannot re-read a product page, so sweeps/tracking must skip them.
    trackable: bool = True

    def matches(self, url: str) -> bool:
        host = host_of(url)
        return any(host == d or host.endswith("." + d) for d in self.domains)

    async def fetch_offer(self, url: str) -> StoreResult:      # pragma: no cover - interface
        raise NotImplementedError

    async def search(self, query: str, limit: int = 5) -> list[StoreResult]:
        return []
