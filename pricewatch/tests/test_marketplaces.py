"""AliExpress + keyless eBay marketplace parsers and tracking guards."""
import asyncio
import pathlib
from decimal import Decimal

from pricewatch.stores.aliexpress import parse_search
from pricewatch.stores.apis import _parse_ebay_search
from pricewatch.stores.base import StoreResult

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


# ── AliExpress search parsing (fixture captured from the live site) ──────
def test_aliexpress_parse_live_fixture():
    raw = (FIXTURES / "aliexpress_search.txt").read_text()
    results = parse_search(raw, limit=10)
    assert len(results) >= 3
    first = results[0]
    assert "SUNLU" in first.title
    assert first.currency == "USD"
    assert first.price > Decimal("20")
    assert "aliexpress.com" in first.url
    assert first.method == "aliexpress-search"
    assert first.sku       # productId retained for reference


def test_aliexpress_skips_ad_blocks_without_titles():
    raw = (
        '"productId":"111","prices":{"salePrice":{"discount":1,'
        '"priceType":"sale_price","currencyCode":"USD","minPrice":5.5,'
        '"formattedPrice":"US $5.50"}} '
        '"productId":"222","title":{"displayTitle":"Widget X 3D"},'
        '"prices":{"salePrice":{"discount":1,"priceType":"sale_price",'
        '"currencyCode":"USD","minPrice":42.5,"formattedPrice":"US $42.50"}}'
    )
    results = parse_search(raw)
    assert [r.sku for r in results] == ["222"]
    assert results[0].price == Decimal("42.5")


def test_aliexpress_unescapes_titles():
    raw = ('"productId":"333","title":{"displayTitle":"Heater \\u0026 Dryer 70\\u00b0C"},'
           '"prices":{"salePrice":{"priceType":"sale_price",'
           '"currencyCode":"USD","minPrice":9.99,"formattedPrice":"US $9.99"}}')
    results = parse_search(raw)
    assert results[0].title == "Heater & Dryer 70°C"


# ── eBay keyless search parsing ──────────────────────────────────────────
_EBAY_CARD = """
<div class="su-card-container su-card-container--vertical">
  <div class="su-card-container__header">
    <a href="https://www.ebay.com/itm/{item_id}?hash=abc"><img/></a>
  </div>
  <div class="su-card-container__content">
    <div class="s-card__title"><span class="su-styled-text">{title}</span></div>
    <div class="s-card__attribute-row">
      <span class="su-styled-text primary bold large-1 s-card__price">{price}</span>
    </div>
    <a href="https://www.ebay.com/itm/{item_id}?hash=abc">{title}</a>
  </div>
</div>
"""


def _page(*cards: str) -> str:
    return f"<html><body><ul>{''.join(cards)}</ul></body></html>"


def test_ebay_search_parse():
    raw = _page(
        _EBAY_CARD.format(item_id="123456789012", title="SUNLU AMS Heater for Bambu", price="$99.99"),
        _EBAY_CARD.format(item_id="234567890123", title="AMS Heater Upgrade Kit", price="$134.50"),
    )
    results = _parse_ebay_search(raw)
    assert len(results) == 2
    assert results[0].price == Decimal("99.99")
    assert results[0].currency == "USD"
    assert results[0].url == "https://www.ebay.com/itm/123456789012"
    assert results[0].sku == "123456789012"
    assert results[0].method == "ebay-html"


def test_ebay_search_skips_placeholder_and_dedupes():
    raw = _page(
        _EBAY_CARD.format(item_id="123456789012", title="Shop on eBay", price="$20.00"),
        _EBAY_CARD.format(item_id="234567890123", title="Real Item", price="$10.00"),
        _EBAY_CARD.format(item_id="234567890123", title="Real Item", price="$10.00"),
    )
    results = _parse_ebay_search(raw)
    assert [r.title for r in results] == ["Real Item"]


def test_ebay_search_garbage_is_safe():
    assert _parse_ebay_search("") == []
    assert _parse_ebay_search("<html><body>nothing here</body></html>") == []


# ── tracking guards for search-only stores ───────────────────────────────
def test_aliexpress_registered_but_not_trackable():
    from pricewatch import service
    from pricewatch.stores.registry import ADAPTERS
    assert ADAPTERS["aliexpress"].trackable is False
    assert service._is_trackable("aliexpress") is False
    assert service._is_trackable("west3d") is True
    assert service._is_trackable("unknown-store") is True


def test_track_query_refuses_search_only_matches(monkeypatch):
    from pricewatch import service

    async def fake_search(query, store_keys=None, limit_per_store=3, timeout=45.0):
        return [StoreResult(
            store="aliexpress", url="https://www.aliexpress.com/item/1.html",
            title="SUNLU AMS Heater for Bambu Lab", price=Decimal("99"),
            currency="USD", in_stock=True, method="aliexpress-search")]

    monkeypatch.setattr(service, "search_stores", fake_search)
    out = asyncio.run(service.track_query("SUNLU AMS Heater Bambu Lab"))
    assert out["ok"] is False
    assert "cannot be tracked" in out["error"]
    assert out["matches"][0]["store"] == "aliexpress"


def test_aliexpress_fetch_offer_is_honest():
    from pricewatch.stores.aliexpress import AliExpressAdapter
    result = asyncio.run(AliExpressAdapter().fetch_offer(
        "https://www.aliexpress.com/item/1.html"))
    assert not result.ok
    assert result.method == "unsupported"
    assert "compare_prices" in result.error
