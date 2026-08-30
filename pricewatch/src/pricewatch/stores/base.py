"""Store adapter contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from urllib.parse import urlsplit

from .. import preferences
from ..money import convert, currency_for_host, region_for_host


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
        payload = {
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
        # Adapters use `extra` for the caveats that decide whether a figure is
        # quotable at all — a search-result price that may be a variant, a
        # renewed unit, a paid placement. Dropping it here left every one of
        # those unsaid.
        details = {k: v for k, v in self.extra.items() if v is not None}
        if details:
            payload["extra"] = details
        approximate = self.approx_in_preferred()
        if approximate is not None:
            payload["approx_in_preferred"] = approximate
        return payload

    def approx_in_preferred(self) -> dict | None:
        """Roughly what this costs in the user's currency, when it is foreign.

        Deliberately separate from `price`/`currency`, which stay the only
        figures safe to quote. This exists so a CAD shopper can tell at a
        glance whether a USD listing is really the cheaper one — not so the
        engine can invent a Canadian price for an American store.
        """
        preferred = preferences.preferred_currency()
        if not preferred or self.price is None:
            return None
        if (self.currency or "").upper() == preferred:
            return None
        amount = convert(self.price, self.currency, preferred)
        if amount is None:
            return None
        return {
            "currency": preferred,
            "amount": float(amount),
            "basis": ("indicative rate, not a quote — report the price above in "
                      f"{self.currency} and present this only as an approximation"),
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

    def storefront(self) -> str:
        """The domain to search on: the user's regional storefront when this
        retailer runs one, else the primary.

        Search endpoints are per-storefront, and a store's Canadian site quotes
        CAD where its American one quotes USD — so for a shopper with a
        currency preference this is what keeps results in the right money
        instead of needing a conversion afterwards.
        """
        if not self.domains:
            return ""
        region = preferences.preferred_region()
        if region:
            for domain in self.domains:
                if region_for_host(domain) == region:
                    return domain
        # No storefront for that exact country — a shared-currency one (a euro
        # zone shopper on eu.store.…) is still better than the US default.
        currency = preferences.preferred_currency()
        if currency:
            for domain in self.domains:
                if currency_for_host(domain, "") == currency:
                    return domain
        return self.domains[0]

    async def fetch_offer(self, url: str) -> StoreResult:      # pragma: no cover - interface
        raise NotImplementedError

    async def search(self, query: str, limit: int = 5) -> list[StoreResult]:
        return []
