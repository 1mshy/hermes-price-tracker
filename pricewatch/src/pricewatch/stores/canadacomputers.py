"""Canada Computers — a PrestaShop storefront billing CAD.

Product pages publish schema.org JSON-LD, so `StructuredAdapter` already reads
them over plain HTTP; what needs hand-writing is search, which is parsed from
the result cards of /en/search?s=… (title, price, URL, online stock).
"""
from __future__ import annotations

import re
from urllib.parse import quote_plus, urlsplit, urlunsplit

from lxml import html as lxml_html

from ..fetch import fetcher
from ..money import parse_price
from .base import StoreResult
from .structured import StructuredAdapter

_ORIGIN = "https://www.canadacomputers.com"


class CanadaComputersAdapter(StructuredAdapter):
    def __init__(self):
        # A .com that bills CAD: tell the structured reader so a bare `$` on
        # a product page is not mistaken for USD.
        super().__init__(name="canadacomputers", domains=("canadacomputers.com",), currency="CAD")

    async def search(self, query: str, limit: int = 5) -> list[StoreResult]:
        url = f"{_ORIGIN}/en/search?s={quote_plus(query)}"
        try:
            page = await fetcher.get(url)
        except Exception:                                  # noqa: BLE001 — network
            return []
        if page.status != 200 or page.looks_blocked:
            return []
        return parse_search(page.text, store=self.name, limit=limit)


def _clean_url(link) -> str:
    """The product URL without the `?keyword=` echo the cards append."""
    href = link.get("content") or link.get("href") or ""
    parts = urlsplit(href)
    if not parts.netloc:
        href = _ORIGIN + href
        parts = urlsplit(href)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def parse_search(raw: str, store: str = "canadacomputers", limit: int = 5) -> list[StoreResult]:
    """Result cards from the search page: `article.product-miniature` each with
    a `.product-description[data-price]`, an `h2 a` and an `.available-tag`.

    Prices are printed bare ("$719.99") and the store bills only CAD, so the
    currency is fixed rather than sniffed. "$19.99 and up" is the cheapest
    variant, which the note says so a filament colour is not quoted as the
    spool's only price.
    """
    try:
        doc = lxml_html.fromstring(raw)
    except Exception:                                      # noqa: BLE001
        return []
    results: list[StoreResult] = []
    seen: set[str] = set()
    for card in doc.xpath("//article[contains(@class,'product-miniature')]"):
        links = (card.xpath(".//h2[contains(@class,'product-title')]//a[@href]")
                 or card.xpath(".//a[contains(@href,'.html')]"))
        if not links:
            continue
        url = _clean_url(links[0])
        if not url or url in seen:
            continue
        title = " ".join(links[0].text_content().split()) or None
        description = card.xpath(".//*[contains(@class,'product-description')]")
        price_text = (description[0].get("data-price") if description else "") or ""
        if not price_text:
            spans = card.xpath(".//span[contains(@class,'price')]")
            price_text = spans[0].text_content() if spans else ""
        price = parse_price(price_text)
        if price is None:
            continue
        regular = parse_price(description[0].get("data-regular_price")) if description else None
        displayed = " ".join(" ".join(
            s.text_content() for s in card.xpath(".//span[contains(@class,'price')]")).split())
        in_stock = None
        tag = card.xpath(".//*[contains(@class,'available-tag')]/@data-stock_availability_online")
        if tag:
            in_stock = tag[0].strip() == "1"
        thumb = card.xpath(".//a[contains(@class,'product-thumbnail')]/@data-id")
        sku = (thumb[0].strip() if thumb else "") or card.get("data-id-product") or None
        extra = {
            "regular_price": float(regular) if regular is not None and regular != price else None,
            "note": ("lowest variant price ('and up') — confirm the exact spool/colour "
                     "with get_price on the product URL") if re.search(r"and up", displayed, re.I) else None,
        }
        seen.add(url)
        results.append(StoreResult(store=store, url=url, title=title, price=price,
                                   currency="CAD", in_stock=in_stock, sku=sku,
                                   method="http:cc-search", extra=extra))
        if len(results) >= limit:
            break
    return results
