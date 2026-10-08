"""Opt-in managed product API fallback after direct retailer access fails.

Uses SerpApi's product endpoints, not search snippets. No cache, no retries
that multiply credit usage, and no cross-market substitution. Disabled unless
PW_RETAIL_SERPAPI_ENABLED and SERPAPI_KEY are both configured.
"""
from __future__ import annotations

import datetime as dt
import re
from urllib.parse import urlsplit

from ..fetch import fetcher
from ..money import currency_for_host, currency_from_text, parse_price
from ..settings import settings
from .base import StoreResult


def _request(url: str) -> tuple[str, str, dict] | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    if host in ("amazon.com", "amazon.ca", "amazon.co.uk", "amazon.de"):
        match = re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:/|$)", parts.path)
        if match:
            return "amazon", match[1], {"engine": "amazon_product", "asin": match[1],
                                         "amazon_domain": host}
    # This provider's Walmart product API is the US marketplace only.
    if host == "walmart.com":
        match = re.search(r"/ip/(?:[^/]+/)?([A-Za-z0-9]+)$", parts.path.rstrip("/"))
        if match:
            return "walmart", match[1], {"engine": "walmart_product", "product_id": match[1]}
    return None


def _parse(data: dict, url: str, store: str, item_id: str) -> StoreResult:
    method = "serpapi:product"
    def failed(reason):
        return StoreResult(store=store, url=url, method=method, error=reason)
    if not isinstance(data, dict) or data.get("error"):
        return failed("product API returned no product data")
    metadata = data.get("search_metadata") or {}
    try:
        processed = dt.datetime.fromisoformat(metadata["processed_at"].replace(" UTC", "+00:00").replace("Z", "+00:00"))
        age = (dt.datetime.now(dt.timezone.utc) - processed).total_seconds()
        if not -60 <= age <= 600:
            return failed("product API data is too old to report as a live price")
    except (KeyError, TypeError, ValueError):
        return failed("product API did not supply a valid observation time")
    host = urlsplit(url).hostname
    native = currency_for_host(host, "USD")
    stock = None
    extra = {"provider": "SerpApi", "observed_at": processed.isoformat(),
             "note": "Live provider product-page observation; shipping, tax and account-specific offers may differ."}
    if store == "amazon":
        product = data.get("product_results") or {}
        actual = (data.get("product_details") or {}).get("asin")
        if actual != item_id:
            return failed("product API did not confirm the requested ASIN")
        maximum = parse_price(product.get("extracted_max_price"))
        price = parse_price(product.get("extracted_price"))
        if maximum is not None and maximum != price:
            return failed("product API returned a variant price range; select a specific variant")
        currency = currency_from_text(product.get("price") or "", host, native)
        extra["coupon"] = product.get("save_with_coupon")
        extra["price_basis"] = "sticker price; coupons are not deducted by this provider adapter"
    else:
        product = data.get("product_result") or {}
        if item_id not in (str(product.get("us_item_id", "")), str(product.get("product_id", ""))):
            return failed("product API returned a different Walmart item")
        price_map = product.get("price_map") or {}
        price = parse_price(price_map.get("price"))
        currency = price_map.get("currency")
        if currency != "USD":
            return failed("US Walmart product API did not confirm USD pricing")
        stock = product.get("in_stock") if isinstance(product.get("in_stock"), bool) else None
        extra["seller"] = product.get("seller_name")
        extra["location"] = (data.get("search_information") or {}).get("location")
    if price is None or not price.is_finite() or price <= 0 or not product.get("title"):
        return failed("product API did not return a usable product price and title")
    return StoreResult(store=store, url=url, title=product["title"], price=price,
                       currency=currency, in_stock=stock, sku=item_id, method=method, extra=extra)


async def fallback(url: str) -> StoreResult | None:
    if not settings.pw_retail_serpapi_enabled or not settings.serpapi_key:
        return None
    request = _request(url)
    if request is None:
        return None
    store, item_id, params = request
    try:
        client = await fetcher.client()
        response = await client.get("https://serpapi.com/search.json",
                                    params={**params, "no_cache": "true", "api_key": settings.serpapi_key},
                                    timeout=45.0)
        if response.status_code != 200:
            return StoreResult(store=store, url=url, method="serpapi:product",
                               error=f"product API unavailable (HTTP {response.status_code})")
        return _parse(response.json(), url, store, item_id)
    except Exception:
        # Client exception strings contain the authenticated URL. Never expose it.
        return StoreResult(store=store, url=url, method="serpapi:product",
                           error="product API request failed or returned an invalid response")
