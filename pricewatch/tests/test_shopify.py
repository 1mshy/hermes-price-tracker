"""Shopify storefronts, and the Markets prefix that changes what a price means."""
import asyncio
from decimal import Decimal

import pytest

from pricewatch.fetch import Page
from pricewatch.stores import shopify

CA_URL = "https://shop.example.com/en-ca/products/pla-pro"
US_URL = "https://shop.example.com/products/pla-pro"

_PRODUCT_JS = {
    "id": 42, "title": "PLA Pro", "available": True,
    "variants": [
        {"id": 1, "title": "Black", "sku": "PLA-BK", "price": 3699,
         "compare_at_price": 4299, "available": True},
        {"id": 2, "title": "Red", "sku": "PLA-RD", "price": 3499, "available": False},
    ],
}


@pytest.fixture(autouse=True)
def _clear_market_caches():
    shopify._MARKET_CURRENCY.clear()
    shopify._PREFERRED_MARKET.clear()
    yield
    shopify._MARKET_CURRENCY.clear()
    shopify._PREFERRED_MARKET.clear()


def _patch_json(monkeypatch, routes: dict, seen: list | None = None):
    """Serve canned JSON per URL; anything unrouted 404s like a real store."""
    async def fake_get_json(url, **kwargs):
        if seen is not None:
            seen.append(url)
        for prefix, payload in routes.items():
            if url.startswith(prefix):
                return payload
        raise RuntimeError(f"HTTP 404 for {url}")

    monkeypatch.setattr(shopify.fetcher, "get_json", fake_get_json)


def _no_preference(monkeypatch):
    monkeypatch.setattr(shopify.preferences, "preferred_currency", lambda: "")
    monkeypatch.setattr(shopify.preferences, "preferred_region", lambda: "")


# ── Markets locale prefix ────────────────────────────────────────────────
def test_locale_prefix_recognised_only_where_it_is_one():
    assert shopify._locale_prefix(CA_URL) == "/en-ca"
    assert shopify._locale_prefix("https://s.com/de/products/x") == "/de"
    assert shopify._locale_prefix(US_URL) == ""
    assert shopify._locale_prefix("https://s.com/collections/pla/products/x") == ""
    # A bare market homepage has nothing after the locale — not a product URL.
    assert shopify._locale_prefix("https://s.com/en-ca") == ""


def test_market_root_keeps_the_prefix():
    assert shopify._market_root(CA_URL) == "https://shop.example.com/en-ca"
    assert shopify._market_root(US_URL) == "https://shop.example.com"


def test_fetch_offer_reads_the_market_the_url_points_at(monkeypatch):
    seen: list[str] = []
    _patch_json(monkeypatch, {
        "https://shop.example.com/en-ca/products/pla-pro.js": _PRODUCT_JS,
        "https://shop.example.com/en-ca/cart.js": {"currency": "CAD"},
        # The default market is a different price list entirely.
        "https://shop.example.com/products/pla-pro.js": {**_PRODUCT_JS, "variants": [
            {"id": 1, "price": 2599, "available": True}]},
        "https://shop.example.com/cart.js": {"currency": "USD"},
    }, seen)

    result = asyncio.run(shopify.ShopifyAdapter().fetch_offer(CA_URL))
    assert result.price == Decimal("36.99")
    assert result.currency == "CAD"
    assert result.extra["market"] == "/en-ca"
    assert any("/en-ca/products/pla-pro.js" in url for url in seen)
    assert not any(url == "https://shop.example.com/products/pla-pro.js" for url in seen)


def test_fetch_offer_without_a_prefix_reads_the_default_market(monkeypatch):
    _patch_json(monkeypatch, {
        "https://shop.example.com/products/pla-pro.js": {**_PRODUCT_JS, "variants": [
            {"id": 1, "price": 2599, "available": True}]},
        "https://shop.example.com/cart.js": {"currency": "USD"},
    })
    result = asyncio.run(shopify.ShopifyAdapter().fetch_offer(US_URL))
    assert result.price == Decimal("25.99")
    assert result.currency == "USD"
    assert result.extra["market"] is None


# ── currency ─────────────────────────────────────────────────────────────
def test_currency_comes_from_cart_js_not_the_hostname(monkeypatch):
    """A .com storefront that bills CAD is exactly what the host guess gets wrong."""
    _patch_json(monkeypatch, {
        "https://shop.example.com/products/pla-pro.js": _PRODUCT_JS,
        "https://shop.example.com/cart.js": {"currency": "CAD"},
    })
    result = asyncio.run(shopify.ShopifyAdapter().fetch_offer(US_URL))
    assert result.currency == "CAD"


def test_currency_falls_back_to_the_hostname_when_cart_js_is_absent(monkeypatch):
    _patch_json(monkeypatch, {"https://shop.example.ca/products/pla-pro.js": _PRODUCT_JS})
    result = asyncio.run(
        shopify.ShopifyAdapter().fetch_offer("https://shop.example.ca/products/pla-pro"))
    assert result.currency == "CAD"


def test_market_currency_is_asked_once_per_market(monkeypatch):
    seen: list[str] = []
    _patch_json(monkeypatch, {"https://s.com/cart.js": {"currency": "GBP"}}, seen)
    for _ in range(3):
        assert asyncio.run(shopify.market_currency("https://s.com", "USD")) == "GBP"
    assert seen.count("https://s.com/cart.js") == 1


# ── variants and the legacy .json view ───────────────────────────────────
def test_variant_query_param_wins_over_cheapest(monkeypatch):
    _patch_json(monkeypatch, {
        "https://shop.example.com/products/pla-pro.js": _PRODUCT_JS,
        "https://shop.example.com/cart.js": {"currency": "USD"},
    })
    result = asyncio.run(shopify.ShopifyAdapter().fetch_offer(US_URL + "?variant=2"))
    assert result.price == Decimal("34.99")
    assert result.sku == "PLA-RD"
    assert result.in_stock is False


def test_cheapest_available_variant_is_the_starting_price(monkeypatch):
    _patch_json(monkeypatch, {
        "https://shop.example.com/products/pla-pro.js": _PRODUCT_JS,
        "https://shop.example.com/cart.js": {"currency": "USD"},
    })
    result = asyncio.run(shopify.ShopifyAdapter().fetch_offer(US_URL))
    # 34.99 is cheaper but sold out, so the honest quote is the 36.99 in stock.
    assert result.price == Decimal("36.99")
    assert result.in_stock is True
    assert result.extra["compare_at"] == Decimal("42.99")


def test_falls_back_to_dot_json_when_dot_js_is_unavailable(monkeypatch):
    _patch_json(monkeypatch, {
        "https://shop.example.com/products/pla-pro.json": {"product": {
            "id": 42, "title": "PLA Pro",
            "variants": [{"id": 1, "title": "Black", "sku": "PLA-BK", "price": "36.99"}],
        }},
        "https://shop.example.com/cart.js": {"currency": "USD"},
    })
    result = asyncio.run(shopify.ShopifyAdapter().fetch_offer(US_URL))
    assert result.price == Decimal("36.99")
    assert result.method == "shopify-json-legacy"
    # That view carries no availability flag — unknown, not sold out.
    assert result.in_stock is None


def test_both_views_missing_reports_the_dot_js_error(monkeypatch):
    _patch_json(monkeypatch, {})
    result = asyncio.run(shopify.ShopifyAdapter().fetch_offer(US_URL))
    assert result.price is None
    assert "shopify .js failed" in result.error


def test_non_product_url_is_rejected(monkeypatch):
    _patch_json(monkeypatch, {})
    result = asyncio.run(
        shopify.ShopifyAdapter().fetch_offer("https://shop.example.com/collections/pla"))
    assert result.error == "not a Shopify product URL"


# ── search ───────────────────────────────────────────────────────────────
_SUGGEST = {"resources": {"results": {"products": [
    {"title": "PLA Pro", "url": "/en-ca/products/pla-pro?_pos=1", "price": "22.99",
     "available": True}]}}}


def test_search_switches_to_the_market_that_bills_in_your_currency(monkeypatch):
    monkeypatch.setattr(shopify.preferences, "preferred_currency", lambda: "CAD")
    monkeypatch.setattr(shopify.preferences, "preferred_region", lambda: "CA")
    seen: list[str] = []
    _patch_json(monkeypatch, {
        "https://shop.example.com/en-ca/cart.js": {"currency": "CAD"},
        "https://shop.example.com/en-ca/search/suggest.json": _SUGGEST,
        "https://shop.example.com/cart.js": {"currency": "USD"},
    }, seen)

    adapter = shopify.ShopifyAdapter(name="example", domains=("shop.example.com",))
    results = asyncio.run(adapter.search("pla", limit=3))
    assert [r.currency for r in results] == ["CAD"]
    assert results[0].price == Decimal("22.99")
    # suggest.json already returns market paths; joining must not double them up.
    assert results[0].url == "https://shop.example.com/en-ca/products/pla-pro?_pos=1"
    assert any("/en-ca/search/suggest.json" in url for url in seen)


def test_search_stays_put_when_no_market_bills_in_your_currency(monkeypatch):
    monkeypatch.setattr(shopify.preferences, "preferred_currency", lambda: "CAD")
    monkeypatch.setattr(shopify.preferences, "preferred_region", lambda: "CA")
    _patch_json(monkeypatch, {
        "https://shop.example.com/cart.js": {"currency": "USD"},
        "https://shop.example.com/search/suggest.json": {"resources": {"results": {
            "products": [{"title": "PLA Pro", "url": "/products/pla-pro",
                          "price": "23.99", "available": True}]}}},
    })
    adapter = shopify.ShopifyAdapter(name="example", domains=("shop.example.com",))
    results = asyncio.run(adapter.search("pla"))
    # USD is what it charges, so USD is what it gets reported as.
    assert [r.currency for r in results] == ["USD"]
    assert results[0].url == "https://shop.example.com/products/pla-pro"


def test_market_probe_is_skipped_without_a_currency_preference(monkeypatch):
    _no_preference(monkeypatch)
    seen: list[str] = []
    _patch_json(monkeypatch, {"https://shop.example.com/cart.js": {"currency": "USD"}}, seen)
    assert asyncio.run(shopify.preferred_market("shop.example.com", "USD")) == ""
    assert seen == []


def test_adapter_without_domains_does_not_search(monkeypatch):
    _patch_json(monkeypatch, {})
    assert asyncio.run(shopify.ShopifyAdapter().search("pla")) == []


# ── platform detection ───────────────────────────────────────────────────
def _patch_get(monkeypatch, routes: dict):
    async def fake_get(url, **kwargs):
        for prefix, (status, body) in routes.items():
            if url.startswith(prefix):
                return Page(url, status, body, "http")
        return Page(url, 404, "", "http")

    monkeypatch.setattr(shopify.fetcher, "get", fake_get)


def test_detects_via_open_products_json(monkeypatch):
    _patch_get(monkeypatch, {
        "https://s.com/products.json": (200, '{"products":[{"id":1}]}')})
    assert asyncio.run(shopify.looks_like_shopify("https://s.com/products/x")) is True


def test_detects_via_theme_markers_when_products_json_is_gated(monkeypatch):
    _patch_get(monkeypatch, {
        "https://s.com/products.json": (404, "Not Found"),
        "https://s.com/products/x": (200,
            '<html><body><div id="shopify-section-header"></div>'
            '<img src="https://cdn.shopify.com/s/files/1/x.png"></body></html>')})
    assert asyncio.run(shopify.looks_like_shopify("https://s.com/products/x")) is True


def test_non_shopify_store_is_not_claimed(monkeypatch):
    _patch_get(monkeypatch, {
        "https://s.com/products.json": (404, "Not Found"),
        "https://s.com/products/x": (200, "<html><body>WooCommerce shop</body></html>")})
    assert asyncio.run(shopify.looks_like_shopify("https://s.com/products/x")) is False
