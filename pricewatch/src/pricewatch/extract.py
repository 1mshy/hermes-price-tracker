"""Pull price/title/stock out of a raw HTML page without site-specific selectors.

Order of preference: schema.org JSON-LD → microdata → OpenGraph/meta → embedded
state blobs. Most storefronts emit at least one of these because Google Shopping
requires it, which is why this covers far more sites than hand-written selectors.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal

from lxml import html as lxml_html

from .money import detect_currency, parse_price

_JSONLD_XPATH = "//script[@type='application/ld+json']/text()"

_IN_STOCK_WORDS = ("instock", "in_stock", "in stock", "backorder", "preorder", "limitedavailability")
_OUT_WORDS = ("outofstock", "out_of_stock", "out of stock", "soldout", "sold out", "discontinued")


@dataclass
class Extracted:
    price: Decimal | None = None
    currency: str | None = None
    title: str | None = None
    sku: str | None = None
    in_stock: bool | None = None
    method: str = "none"

    @property
    def ok(self) -> bool:
        return self.price is not None


def _iter_jsonld(doc) -> list:
    """Yield every JSON-LD node, recursively, so nested @graph / hasVariant /
    isVariantOf structures are reachable. Modern Shopify themes emit a
    ProductGroup whose real prices only live inside hasVariant[].offers."""
    nodes: list = []
    for blob in doc.xpath(_JSONLD_XPATH):
        text = (blob or "").strip()
        if not text:
            continue
        # Some stores emit trailing commas / JS comments that break strict JSON.
        data = None
        for candidate in (text, re.sub(r",\s*([}\]])", r"\1", text)):
            try:
                data = json.loads(candidate)
                break
            except Exception:
                continue
        if data is None:
            continue

        def walk(node, depth: int = 0) -> None:
            if depth > 12:
                return
            if isinstance(node, list):
                for item in node:
                    walk(item, depth + 1)
            elif isinstance(node, dict):
                nodes.append(node)
                for value in node.values():
                    if isinstance(value, (dict, list)):
                        walk(value, depth + 1)

        walk(data)
    return nodes


def _types(node: dict) -> set[str]:
    raw = node.get("@type") or node.get("type") or ""
    values = raw if isinstance(raw, list) else [raw]
    return {str(v).split("/")[-1].lower() for v in values}


def _availability(value) -> bool | None:
    if value is None:
        return None
    text = str(value).lower()
    if any(word in text for word in _OUT_WORDS):
        return False
    if any(word in text for word in _IN_STOCK_WORDS):
        return True
    return None


def _from_offer_node(offer: dict) -> tuple[Decimal | None, str | None, bool | None]:
    if not isinstance(offer, dict):
        return None, None, None
    price = parse_price(offer.get("price") or offer.get("lowPrice") or offer.get("highPrice"))
    spec = offer.get("priceSpecification")
    if price is None and isinstance(spec, dict):
        price = parse_price(spec.get("price"))
    if price is None and isinstance(spec, list) and spec:
        price = parse_price(spec[0].get("price") if isinstance(spec[0], dict) else None)
    currency = offer.get("priceCurrency")
    if not currency and isinstance(spec, dict):
        currency = spec.get("priceCurrency")
    return price, (currency or None), _availability(offer.get("availability"))


def from_jsonld(doc) -> Extracted:
    """Prefer the cheapest in-stock offer attached to a Product/ProductGroup."""
    best: tuple[Decimal, str | None, bool | None] | None = None
    title = sku = None

    for node in _iter_jsonld(doc):
        kinds = _types(node)
        if not (kinds & {"product", "productgroup", "offer", "aggregateoffer"}):
            continue

        if kinds & {"product", "productgroup"}:
            if title is None and isinstance(node.get("name"), str):
                title = node["name"]
            if sku is None:
                sku = node.get("sku") or node.get("mpn") or node.get("gtin13")

        offers = node.get("offers")
        if offers is None and kinds & {"offer", "aggregateoffer"}:
            offers = node
        candidates = offers if isinstance(offers, list) else [offers] if offers else []

        for offer in candidates:
            price, currency, stock = _from_offer_node(offer)
            if price is None:
                continue
            # An in-stock offer always beats an out-of-stock one; then cheapest wins.
            if best is None:
                best = (price, currency, stock)
                continue
            better_stock = (stock is not False) and (best[2] is False)
            same_stock = (stock is not False) == (best[2] is not False)
            if better_stock or (same_stock and price < best[0]):
                best = (price, currency, stock)

    if best is None:
        return Extracted()
    return Extracted(
        price=best[0],
        currency=(best[1] or "").upper() or None,
        title=title,
        sku=str(sku) if sku else None,
        in_stock=best[2],
        method="json-ld",
    )


def from_microdata(doc) -> Extracted:
    nodes = doc.xpath("//*[@itemprop='price' or @itemprop='lowPrice']")
    for node in nodes:
        price = parse_price(node.get("content") or node.text_content())
        if price is None:
            continue
        cur_node = doc.xpath("//*[@itemprop='priceCurrency']/@content")
        avail = doc.xpath("//*[@itemprop='availability']/@href | //*[@itemprop='availability']/@content")
        title = doc.xpath("//*[@itemprop='name']/text()")
        return Extracted(
            price=price,
            currency=(cur_node[0].upper() if cur_node else None),
            title=title[0].strip() if title else None,
            in_stock=_availability(avail[0] if avail else None),
            method="microdata",
        )
    return Extracted()


def from_meta(doc) -> Extracted:
    def meta(*names: str) -> str | None:
        for name in names:
            hit = doc.xpath(
                f"//meta[@property='{name}' or @name='{name}' or @itemprop='{name}']/@content"
            )
            if hit and hit[0].strip():
                return hit[0].strip()
        return None

    price = parse_price(meta("product:price:amount", "og:price:amount", "twitter:data1", "price"))
    if price is None:
        return Extracted()
    currency = meta("product:price:currency", "og:price:currency")
    availability = meta("product:availability", "og:availability")
    return Extracted(
        price=price,
        currency=(currency or "").upper() or None,
        title=meta("og:title", "twitter:title"),
        in_stock=_availability(availability),
        method="meta",
    )


# Storefront SPAs stash the real price in a JSON blob; grab the most plausible one.
_EMBEDDED_PRICE_RE = re.compile(
    r'"(?:current_?price|price_?amount|salePrice|currentPrice|finalPrice|priceValue)"\s*:\s*'
    r'(?:{[^{}]*?"(?:amount|value|price)"\s*:\s*)?"?(\d+(?:[.,]\d{1,2})?)"?'
)


def from_embedded_json(page_text: str) -> Extracted:
    matches = _EMBEDDED_PRICE_RE.findall(page_text[:1_500_000])
    prices = [p for p in (parse_price(m) for m in matches) if p is not None]
    if not prices:
        return Extracted()
    # Multiple hits usually means list-price + sale-price + variants; take the mode-ish min.
    return Extracted(price=min(prices), method="embedded-json")


# Amazon (and a few other CDNs) ship the buy-box price as a precise JSON pair
# rather than schema.org data. It appears once, at the real price, so it is far
# safer than scraping the many `a-offscreen` spans a PDP carries.
_PRICE_AMOUNT_RE = re.compile(
    r'"priceAmount"\s*:\s*(\d+(?:\.\d{1,2})?)\s*,\s*"currencySymbol"\s*:\s*"([^"]{1,4})"'
)


def from_price_amount(page_text: str) -> Extracted:
    match = _PRICE_AMOUNT_RE.search(page_text[:1_500_000])
    if not match:
        return Extracted()
    price = parse_price(match.group(1))
    if price is None:
        return Extracted()
    return Extracted(price=price, currency=detect_currency(match.group(2), "") or None,
                     method="price-amount")


def extract(page_text: str, *, default_currency: str = "USD") -> Extracted:
    try:
        doc = lxml_html.fromstring(page_text)
    except Exception:
        return Extracted()

    for reader in (from_jsonld, from_microdata, from_meta):
        found = reader(doc)
        if found.ok:
            found.currency = found.currency or detect_currency(page_text[:4000], default_currency)
            if found.title:
                found.title = re.sub(r"\s+", " ", found.title).strip()[:400]
            return found

    for reader in (from_price_amount, from_embedded_json):
        found = reader(page_text)
        if found.ok:
            titles = doc.xpath("//title//text()") or doc.xpath("//h1//text()")
            found.title = re.sub(r"\s+", " ", " ".join(titles)).strip()[:400] or None
            found.currency = found.currency or detect_currency(page_text[:4000], default_currency)
            return found
    return found
