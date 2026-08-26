"""Shopify storefronts.

Shopify exposes an unauthenticated JSON view of every product:
  /products/<handle>.js            → single product, price in integer cents
  /search/suggest.json?q=…         → typeahead search with prices
This is a documented storefront endpoint, so it is both far more reliable and far
lighter on the store than scraping rendered HTML. A large share of the 3D-printing
market (Bambu Lab, Elegoo, Creality, Anycubic, E3D, Micro Swiss, Printed Solid,
Polymaker, Slice, Proto-pasta, West3D, Fabreeko…) runs on Shopify.
"""
from __future__ import annotations

import re
from urllib.parse import quote, urlsplit, urlunsplit

from ..fetch import fetcher
from ..money import cents_to_decimal, parse_price
from .base import StoreAdapter, StoreResult, host_of

_HANDLE_RE = re.compile(r"/products/([^/?#]+)")


def _root(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme or "https", parts.netloc, "", "", ""))


def _pick_variant(product: dict, url: str) -> dict | None:
    variants = product.get("variants") or []
    if not variants:
        return None
    wanted = None
    query = urlsplit(url).query
    match = re.search(r"variant=(\d+)", query)
    if match:
        wanted = match.group(1)
        for variant in variants:
            if str(variant.get("id")) == wanted:
                return variant
    available = [v for v in variants if v.get("available")]
    pool = available or variants
    # Cheapest available variant is the honest "starting at" price.
    return min(pool, key=lambda v: v.get("price") or 10**12)


def _suggest_price(raw):
    """suggest.json returns cents on some themes and a formatted string on others."""
    if isinstance(raw, int) or (isinstance(raw, str) and raw.isdigit()):
        return cents_to_decimal(raw)
    return parse_price(raw)


class ShopifyAdapter(StoreAdapter):
    name = "shopify"

    def __init__(self, name: str | None = None, domains: tuple[str, ...] = ()):
        if name:
            self.name = name
        self.domains = domains

    def matches(self, url: str) -> bool:
        if self.domains:
            return super().matches(url)
        return False

    async def fetch_offer(self, url: str) -> StoreResult:
        store = self.name if self.domains else host_of(url)
        match = _HANDLE_RE.search(urlsplit(url).path)
        if not match:
            return StoreResult(store=store, url=url, error="not a Shopify product URL")
        handle = match.group(1).removesuffix(".js").removesuffix(".json")
        endpoint = f"{_root(url)}/products/{handle}.js"
        try:
            data = await fetcher.get_json(endpoint)
        except Exception as exc:
            return StoreResult(store=store, url=url, error=f"shopify .js failed: {exc}")

        variant = _pick_variant(data, url)
        price = cents_to_decimal((variant or {}).get("price") or data.get("price"))
        return StoreResult(
            store=store,
            url=url,
            title=data.get("title"),
            price=price,
            currency="USD",
            in_stock=bool((variant or {}).get("available", data.get("available", False))),
            sku=(variant or {}).get("sku") or str(data.get("id") or "") or None,
            method="shopify-json",
            extra={
                "variant": (variant or {}).get("title"),
                "compare_at": cents_to_decimal((variant or {}).get("compare_at_price")),
            },
        )

    async def search(self, query: str, limit: int = 5) -> list[StoreResult]:
        if not self.domains:
            return []
        root = f"https://{self.domains[0]}"
        endpoint = (
            f"{root}/search/suggest.json?q={quote(query)}"
            f"&resources[type]=product&resources[limit]={min(limit, 10)}"
        )
        try:
            data = await fetcher.get_json(endpoint)
        except Exception:
            return []
        products = (((data.get("resources") or {}).get("results") or {}).get("products")) or []
        results: list[StoreResult] = []
        for item in products[:limit]:
            url = item.get("url") or ""
            if url.startswith("/"):
                url = root + url
            results.append(
                StoreResult(
                    store=self.name,
                    url=url,
                    title=item.get("title"),
                    price=_suggest_price(item.get("price")),
                    currency="USD",
                    in_stock=item.get("available"),
                    method="shopify-suggest",
                )
            )
        return results


async def looks_like_shopify(url: str) -> bool:
    """Cheap probe used when a domain is not in the catalog."""
    try:
        page = await fetcher.get(f"{_root(url)}/products.json?limit=1")
        return page.status == 200 and '"products"' in page.text[:2000]
    except Exception:
        return False
