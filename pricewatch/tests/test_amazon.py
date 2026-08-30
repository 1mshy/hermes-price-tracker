"""Amazon keyless search: SERP parsing and relevance ranking.

The fixture is a real amazon.ca `/s?k=logitech+mx+master+3s` page, cut down to
five cards (two plain, two discounted, one sponsored) with scripts, styles and
image attributes stripped — the structural attributes the parser reads are
untouched.
"""
import asyncio
import pathlib
from decimal import Decimal

from pricewatch import preferences
from pricewatch.fetch import Page
from pricewatch.stores.bigbox import AmazonAdapter, parse_amazon_search

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
RAW = (FIXTURES / "amazon_search.html").read_text()


# ── SERP parsing ─────────────────────────────────────────────────────────
def test_parses_live_fixture():
    results = parse_amazon_search(RAW, storefront="amazon.ca", currency="CAD")
    assert len(results) == 5
    first = results[0]
    assert first.currency == "CAD"
    assert first.price > Decimal("0")
    assert first.sku == "B0FB21526X"
    assert first.url == "https://www.amazon.ca/dp/B0FB21526X"
    assert first.method == "http:amazon-search"
    assert first.in_stock is True


def test_title_is_the_full_listing_name_not_just_the_brand():
    # The h2's spans lead with a separate brand node, so text() yields
    # "Logitech" and every similarity score collapses to the same value.
    titles = [r.title for r in parse_amazon_search(RAW, currency="CAD")]
    assert all(t != "Logitech" for t in titles)
    assert any("MX Master 3S" in t for t in titles)


def test_sponsored_prefix_is_stripped_but_flagged():
    results = parse_amazon_search(RAW, currency="CAD")
    ads = [r for r in results if r.extra.get("sponsored")]
    assert len(ads) == 1
    assert not ads[0].title.lower().startswith("sponsored")


def test_discounted_card_keeps_the_list_price():
    results = parse_amazon_search(RAW, currency="CAD")
    discounted = [r for r in results if "list_price" in r.extra]
    assert discounted
    for r in discounted:
        assert Decimal(r.extra["list_price"]) > r.price


def test_every_result_warns_that_the_serp_price_is_provisional():
    for r in parse_amazon_search(RAW, currency="CAD"):
        assert "confirm with get_price" in r.extra["note"]


def test_cards_without_a_price_are_skipped():
    raw = ('<div data-component-type="s-search-result" data-asin="B000000000">'
           '<h2 aria-label="Some Widget"></h2></div>')
    assert parse_amazon_search(raw) == []


def test_blocked_or_empty_page_yields_nothing():
    assert parse_amazon_search("<html><body>Robot Check</body></html>") == []
    assert parse_amazon_search("") == []


# ── search(): relevance ordering ─────────────────────────────────────────
def _search(monkeypatch, query, limit):
    async def fake_get(url, **kwargs):
        return Page("https://www.amazon.ca" + url.split("amazon.ca")[-1], 200, RAW, "http")

    import pricewatch.stores.bigbox as bigbox
    monkeypatch.setattr(bigbox.fetcher, "get", fake_get)
    return asyncio.run(AmazonAdapter().search(query, limit=limit))


def test_search_ranks_relevance_above_amazon_page_order(monkeypatch):
    # The sponsored keyboard sits above the real product on the live page; a
    # naive `[:limit]` would return the ad and drop what was asked for.
    top = _search(monkeypatch, "logitech mx master 3s", limit=2)
    assert len(top) == 2
    assert "MX Master 3S" in top[0].title
    assert not any(r.extra.get("sponsored") for r in top)


def test_search_respects_the_limit(monkeypatch):
    assert len(_search(monkeypatch, "logitech mouse", limit=3)) == 3


def test_search_gives_up_quietly_when_amazon_walls_the_page(monkeypatch):
    async def blocked(url, **kwargs):
        return Page(url, 200, "<html>Enter the characters you see below</html>", "http")

    import pricewatch.stores.bigbox as bigbox
    monkeypatch.setattr(bigbox.fetcher, "get", blocked)
    assert asyncio.run(AmazonAdapter().search("anything")) == []


def test_amazon_is_now_searchable_in_the_registry():
    from pricewatch.stores.registry import searchable
    assert searchable("amazon")


# ── retired ASINs ────────────────────────────────────────────────────────
def test_retired_asin_reports_not_found_without_a_browser(monkeypatch):
    body = ("<html><title>Page Not Found</title><body>To discuss automated access "
            "to Amazon data please contact api-services-support@amazon.com</body></html>")
    rendered = []

    async def get(url, **kwargs):
        return Page(url, 404, body, "http")

    async def render(url, **kwargs):
        rendered.append(url)
        raise AssertionError("a 404 must never cost a browser render")

    import pricewatch.stores.bigbox as bigbox
    monkeypatch.setattr(bigbox.fetcher, "get", get)
    monkeypatch.setattr(bigbox.fetcher, "render", render)
    result = asyncio.run(AmazonAdapter().fetch_offer("https://www.amazon.ca/dp/B08MVFJ1RJ"))
    assert result.method == "http:not-found"
    assert "no longer listed" in result.error
    assert rendered == []


# ── storefront routing ───────────────────────────────────────────────────
def test_search_uses_the_regional_storefront(monkeypatch):
    seen = {}

    async def get(url, **kwargs):
        seen["url"] = url
        return Page(url, 200, RAW, "http")

    import pricewatch.stores.bigbox as bigbox
    monkeypatch.setattr(bigbox.fetcher, "get", get)
    monkeypatch.setattr(preferences, "preferred_region", lambda: "CA")
    results = asyncio.run(AmazonAdapter().search("logitech mx master 3s", limit=2))
    assert "amazon.ca/s?k=" in seen["url"]
    assert all(r.currency == "CAD" for r in results)
    assert all(r.url.startswith("https://www.amazon.ca/dp/") for r in results)


# ── accessory noise ──────────────────────────────────────────────────────
_CARD = ('<div data-component-type="s-search-result" data-asin="{asin}">'
         '<h2 aria-label="{title}"></h2>'
         '<span class="a-price"><span class="a-offscreen">${price}</span></span>'
         '</div>')


def _page(*cards):
    return "<html><body>" + "".join(_CARD.format(**c) for c in cards) + "</body></html>"


def _run(monkeypatch, raw, query, limit=3):
    async def get(url, **kwargs):
        return Page(url, 200, raw, "http")

    import pricewatch.stores.bigbox as bigbox
    monkeypatch.setattr(bigbox.fetcher, "get", get)
    return asyncio.run(AmazonAdapter().search(query, limit=limit))


def test_accessories_are_demoted_below_the_actual_product(monkeypatch):
    # All three titles carry every query token, so similarity alone ties them.
    raw = _page(
        {"asin": "B00ACC00001", "title": "Mouse Feet Replacement for Logitech MX Master 3S",
         "price": "11.99"},
        {"asin": "B00ACC00002", "title": "Hard Travel Case for Logitech MX Master 3S",
         "price": "13.99"},
        {"asin": "B00PROD0001", "title": "Logitech MX Master 3S Wireless Mouse - Graphite",
         "price": "119.99"},
    )
    top = _run(monkeypatch, raw, "logitech mx master 3s", limit=1)
    assert top[0].sku == "B00PROD0001"


def test_demotes_accessories_it_has_no_keyword_for(monkeypatch):
    # "Grip Tape for X" is not on any keyword list; what marks it is that the
    # query's words sit after the "for", not before it.
    raw = _page(
        {"asin": "B00ACC00003", "title": "Mouse Grip Tape for Logitech MX Master 3S",
         "price": "13.99"},
        {"asin": "B00PROD0001", "title": "Logitech MX Master 3S Wireless Mouse",
         "price": "119.99"},
    )
    assert _run(monkeypatch, raw, "logitech mx master 3s", limit=1)[0].sku == "B00PROD0001"


def test_a_product_variant_named_for_something_is_not_an_accessory(monkeypatch):
    raw = _page(
        {"asin": "B00ACC00003", "title": "Mouse Grip Tape for Logitech MX Master 3S",
         "price": "13.99"},
        {"asin": "B00PROD0002", "title": "Logitech MX Master 3S for Business, Graphite",
         "price": "129.99"},
    )
    assert _run(monkeypatch, raw, "logitech mx master 3s", limit=1)[0].sku == "B00PROD0002"


def test_an_accessory_query_still_returns_accessories(monkeypatch):
    raw = _page(
        {"asin": "B00ACC00002", "title": "Hard Travel Case for Logitech MX Master 3S",
         "price": "13.99"},
        {"asin": "B00PROD0001", "title": "Logitech MX Master 3S Wireless Mouse - Graphite",
         "price": "119.99"},
    )
    top = _run(monkeypatch, raw, "travel case for logitech mx master 3s", limit=1)
    assert top[0].sku == "B00ACC00002"


def test_renewed_units_are_flagged_not_passed_off_as_new(monkeypatch):
    raw = _page({"asin": "B00RENEW001", "title": "Logitech MX Master 3S, Black (Renewed)",
                 "price": "114.99"})
    assert _run(monkeypatch, raw, "logitech mx master 3s")[0].extra["condition"] == "renewed"


def test_new_listings_carry_no_condition_flag(monkeypatch):
    raw = _page({"asin": "B00PROD0001", "title": "Logitech MX Master 3S Wireless Mouse",
                 "price": "119.99"})
    assert "condition" not in _run(monkeypatch, raw, "logitech mx master 3s")[0].extra


# ── caveats reach the caller ─────────────────────────────────────────────
def test_as_dict_surfaces_the_caveats_the_agent_needs(monkeypatch):
    raw = _page({"asin": "B00RENEW001", "title": "Logitech MX Master 3S (Renewed)",
                 "price": "114.99"})
    payload = _run(monkeypatch, raw, "logitech mx master 3s")[0].as_dict()
    assert payload["extra"]["condition"] == "renewed"
    assert "confirm with get_price" in payload["extra"]["note"]


def test_as_dict_omits_extra_when_there_is_nothing_to_say():
    from pricewatch.stores.base import StoreResult
    assert "extra" not in StoreResult(store="x", url="u").as_dict()
    assert "extra" not in StoreResult(store="x", url="u", extra={"sold": None}).as_dict()


def test_used_in_ordinary_copy_is_not_read_as_a_condition(monkeypatch):
    # This flag now decides what gets tracked, so a false positive costs a
    # real listing — a bare "used" is too common in marketing copy to trust.
    raw = _page({"asin": "B00PROD0003", "title": "Logitech MX Master 3S — used by professionals",
                 "price": "119.99"})
    assert "condition" not in _run(monkeypatch, raw, "logitech mx master 3s")[0].extra
