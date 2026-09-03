"""One retailer, two platforms.

Creality's US store is a Next.js app that only publishes schema.org JSON-LD,
while its regional markets (ca.store.creality.com) are plain Shopify. A single
reader gets one of them wrong: the structured path finds no price on the
Canadian PDP and cannot search at all. So each URL goes to the reader its host
needs, and search runs on the Shopify market whenever that is the shopper's
storefront — which is what puts Ender prices in CAD for a Canadian.
"""
from __future__ import annotations

from .base import StoreAdapter, StoreResult
from .shopify import ShopifyAdapter
from .structured import StructuredAdapter


class SplitPlatformAdapter(StoreAdapter):
    def __init__(self, name: str, domains: tuple[str, ...], shopify_domains: tuple[str, ...],
                 force_browser: bool = False, currency: str | None = None):
        self.name = name
        self.domains = domains
        # The Shopify side asks /cart.js; only the structured reader needs to
        # be told when the primary hostname lies about its money.
        self.structured = StructuredAdapter(name=name, domains=domains,
                                            force_browser=force_browser, currency=currency)
        self.shopify = ShopifyAdapter(name=name, domains=shopify_domains)

    def reader(self, url: str) -> StoreAdapter:
        return self.shopify if self.shopify.matches(url) else self.structured

    def can_search(self) -> bool:
        return self.storefront() in self.shopify.domains

    async def fetch_offer(self, url: str) -> StoreResult:
        return await self.reader(url).fetch_offer(url)

    async def search(self, query: str, limit: int = 5) -> list[StoreResult]:
        # The Shopify market is searchable; the primary storefront is not.
        return await self.shopify.search(query, limit=limit) if self.can_search() else []
