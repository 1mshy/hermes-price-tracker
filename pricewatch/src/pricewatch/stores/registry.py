"""Adapter construction, URL→adapter resolution, and cross-store search fan-out."""
from __future__ import annotations

import asyncio
import logging

from .aliexpress import AliExpressAdapter
from .apis import BestBuyAdapter, EbayAdapter
from .base import StoreAdapter, StoreResult, host_of
from .bigbox import AmazonAdapter, SelectorAdapter, WalmartAdapter
from .catalog import CATALOG, PRINT3D, TECH, keys_for_tag
from .shopify import ShopifyAdapter, looks_like_shopify
from .structured import StructuredAdapter
from .woocommerce import WooAdapter, looks_like_woo

log = logging.getLogger(__name__)

_API_ADAPTERS: dict[str, type[StoreAdapter]] = {
    "amazon": AmazonAdapter,
    "bestbuy": BestBuyAdapter,
    "walmart": WalmartAdapter,
    "ebay": EbayAdapter,
    "aliexpress": AliExpressAdapter,
}


# Stores that render prices into the DOM without publishing schema.org data.
SELECTOR_RULES: dict[str, dict] = {
    "3djake": {
        "price_xpaths": (
            "//*[contains(@class,'product-price')]",
            "//*[contains(@class,'price') and not(contains(@class,'base'))]",
        ),
        "title_xpaths": ("//h1",),
    },
    "newegg": {
        "price_xpaths": (
            "//div[contains(@class,'price-current')]",
            "//li[contains(@class,'price-current')]",
        ),
        "title_xpaths": ("//h1[contains(@class,'product-title')]",),
        "stock_xpath": "//div[contains(@class,'product-inventory')]",
    },
}


def _build(entry: dict) -> StoreAdapter:
    key, kind, domains = entry["key"], entry["kind"], entry["domains"]
    if kind == "api":
        return _API_ADAPTERS[key]()
    if kind == "shopify":
        return ShopifyAdapter(name=key, domains=domains)
    if kind == "woo":
        return WooAdapter(name=key, domains=domains)
    if kind == "selector":
        rules = SELECTOR_RULES[key]
        return SelectorAdapter(name=key, domains=domains, **rules)
    if kind == "browser":
        return StructuredAdapter(name=key, domains=domains, force_browser=True)
    return StructuredAdapter(name=key, domains=domains)


ADAPTERS: dict[str, StoreAdapter] = {entry["key"]: _build(entry) for entry in CATALOG}


def store_key_for_url(url: str) -> str | None:
    host = host_of(url)
    for entry in CATALOG:
        for domain in entry["domains"]:
            if host == domain or host.endswith("." + domain):
                return entry["key"]
    return None


async def resolve(url: str) -> StoreAdapter:
    """Known store → its adapter. Unknown store → sniff the platform."""
    key = store_key_for_url(url)
    if key:
        return ADAPTERS[key]

    host = host_of(url)
    if "/products/" in url and await looks_like_shopify(url):
        log.info("auto-detected Shopify storefront: %s", host)
        return ShopifyAdapter(name=host, domains=(host,))
    if await looks_like_woo(url):
        log.info("auto-detected WooCommerce storefront: %s", host)
        return WooAdapter(name=host, domains=(host,))
    return StructuredAdapter(name=host, domains=(host,))


async def fetch_offer(url: str) -> StoreResult:
    adapter = await resolve(url)
    try:
        return await adapter.fetch_offer(url)
    except Exception as exc:                     # an adapter bug must not kill a sweep
        log.exception("adapter %s failed on %s", adapter.name, url)
        return StoreResult(store=adapter.name, url=url, error=f"{type(exc).__name__}: {exc}")


async def search_stores(query: str, store_keys: list[str] | None = None,
                        limit_per_store: int = 3, timeout: float = 45.0) -> list[StoreResult]:
    """Ask every candidate store for matches, in parallel, tolerating failures."""
    keys = store_keys or (keys_for_tag(TECH) + keys_for_tag(PRINT3D))
    ordered: list[str] = []
    for key in keys:
        if key in ADAPTERS and key not in ordered:
            ordered.append(key)

    async def one(key: str) -> list[StoreResult]:
        try:
            return await asyncio.wait_for(
                ADAPTERS[key].search(query, limit=limit_per_store), timeout=timeout
            )
        except Exception:
            return []

    batches = await asyncio.gather(*(one(k) for k in ordered))
    results = [r for batch in batches for r in batch if r.ok]
    results.sort(key=lambda r: r.price)
    return results


def searchable(key: str) -> bool:
    adapter = ADAPTERS.get(key)
    if adapter is None:
        return False
    return type(adapter).search is not StoreAdapter.search


def catalog_summary() -> list[dict]:
    return [
        {"key": e["key"], "label": e["label"], "kind": e["kind"], "tags": e["tags"],
         "domain": e["domains"][0], "searchable": searchable(e["key"]),
         **({"note": e["note"]} if e.get("note") else {})}
        for e in CATALOG
    ]
