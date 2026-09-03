"""Canada-first store coverage: CAD retailers, `ca.` OEM storefronts, and the
rule that a bare `$` is the host's own dollar. Offline — fixtures only."""
import asyncio
import json
import re
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pricewatch import preferences, service, verify
from pricewatch.api import router
from pricewatch.db import init_db
from pricewatch.fetch import Page
from pricewatch.money import currency_from_text, detect_currency, region_for_currency
from pricewatch.stores import apis, bigbox, canadacomputers, catalog, structured
from pricewatch.stores import registry as reg
from pricewatch.verify import PRODUCT_PATTERNS

FIXTURES = Path(__file__).parent / "fixtures"
CANADIAN = ("bestbuyca", "canadacomputers", "memoryexpress", "voxelfactory", "3dprintingcanada",
            "filamentsca", "digitmakers", "shop3d", "spool3d")


@pytest.fixture
def prefers(monkeypatch):
    def _set(currency: str, region: str = ""):
        monkeypatch.setattr(preferences, "preferred_currency", lambda: currency)
        monkeypatch.setattr(preferences, "preferred_region",
                            lambda: region or region_for_currency(currency))
    return _set


@pytest.fixture
def clean_shopify_caches():
    from pricewatch.stores import shopify
    shopify._MARKET_CURRENCY.clear()
    shopify._PREFERRED_MARKET.clear()
    yield
    shopify._MARKET_CURRENCY.clear()
    shopify._PREFERRED_MARKET.clear()


@pytest.fixture
def no_preference(monkeypatch):
    monkeypatch.setattr(preferences, "preferred_currency", lambda: "")
    monkeypatch.setattr(preferences, "preferred_region", lambda: "")


def _bestbuy_json(monkeypatch, routes: dict, seen: list | None = None):
    async def fake_get_json(url, **kwargs):
        if seen is not None:
            seen.append((url, kwargs.get("headers") or {}))
        for prefix, payload in routes.items():
            if url.startswith(prefix):
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise RuntimeError(f"HTTP 404 for {url}")
    monkeypatch.setattr(apis.fetcher, "get_json", fake_get_json)


# ── Best Buy Canada: keyless JSON API ────────────────────────────────────
def test_bestbuyca_sku_from_url():
    sku = apis.BestBuyCanadaAdapter._sku
    assert sku("https://www.bestbuy.ca/en-ca/product/bambu-lab-p2s-ams-combo/19854444") == "19854444"
    assert sku("https://www.bestbuy.ca/en-ca/product/bambu-lab-p2s/19854444?icmp=x") == "19854444"
    assert sku("https://www.bestbuy.ca/fr-ca/produit/x/19854442/") == "19854442"
    assert sku("https://www.bestbuy.ca/en-ca/search?sku=19854444") == "19854444"
    assert sku("https://www.bestbuy.ca/en-ca/product?skuId=19854442") == "19854442"
    assert sku("https://www.bestbuy.ca/en-ca/category/3d-printers/123") is None


def test_bestbuyca_fetch_offer_reads_cad_and_stock(monkeypatch):
    seen: list = []
    payload = json.loads((FIXTURES / "bestbuyca_product.json").read_text())
    _bestbuy_json(monkeypatch, {"https://www.bestbuy.ca/api/v2/json/product/19854444": payload}, seen)
    result = asyncio.run(reg.fetch_offer(
        "https://www.bestbuy.ca/en-ca/product/bambu-lab-p2s-ams-combo/19854444"))
    assert result.ok
    assert result.store == "bestbuyca"
    assert result.price == Decimal("999.99")
    assert result.currency == "CAD"
    assert result.in_stock is True
    assert result.sku == "19854444"
    assert result.method == "bestbuyca-api"
    assert result.url.startswith("https://www.bestbuy.ca/en-ca/product/")
    assert "P2S" in result.title
    assert result.extra["marketplace"] is False
    assert result.extra["regular_price"] == 999.99
    assert result.extra["on_sale"] is False
    assert "note" not in result.as_dict().get("extra", {})
    url, headers = seen[0]
    assert url.endswith("?lang=en")
    assert headers["Accept"] == "application/json"
    assert headers["Accept-Language"].startswith("en-CA")


def test_bestbuyca_search_flags_marketplace_sellers(monkeypatch):
    payload = json.loads((FIXTURES / "bestbuyca_search.json").read_text())
    _bestbuy_json(monkeypatch, {"https://www.bestbuy.ca/api/v2/json/search?query=bambu%20lab%20p1s": payload})
    results = asyncio.run(reg.ADAPTERS["bestbuyca"].search("bambu lab p1s", limit=3))
    assert len(results) == 3
    assert all(r.currency == "CAD" and r.ok for r in results)
    assert all(r.url.startswith("https://www.bestbuy.ca/en-ca/product/") for r in results)
    assert all(r.in_stock is None for r in results)          # search carries no availability
    by_sku = {r.sku: r for r in results}
    assert by_sku["19854442"].price == Decimal("719.99")
    assert by_sku["19854442"].extra["marketplace"] is False
    third_party = by_sku["20006995"]
    assert third_party.extra["marketplace"] is True
    assert third_party.extra["seller"] == "3D Printing Canada"
    assert "marketplace" in third_party.extra["note"]
    assert "marketplace" in third_party.as_dict()["extra"]["note"]


def test_bestbuyca_unknown_sku_is_a_clear_error(monkeypatch):
    _bestbuy_json(monkeypatch, {})                            # every SKU 404s
    result = asyncio.run(reg.fetch_offer("https://www.bestbuy.ca/en-ca/product/nope/00000001"))
    assert not result.ok
    assert result.price is None
    assert "not found" in result.error and "404" in result.error
    assert result.method == "bestbuyca-api"
    no_sku = asyncio.run(reg.fetch_offer("https://www.bestbuy.ca/en-ca/category/printers"))
    assert "no SKU" in no_sku.error


# ── Canada Computers: search cards ───────────────────────────────────────
def test_canadacomputers_search_parser_reads_cards():
    raw = (FIXTURES / "canadacomputers_search.html").read_text()
    results = canadacomputers.parse_search(raw, limit=10)
    assert len(results) >= 3
    for r in results:
        assert r.ok and r.currency == "CAD" and r.method == "http:cc-search"
        assert re.search(r"^https://www\.canadacomputers\.com/en/[^/]+/\d+/[^/]+\.html$", r.url), r.url
        assert "keyword=" not in r.url
        assert r.title and r.sku
    p1s = next(r for r in results if "P1S Combo" in r.title)
    assert p1s.price == Decimal("719.99")
    assert p1s.in_stock is True
    assert p1s.sku == "QPBAM00014"
    assert "280601" in p1s.url
    assert canadacomputers.parse_search(raw, limit=2)[1].title.startswith("Bambu Lab A1")
    assert canadacomputers.parse_search("<html><body>nothing</body></html>") == []


def test_canadacomputers_search_uses_the_search_page(monkeypatch):
    raw = (FIXTURES / "canadacomputers_search.html").read_text()
    seen: list[str] = []

    async def fake_get(url, **kwargs):
        seen.append(url)
        return Page(url, 200, raw, "http")
    monkeypatch.setattr(canadacomputers.fetcher, "get", fake_get)
    results = asyncio.run(reg.ADAPTERS["canadacomputers"].search("bambu lab", limit=3))
    assert seen == ["https://www.canadacomputers.com/en/search?s=bambu+lab"]
    assert len(results) == 3 and all(r.store == "canadacomputers" for r in results)

    async def blocked(url, **kwargs):
        return Page(url, 403, "", "http")
    monkeypatch.setattr(canadacomputers.fetcher, "get", blocked)
    assert asyncio.run(reg.ADAPTERS["canadacomputers"].search("bambu lab")) == []


def test_canadacomputers_product_page_defaults_to_cad(monkeypatch):
    # A .com that bills CAD: with no priceCurrency in the markup and only a
    # bare "$", the adapter's fixed currency must win over the hostname guess.
    html = ('<html><head><title>Spool</title><script type="application/ld+json">'
            '{"@type":"Product","name":"PLA spool","offers":{"@type":"Offer","price":"19.99",'
            '"availability":"https://schema.org/InStock"}}</script></head><body>$19.99</body></html>')

    async def fake(url, **kwargs):
        return Page(url, 200, html, "http")
    monkeypatch.setattr(structured.fetcher, "get_or_render", fake)
    result = asyncio.run(reg.fetch_offer(
        "https://www.canadacomputers.com/en/filaments/265696/bambu-lab-pla-basic-filament-black.html"))
    assert result.ok and result.store == "canadacomputers"
    assert result.price == Decimal("19.99")
    assert result.currency == "CAD"


# ── a bare "$" means the host's own dollar ───────────────────────────────
def test_bare_dollar_resolves_to_the_hosts_currency():
    assert currency_from_text("$88.41", "newegg.ca") == "CAD"
    assert currency_from_text("$88.41", "www.newegg.ca") == "CAD"
    assert currency_from_text("$88", "newegg.com") == "USD"
    assert currency_from_text("$88", "shop.example.com.au") == "AUD"


def test_explicit_currency_marks_beat_the_host():
    assert currency_from_text("US$88", "newegg.ca") == "USD"
    assert currency_from_text("CDN$ 12.00", "newegg.com") == "CAD"
    assert currency_from_text("CDN$ 12.00") == "CAD"
    assert currency_from_text("CA$ 12.00", "example.com") == "CAD"
    assert currency_from_text("€12", "3djake.com") == "EUR"
    assert currency_from_text("£12", "amazon.ca") == "GBP"
    assert currency_from_text("CAD 138.73", "newegg.com") == "CAD"


def test_bare_dollar_without_a_host_is_the_default():
    assert currency_from_text("$88.41") == "USD"
    assert currency_from_text("$88.41", None, "CAD") == "CAD"
    assert currency_from_text("$88.41", "", "EUR") == "EUR"
    assert currency_from_text("$88.41", "localhost", "CAD") == "CAD"     # unknown host → default
    assert currency_from_text("no money here", "newegg.ca", "EUR") == "EUR"
    assert currency_from_text(None, "newegg.ca", "EUR") == "EUR"
    # The old spelling keeps working and gains the same host awareness.
    assert detect_currency("$99.99") == "USD"
    assert detect_currency("$99.99", "USD", host="newegg.ca") == "CAD"


def test_newegg_ca_selector_price_is_cad(monkeypatch):
    html = ('<html><body><h1 class="product-title">Creality tool kit</h1>'
            '<div class="product-price"><li class="price-current"><strong>88</strong><sup>.41</sup>'
            '</li></div><div class="product-inventory"><strong>In stock.</strong></div></body></html>')
    calls: list[str] = []

    async def fake(url, **kwargs):
        calls.append(url)
        return Page(url, 200, html.replace("<strong>88", "$<strong>88"), "http")
    monkeypatch.setattr(bigbox.fetcher, "get_or_render", fake)
    ca = asyncio.run(reg.fetch_offer("https://www.newegg.ca/creality-3d-tool-kit-filament/p/298-00N0-004D8"))
    assert ca.store == "newegg" and ca.ok
    assert ca.price == Decimal("88.41")
    assert ca.currency == "CAD"
    assert ca.in_stock is True
    us = asyncio.run(reg.fetch_offer("https://www.newegg.com/creality-3d-tool-kit-filament/p/298-00N0-004D8"))
    assert us.currency == "USD"


def test_walmart_ca_reports_cad_without_a_currency_unit(monkeypatch):
    next_data = {"props": {"pageProps": {"initialData": {"data": {"product": {
        "name": "EL3D PLA Silver 1kg", "usItemId": "1DTTTSVGCWNF", "availabilityStatus": "IN_STOCK",
        "priceInfo": {"currentPrice": {"price": 27.95, "priceString": "$27.95"}}}}}}}}
    html = f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script></body></html>'

    async def fake(url, **kwargs):
        return Page(url, 200, html, "http")
    monkeypatch.setattr(bigbox.fetcher, "get_or_render", fake)
    result = asyncio.run(reg.fetch_offer(
        "https://www.walmart.ca/en/ip/EL3D-3D-Printer-Filament-PLA-Silver-1-75mm-1kg/1DTTTSVGCWNF"))
    assert result.store == "walmart" and result.ok
    assert result.price == Decimal("27.95")
    assert result.currency == "CAD"
    assert result.method.endswith(":next-data")
    assert result.sku == "1DTTTSVGCWNF"


def test_walmart_ca_jsonld_fallback_defaults_to_cad(monkeypatch):
    html = ('<html><head><script type="application/ld+json">{"@type":"Product","name":"PLA Silver",'
            '"offers":{"@type":"Offer","price":"28.49","availability":"https://schema.org/InStock"}}'
            '</script></head><body>$28.49</body></html>')

    async def fake(url, **kwargs):
        return Page(url, 200, html, "http")
    monkeypatch.setattr(bigbox.fetcher, "get_or_render", fake)
    result = asyncio.run(reg.fetch_offer("https://www.walmart.ca/en/ip/PLA-Silver/1DTTTSVGCWNF"))
    assert result.ok and result.price == Decimal("28.49")
    assert result.currency == "CAD"
    assert result.method.endswith(":json-ld")


# ── catalog: countries, routing, storefronts ─────────────────────────────
def test_every_store_has_a_country_and_the_canadians_are_ca():
    for entry in catalog.CATALOG:
        assert re.fullmatch(r"[A-Z]{2}", entry["country"]), entry["key"]
    for key in CANADIAN:
        assert catalog.BY_KEY[key]["country"] == "CA", key
    assert set(CANADIAN) <= set(catalog.keys_for_country("CA"))
    assert catalog.keys_for_country("ca") == catalog.keys_for_country("CA")
    # Where the TLD lies, the override wins.
    assert catalog.BY_KEY["prusa"]["country"] == "CZ"
    assert catalog.BY_KEY["e3d"]["country"] == "GB"
    assert catalog.BY_KEY["fillamentum"]["country"] == "CZ"
    assert catalog.BY_KEY["3djake"]["country"] == "AT"
    assert catalog.BY_KEY["amazon"]["country"] == "US"
    assert catalog.BY_KEY["bambulab"]["country"] == "US"


def test_canadian_urls_resolve_to_their_stores():
    assert reg.store_key_for_url("https://www.bestbuy.ca/en-ca/product/x/19854444") == "bestbuyca"
    assert reg.store_key_for_url("https://www.bestbuy.com/site/6535723.p") == "bestbuy"
    assert reg.store_key_for_url("https://www.canadacomputers.com/en/fdm-3d-printer/280601/x.html") == "canadacomputers"
    assert reg.store_key_for_url("https://www.walmart.ca/en/ip/x/1DTTTSVGCWNF") == "walmart"
    assert reg.store_key_for_url("https://www.newegg.ca/x/p/298-00N0-004D8") == "newegg"
    assert reg.store_key_for_url("https://www.voxelfactory.com/products/pla") == "voxelfactory"
    assert reg.store_key_for_url("https://spool3d.ca/bambu-lab-part-cooling-fan-h2d/") == "spool3d"
    assert reg.store_key_for_url("https://www.memoryexpress.com/Products/MX00123456") == "memoryexpress"
    assert reg.store_key_for_url("https://ca.store.creality.com/products/ender-3-v3") == "creality"
    # The adapters agree with the catalog, which is what fetch_offer routes on.
    assert reg.ADAPTERS["walmart"].matches("https://www.walmart.ca/en/ip/x/1")
    assert reg.ADAPTERS["bestbuyca"].matches("https://www.bestbuy.ca/en-ca/product/x/1")
    assert not reg.ADAPTERS["bestbuy"].matches("https://www.bestbuy.ca/en-ca/product/x/1")


def test_oem_storefronts_follow_the_shopper(prefers):
    prefers("CAD", "CA")
    assert reg.ADAPTERS["creality"].storefront() == "ca.store.creality.com"
    assert reg.ADAPTERS["elegoo"].storefront() == "ca.elegoo.com"
    assert reg.ADAPTERS["anycubic"].storefront() == "ca.anycubic.com"
    assert reg.ADAPTERS["qidi"].storefront() == "ca.qidi3d.com"
    assert reg.ADAPTERS["bambulab"].storefront() == "ca.store.bambulab.com"
    # Canadian retailers have one storefront and it is already the right one.
    assert reg.ADAPTERS["voxelfactory"].storefront() == "voxelfactory.com"


def test_oem_storefronts_default_to_the_primary_domain(no_preference):
    assert reg.ADAPTERS["creality"].storefront() == "store.creality.com"
    assert reg.ADAPTERS["elegoo"].storefront() == "us.elegoo.com"
    assert reg.ADAPTERS["anycubic"].storefront() == "store.anycubic.com"
    assert reg.ADAPTERS["qidi"].storefront() == "qidi3d.com"


def test_catalog_summary_and_api_carry_country():
    rows = {row["key"]: row for row in reg.catalog_summary()}
    assert all("country" in row for row in rows.values())
    assert rows["voxelfactory"]["country"] == "CA"
    assert rows["bestbuyca"]["searchable"] is True
    assert rows["canadacomputers"]["searchable"] is True
    assert rows["memoryexpress"]["searchable"] is False

    init_db()
    app = FastAPI()
    app.include_router(router, prefix="/api")
    stores = TestClient(app).get("/api/stores").json()["stores"]
    assert all("country" in s for s in stores)
    assert {s["key"] for s in stores if s["country"] == "CA"} >= set(CANADIAN)


def test_verify_patterns_cover_the_new_stores():
    assert re.search(PRODUCT_PATTERNS["bestbuyca"], "/en-ca/product/bambu-lab-p2s/19854444")
    assert re.search(PRODUCT_PATTERNS["canadacomputers"], "/en/fdm-3d-printer/280601/bambu-lab-p1s.html")
    assert re.search(PRODUCT_PATTERNS["memoryexpress"], "/Products/MX00129873")
    assert not re.search(PRODUCT_PATTERNS["bestbuyca"], "/en-ca/category/3d-printers/123")
    for key in ("bestbuyca", "canadacomputers", "spool3d"):
        assert catalog.BY_KEY[key].get("probe"), key


# ── Creality: Next.js in the US, Shopify in Canada ───────────────────────
def test_creality_routes_each_market_to_its_platform(prefers):
    from pricewatch.stores.split import SplitPlatformAdapter
    adapter = reg.ADAPTERS["creality"]
    assert isinstance(adapter, SplitPlatformAdapter)
    assert adapter.reader("https://store.creality.com/products/k1-se-3d-printer") is adapter.structured
    assert adapter.reader("https://ca.store.creality.com/products/ender-3-v3-se-3d-printer") is adapter.shopify
    # Search exists only where the storefront in play is the Shopify market, and
    # list_stores says so for the current shopper rather than for the class.
    prefers("CAD", "CA")
    assert reg.searchable("creality")
    assert next(r for r in reg.catalog_summary() if r["key"] == "creality")["searchable"] is True
    prefers("", "")
    assert not reg.searchable("creality")
    assert next(r for r in reg.catalog_summary() if r["key"] == "creality")["searchable"] is False


def test_creality_searches_the_canadian_shopify_market(monkeypatch, prefers, clean_shopify_caches):
    from pricewatch.stores import shopify
    seen: list[str] = []

    async def fake_get_json(url, **kwargs):
        seen.append(url)
        if url.startswith("https://ca.store.creality.com/cart.js"):
            return {"currency": "CAD"}
        if url.startswith("https://ca.store.creality.com/search/suggest.json"):
            return {"resources": {"results": {"products": [
                {"title": "Ender-3 V3 SE 3D Printer", "price": "199.00", "available": True,
                 "url": "/products/ender-3-v3-se-3d-printer?_pos=1"}]}}}
        raise RuntimeError(f"HTTP 404 for {url}")
    monkeypatch.setattr(shopify.fetcher, "get_json", fake_get_json)

    prefers("CAD", "CA")
    hits = asyncio.run(reg.ADAPTERS["creality"].search("Ender 3 V3", limit=2))
    assert len(hits) == 1
    assert hits[0].currency == "CAD" and hits[0].price == Decimal("199.00")
    assert hits[0].url == "https://ca.store.creality.com/products/ender-3-v3-se-3d-printer"
    assert hits[0].store == "creality"
    assert all(u.startswith("https://ca.store.creality.com/") for u in seen)


def test_creality_has_no_search_on_the_us_store(no_preference, monkeypatch):
    from pricewatch.stores import shopify

    async def never(url, **kwargs):
        raise AssertionError(f"unexpected request {url}")
    monkeypatch.setattr(shopify.fetcher, "get_json", never)
    assert asyncio.run(reg.ADAPTERS["creality"].search("Ender 3 V3")) == []


# ── marketplace sellers and the tracker ──────────────────────────────────
def _bestbuyca_hits(*items: dict) -> list:
    adapter = reg.ADAPTERS["bestbuyca"]
    return [adapter._result(item, "", detailed=False) for item in items]


def _track_ranked(monkeypatch, results):
    init_db()

    async def ranked(query, **kwargs):
        return results, len(results), query
    monkeypatch.setattr(service, "_search_ranked", ranked)
    return asyncio.run(service.track_query("Bambu Lab P1S Combo", drop_pct=10))


def test_a_marketplace_seller_never_becomes_the_tracking_baseline(monkeypatch, prefers):
    # The reseller undercuts Best Buy's own price, so an unguarded min() would
    # make a third party's price the permanent baseline — the refurb trap again.
    prefers("CAD", "CA")
    payload = json.loads((FIXTURES / "bestbuyca_search.json").read_text())
    first_party = next(p for p in payload["products"] if p["sku"] == "19854442")
    reseller = {**first_party, "sku": "20009999", "salePrice": 689.0, "isMarketplace": True,
                "seller": {"name": "Some Reseller"},
                "productUrl": "/en-ca/product/bambu-lab-p1s-combo/20009999"}
    out = _track_ranked(monkeypatch, _bestbuyca_hits(first_party, reseller))
    assert out["ok"]
    assert (out["best_price"], out["currency"]) == (719.99, "CAD")
    assert [o["price"] for o in out["offers"]] == [719.99]          # the reseller is not watched
    # Kept only when that is all there is.
    only = _track_ranked(monkeypatch, _bestbuyca_hits(reseller))
    assert only["ok"] and only["best_price"] == 689.0


def test_tracking_a_marketplace_url_says_so(monkeypatch):
    init_db()
    product = json.loads((FIXTURES / "bestbuyca_product.json").read_text())
    reseller = {**product, "sku": "20006995", "isMarketplace": True,
                "seller": {"name": "3D Printing Canada"},
                "productUrl": "https://www.bestbuy.ca/en-ca/product/micro-swiss-flowtech-hotend/20006995"}
    _bestbuy_json(monkeypatch, {"https://www.bestbuy.ca/api/v2/json/product/20006995": reseller,
                                "https://www.bestbuy.ca/api/v2/json/product/19854444": product})
    out = asyncio.run(service.track_url(
        "https://www.bestbuy.ca/en-ca/product/micro-swiss-flowtech-hotend/20006995", drop_pct=10))
    assert out["ok"] and out["store"] == "bestbuyca" and out["currency"] == "CAD"
    assert "marketplace" in out["note"] and "3D Printing Canada" in out["note"]
    # Best Buy's own listing carries no such caveat.
    own = asyncio.run(service.track_url(
        "https://www.bestbuy.ca/en-ca/product/bambu-lab-p2s-ams-combo/19854444", drop_pct=10))
    assert own["ok"] and "note" not in own


# ── `country` reaches the structured reader ──────────────────────────────
def test_country_decides_what_a_bare_dollar_means_on_a_structured_com(monkeypatch):
    # The README promise: `country: CA` on a Montreal .com is enough.
    row = {"key": "montrealshop", "label": "Montreal Shop", "kind": "structured", "tags": [],
           "domains": ("montrealshop.com",), "country": "CA"}
    adapter = reg._build(row)
    assert adapter.currency == "CAD"
    html = ('<html><head><script type="application/ld+json">{"@type":"Product","name":"Spool",'
            '"offers":{"@type":"Offer","price":"49.99","availability":"https://schema.org/InStock"}}'
            '</script></head><body>$49.99</body></html>')

    async def fake(url, **kwargs):
        return Page(url, 200, html, "http")
    monkeypatch.setattr(structured.fetcher, "get_or_render", fake)
    result = asyncio.run(adapter.fetch_offer("https://www.montrealshop.com/products/spool"))
    assert result.ok and result.price == Decimal("49.99")
    assert result.currency == "CAD"
    # A hostname that already agrees with the country is left to speak for itself.
    assert reg._build({**row, "domains": ("montrealshop.ca",)}).currency is None
    assert reg._build({**row, "domains": ("usshop.com",), "country": "US"}).currency is None
    # The catalog's own lying-TLD rows, and the explicit override.
    assert reg.ADAPTERS["memoryexpress"].currency == "CAD"
    assert reg.ADAPTERS["prusa"].currency == "USD"
    assert reg._build({**row, "currency": "USD"}).currency == "USD"
    # A split store forwards it to its structured half; the Shopify half asks /cart.js.
    split = reg._build({**row, "shopify_markets": ("ca.montrealshop.com",)})
    assert split.structured.currency == "CAD"


# ── currency sniffing edge cases ─────────────────────────────────────────
def test_selector_price_without_a_glyph_is_still_the_hosts_money(monkeypatch):
    # Newegg themes put the "$" in a sibling span; the matched node then reads
    # "88.41" with no currency mark at all.
    html = ('<html><body><h1 class="product-title">Creality tool kit</h1>'
            '<span class="price-current-label">$</span>'
            '<li class="price-current"><strong>88</strong><sup>.41</sup></li></body></html>')

    async def fake(url, **kwargs):
        return Page(url, 200, html, "http")
    monkeypatch.setattr(bigbox.fetcher, "get_or_render", fake)
    ca = asyncio.run(reg.fetch_offer("https://www.newegg.ca/x/p/298-00N0-004D8"))
    assert ca.ok and ca.price == Decimal("88.41") and ca.method == "http:selector"
    assert ca.currency == "CAD"
    us = asyncio.run(reg.fetch_offer("https://www.newegg.com/x/p/298-00N0-004D8"))
    assert us.currency == "USD"


def test_iso_codes_are_matched_as_whole_words():
    # The head of a .ca electronics page says "audio" and "arcade" long before
    # it names a currency; those must not read as AUD / CAD / EUR.
    assert currency_from_text("Audio cable $12.99", "newegg.ca") == "CAD"
    assert currency_from_text("Audio cable $12.99", "newegg.com") == "USD"
    assert currency_from_text("Amateur radio kit $12.99", "newegg.ca") == "CAD"
    assert currency_from_text("Decade edition $9", "newegg.com") == "USD"
    assert currency_from_text("Audio", "newegg.ca", "EUR") == "EUR"
    assert currency_from_text("12 USD", "newegg.ca") == "USD"
    assert currency_from_text("usd 12", "newegg.ca") == "USD"
    assert currency_from_text('"currency":"CAD"', "newegg.com") == "CAD"
    assert currency_from_text("Price: CAD$12", "newegg.com") == "CAD"


# ── Best Buy Canada: the payloads that are not a price ───────────────────
def test_bestbuyca_priceless_listing_is_an_explicit_error(monkeypatch):
    product = json.loads((FIXTURES / "bestbuyca_product.json").read_text())
    gone = {**product, "salePrice": None,
            "availability": {**product["availability"], "isAvailableOnline": False,
                             "onlineAvailability": "SoldOut"}}
    _bestbuy_json(monkeypatch, {"https://www.bestbuy.ca/api/v2/json/product/19854444": gone})
    result = asyncio.run(reg.fetch_offer("https://www.bestbuy.ca/en-ca/product/x/19854444"))
    assert not result.ok and result.price is None
    assert "no price" in result.error and "19854444" in result.error
    assert result.in_stock is False
    assert result.method == "bestbuyca-api"


def test_bestbuyca_search_derives_a_url_from_the_sku(monkeypatch):
    payload = {"total": 2, "products": [
        {"sku": "12345678", "name": "Y", "salePrice": 10.0, "regularPrice": 10.0, "seoText": "y-thing"},
        {"name": "no sku, no url", "salePrice": 5.0, "regularPrice": 5.0},
    ]}
    _bestbuy_json(monkeypatch, {"https://www.bestbuy.ca/api/v2/json/search?query=y": payload})
    results = asyncio.run(reg.ADAPTERS["bestbuyca"].search("y", limit=5))
    assert [r.url for r in results] == ["https://www.bestbuy.ca/en-ca/product/y-thing/12345678"]
    assert apis.BestBuyCanadaAdapter._sku(results[0].url) == "12345678"


# ── compare: `country` is the "only my market" shortcut on both doors ─────
def test_compare_country_is_a_shortcut_for_the_users_own_stores(monkeypatch):
    seen: list = []

    async def fake_search(query, store_keys=None, limit_per_store=3, timeout=45.0):
        seen.append(store_keys)
        return []
    monkeypatch.setattr(service, "search_stores", fake_search)
    canadian = catalog.keys_for_country("CA")

    out = asyncio.run(service.compare("bambu lab p1s", country="ca"))
    assert out["stores_searched"] == canadian
    assert seen and all(keys == canadian for keys in seen)
    seen.clear()
    asyncio.run(service.compare("bambu lab p1s", stores=["amazon"], country="CA"))
    assert seen[0] == ["amazon"]                                   # explicit keys win
    seen.clear()
    nowhere = asyncio.run(service.compare("bambu lab p1s", country="ZZ"))
    assert nowhere["count"] == 0 and "ZZ" in nowhere["note"] and seen == []

    init_db()
    app = FastAPI()
    app.include_router(router, prefix="/api")
    via_api = TestClient(app).post("/api/compare", json={"query": "bambu lab p1s", "country": "CA"}).json()
    assert via_api["stores_searched"] == canadian


# ── verify: a wall is not "no product found" ─────────────────────────────
def test_verify_reports_a_walled_homepage_as_blocked(monkeypatch):
    challenge = "<html><title>Just a moment...</title><body>checking your browser</body></html>"

    async def walled(url, **kwargs):
        return Page(url, 200, challenge, "http")
    monkeypatch.setattr(verify.fetcher, "get", walled)
    entry = catalog.BY_KEY["memoryexpress"]
    url, how = asyncio.run(verify.discover_product_url(entry, allow_browser=False))
    assert (url, how) == (None, "blocked")
    assert verify._classify(None, url, how) == "BLOCKED"

    async def empty(url, **kwargs):                       # open, but nothing to scrape
        return Page(url, 200, "<html><body>" + "x" * 3000 + "</body></html>", "http")
    monkeypatch.setattr(verify.fetcher, "get", empty)
    url, how = asyncio.run(verify.discover_product_url(entry, allow_browser=False))
    assert (url, how) == (None, "not-found")
    assert verify._classify(None, url, how) == "NO-URL"
