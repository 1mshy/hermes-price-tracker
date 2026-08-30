"""WooCommerce storefronts via the public Store API (/wp-json/wc/store/v1)."""
from __future__ import annotations

from decimal import Decimal
from urllib.parse import quote, urlsplit, urlunsplit

from ..fetch import fetcher
from .base import StoreAdapter, StoreResult, host_of


def _root(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme or "https", parts.netloc, "", "", ""))


def _price(prices: dict | None) -> Decimal | None:
    """Store API returns minor units plus the exponent to apply."""
    if not prices:
        return None
    raw = prices.get("sale_price") or prices.get("price")
    if raw in (None, ""):
        return None
    try:
        minor = int(prices.get("currency_minor_unit", 2))
        return Decimal(str(raw)) / (Decimal(10) ** minor)
    except Exception:
        return None


def _slug(url: str) -> str | None:
    parts = [p for p in urlsplit(url).path.split("/") if p]
    return parts[-1] if parts else None


class WooAdapter(StoreAdapter):
    name = "woocommerce"

    def __init__(self, name: str | None = None, domains: tuple[str, ...] = ()):
        if name:
            self.name = name
        self.domains = domains

    def matches(self, url: str) -> bool:
        return super().matches(url) if self.domains else False

    async def fetch_offer(self, url: str) -> StoreResult:
        store = self.name if self.domains else host_of(url)
        slug = _slug(url)
        if not slug:
            return StoreResult(store=store, url=url, error="no product slug in URL")
        endpoint = f"{_root(url)}/wp-json/wc/store/v1/products?slug={quote(slug)}"
        try:
            data = await fetcher.get_json(endpoint)
        except Exception as exc:
            return StoreResult(store=store, url=url, error=f"woo store api failed: {exc}")
        if not isinstance(data, list) or not data:
            return StoreResult(store=store, url=url, error="product not found in Store API")
        item = data[0]
        return StoreResult(
            store=store,
            url=url,
            title=item.get("name"),
            price=_price(item.get("prices")),
            currency=(item.get("prices") or {}).get("currency_code", "USD"),
            in_stock=bool(item.get("is_in_stock", True)),
            sku=item.get("sku") or None,
            method="woo-store-api",
        )

    async def search(self, query: str, limit: int = 5) -> list[StoreResult]:
        if not self.domains:
            return []
        root = f"https://{self.storefront()}"
        endpoint = f"{root}/wp-json/wc/store/v1/products?search={quote(query)}&per_page={limit}"
        try:
            data = await fetcher.get_json(endpoint)
        except Exception:
            return []
        results = []
        for item in (data if isinstance(data, list) else [])[:limit]:
            results.append(
                StoreResult(
                    store=self.name,
                    url=item.get("permalink") or root,
                    title=item.get("name"),
                    price=_price(item.get("prices")),
                    currency=(item.get("prices") or {}).get("currency_code", "USD"),
                    in_stock=bool(item.get("is_in_stock", True)),
                    sku=item.get("sku") or None,
                    method="woo-store-api",
                )
            )
        return results


async def looks_like_woo(url: str) -> bool:
    try:
        page = await fetcher.get(f"{_root(url)}/wp-json/wc/store/v1/products?per_page=1")
        return page.status == 200 and page.text.strip().startswith("[")
    except Exception:
        return False
