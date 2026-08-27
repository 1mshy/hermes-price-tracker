"""AliExpress — search-only adapter.

Product pages serve a JS shell to automated clients from this host (~77 KB, no
price), but the *search* page returns full embedded JSON — including USD sale
prices once the locale cookie pins the site to US/USD. So this adapter powers
compare_prices with live marketplace prices and clearly refuses tracking: a
watch needs fetch_offer to work on every sweep, and AliExpress blocks that.

This matters because deal chatter ("people are getting these for $50") very
often points at AliExpress, where OEMs like SUNLU sell direct.
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import quote_plus

from ..fetch import fetcher
from ..money import parse_price
from .base import StoreAdapter, StoreResult

log = logging.getLogger(__name__)

#: Pins the storefront to US/USD; without it prices come back geo-localised.
_LOCALE_COOKIE = ("aep_usuc_f=site=glo&c_tp=USD&region=US&b_locale=en_US; "
                  "intl_locale=en_US")

_BLOCK_RE = re.compile(r'"productId":"(\d+)"')
_TITLE_RE = re.compile(r'"displayTitle":"((?:[^"\\]|\\.)*)"')
_SALE_RE = re.compile(
    r'"salePrice":\{[^{}]*?"currencyCode":"([A-Z]{3})","minPrice":([\d.]+)')
_URL_RE = re.compile(r'"productDetailUrl":"(https:[^"?\\]+)')
_SOLD_RE = re.compile(r'"tradeDesc":"([^"]{1,30})"')


def _unescape(raw: str) -> str:
    """Titles arrive JSON-escaped (\\u0026, \\u00b0C…)."""
    try:
        return json.loads(f'"{raw}"')
    except Exception:                                  # noqa: BLE001
        return raw.replace("\\u0026", "&")


def parse_search(raw: str, store: str = "aliexpress",
                 limit: int = 5) -> list[StoreResult]:
    """Pull product cards out of a search page's embedded JSON.

    Cards without a displayTitle are ad/brand-zone tiles — skipped. Only a
    minority of cards carry productDetailUrl; the rest get the canonical
    /item/<productId>.html form.
    """
    matches = list(_BLOCK_RE.finditer(raw))
    results: list[StoreResult] = []
    seen: set[str] = set()
    for match, following in zip(matches, matches[1:] + [None]):
        product_id = match.group(1)
        if product_id in seen:
            continue
        end = following.start() if following else min(len(raw), match.start() + 8000)
        segment = raw[match.start():end][:8000]

        title_match = _TITLE_RE.search(segment)
        sale_match = _SALE_RE.search(segment)
        if not title_match or not sale_match:
            continue
        price = parse_price(sale_match.group(2))
        if price is None:
            continue

        seen.add(product_id)
        url_match = _URL_RE.search(segment)
        sold_match = _SOLD_RE.search(segment)
        results.append(StoreResult(
            store=store,
            url=(url_match.group(1) if url_match
                 else f"https://www.aliexpress.com/item/{product_id}.html"),
            title=_unescape(title_match.group(1)),
            price=price,
            currency=sale_match.group(1),
            in_stock=True,       # listed and buyable; finer stock data not exposed
            sku=product_id,
            method="aliexpress-search",
            extra={"sold": sold_match.group(1) if sold_match else None},
        ))
        if len(results) >= limit:
            break
    return results


class AliExpressAdapter(StoreAdapter):
    name = "aliexpress"
    domains = ("aliexpress.com", "aliexpress.us")
    #: compare-only — sweeps cannot re-read a product page, so no tracking.
    trackable = False

    async def fetch_offer(self, url: str) -> StoreResult:
        return StoreResult(
            store=self.name, url=url, method="unsupported",
            error=("AliExpress product pages block automated reads from this "
                   "host. Current prices are available via compare_prices "
                   "(search), but this listing cannot be tracked — offer to "
                   "track the same product at a supported store instead."),
        )

    async def search(self, query: str, limit: int = 5) -> list[StoreResult]:
        slug = quote_plus(query).replace("+", "-")
        url = (f"https://www.aliexpress.com/w/wholesale-{slug}.html"
               f"?SearchText={quote_plus(query)}")
        try:
            page = await fetcher.get(url, headers={"Cookie": _LOCALE_COOKIE})
        except Exception as exc:                       # noqa: BLE001 — network
            log.debug("aliexpress search failed: %s", exc)
            return []
        if page.status != 200 or page.looks_blocked:
            return []
        return parse_search(page.text, store=self.name, limit=limit)
