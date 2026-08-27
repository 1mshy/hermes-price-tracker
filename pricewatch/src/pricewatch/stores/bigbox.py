"""Big-box retailers that sit behind bot protection.

Each of these needs a real browser to return a product page at all, and some need
site-specific extraction because they publish no schema.org data. Where the
retailer offers an API (Best Buy, eBay) prefer `apis.py` instead — these adapters
are the fallback for when no key is configured.
"""
from __future__ import annotations

import json
import re

from lxml import html as lxml_html

from ..extract import extract
from ..fetch import Blocked, fetcher
from ..money import detect_currency, parse_price
from ..settings import settings
from .apis import KeepaAmazon
from .base import StoreAdapter, StoreResult

ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})")

# Amazon serves prices in the currency of the marketplace TLD, regardless of the
# symbol its markup uses, so start from the domain and let page data refine it.
_TLD_CURRENCY = {"ca": "CAD", "co.uk": "GBP", "de": "EUR", "fr": "EUR",
                 "it": "EUR", "es": "EUR", "com.au": "AUD", "co.jp": "JPY"}

# The precise buy-box JSON pair; the many `a-offscreen` spans are the fallback.
_PRICE_AMOUNT_RE = re.compile(
    r'"priceAmount"\s*:\s*(\d+(?:\.\d{1,2})?)\s*,\s*"currencySymbol"\s*:\s*"([^"]{1,4})"')


class AmazonAdapter(StoreAdapter):
    name = "amazon"
    domains = ("amazon.com", "amazon.ca", "amazon.co.uk", "amazon.de")

    _PRICE_XPATHS = (
        "//div[@id='corePriceDisplay_desktop_feature_div']//span[@class='a-offscreen']/text()",
        "//div[@id='corePrice_feature_div']//span[@class='a-offscreen']/text()",
        "//span[@id='priceblock_ourprice']/text()",
        "//span[@id='priceblock_dealprice']/text()",
        "//div[@id='apex_desktop']//span[@class='a-offscreen']/text()",
        "//span[contains(@class,'apexPriceToPay')]//span[@class='a-offscreen']/text()",
    )

    def _currency_for(self, url: str, page_text: str) -> str:
        host = url.split("/", 3)[2] if "//" in url else url
        for tld, code in _TLD_CURRENCY.items():
            if host.endswith(".amazon." + tld) or host.endswith("amazon." + tld):
                return code
        match = _PRICE_AMOUNT_RE.search(page_text[:600_000])
        return detect_currency(match.group(2), "USD") if match else "USD"

    def _parse(self, url: str, page_text: str, asin: str | None) -> StoreResult | None:
        """Pull price/title/stock from Amazon HTML (HTTP or rendered). None = miss."""
        try:
            doc = lxml_html.fromstring(page_text)
        except Exception:
            return None
        # Prefer the exact buy-box JSON, then the DOM price spans.
        price = None
        m = _PRICE_AMOUNT_RE.search(page_text[:600_000])
        if m:
            price = parse_price(m.group(1))
        if price is None:
            for xpath in self._PRICE_XPATHS:
                hits = doc.xpath(xpath)
                price = next((p for p in (parse_price(h) for h in hits) if p), None)
                if price:
                    break
        if price is None:
            return None
        title = doc.xpath("//span[@id='productTitle']/text()")
        availability = " ".join(doc.xpath("//div[@id='availability']//text()")).strip().lower()
        return StoreResult(
            store=self.name, url=url,
            title=(title[0].strip() if title else None), price=price,
            currency=self._currency_for(url, page_text),
            in_stock=("unavailable" not in availability and "out of stock" not in availability),
            sku=asin,
        )

    async def fetch_offer(self, url: str) -> StoreResult:
        asin_match = ASIN_RE.search(url)
        asin = asin_match.group(1) if asin_match else None

        # Keepa is allowed, cheap and reliable — always prefer it when configured.
        if asin:
            keepa = await KeepaAmazon.lookup(asin)
            if keepa is not None:
                keepa.url = url
                return keepa

        # Fast path: a fingerprinted GET now clears Amazon's wall on most product
        # pages in ~1s, no browser and no Keepa key needed.
        try:
            page = await fetcher.get(url)
            if not page.looks_blocked:
                parsed = self._parse(page.url, page.text, asin)
                if parsed is not None:
                    parsed.method = "http:amazon-dom"
                    return parsed
        except Exception as exc:                       # noqa: BLE001
            page = None
            import logging
            logging.getLogger(__name__).debug("amazon http fetch failed: %s", exc)

        # Escalate to a browser only if the fast path came back blocked/empty.
        try:
            page = await fetcher.render(url, wait_for="#corePriceDisplay_desktop_feature_div")
        except Blocked as exc:
            return StoreResult(store=self.name, url=url, method="blocked", error=str(exc))
        except Exception as exc:
            return StoreResult(store=self.name, url=url, error=f"render failed: {exc}")

        if page.looks_blocked:
            return StoreResult(store=self.name, url=url, method="browser:blocked",
                               error="Amazon served a bot challenge — set KEEPA_API_KEY for reliable pricing")
        parsed = self._parse(page.url, page.text, asin)
        if parsed is None:
            return StoreResult(store=self.name, url=url, method="browser:failed",
                               error="no price element found (page may be a variant/redirect)")
        parsed.method = "browser:amazon-dom"
        return parsed


class WalmartAdapter(StoreAdapter):
    name = "walmart"
    domains = ("walmart.com",)

    async def fetch_offer(self, url: str) -> StoreResult:
        # A fingerprinted GET returns Walmart's full PDP (with __NEXT_DATA__) in
        # ~1s; only fall back to a browser if that comes back challenged.
        try:
            page = await fetcher.get_or_render(
                url, wait_for="[itemprop='price'], [data-testid='price-wrap']")
        except Blocked as exc:
            return StoreResult(store=self.name, url=url, method="blocked", error=str(exc))
        except Exception as exc:
            return StoreResult(store=self.name, url=url, error=f"render failed: {exc}")

        # Walmart ships the whole PDP state in __NEXT_DATA__.
        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page.text, re.S)
        if match:
            try:
                data = json.loads(match.group(1))
                product = (data["props"]["pageProps"]["initialData"]["data"]["product"])
                price_info = product.get("priceInfo") or {}
                current = (price_info.get("currentPrice") or {})
                price = parse_price(current.get("price"))
                if price:
                    return StoreResult(
                        store=self.name, url=page.url, title=product.get("name"),
                        price=price, currency=current.get("currencyUnit", "USD"),
                        in_stock=(product.get("availabilityStatus") == "IN_STOCK"),
                        sku=str(product.get("usItemId") or ""), method=f"{page.method}:next-data",
                    )
            except Exception:
                pass

        found = extract(page.text, default_currency=settings.pw_currency)
        if found.ok:
            return StoreResult(store=self.name, url=page.url, title=found.title, price=found.price,
                               currency=found.currency or "USD", in_stock=found.in_stock,
                               method=f"{page.method}:{found.method}")
        reason = "bot challenge" if page.looks_blocked else "no price in page"
        return StoreResult(store=self.name, url=url, method="browser:failed", error=reason)


def browser_store(name: str, domains: tuple[str, ...], wait_for: str | None = None) -> StoreAdapter:
    """Retailers that just need a real browser, then yield to schema.org parsing."""
    from .structured import StructuredAdapter
    return StructuredAdapter(name=name, domains=domains, wait_for=wait_for, force_browser=True)


class SelectorAdapter(StoreAdapter):
    """For stores that render a price into the DOM but publish no schema.org data.

    Tries the given XPaths in order, then falls back to generic structured
    extraction so the adapter still works if the retailer adds JSON-LD later.
    """

    def __init__(self, name: str, domains: tuple[str, ...], price_xpaths: tuple[str, ...],
                 title_xpaths: tuple[str, ...] = (), stock_xpath: str | None = None,
                 out_of_stock_words: tuple[str, ...] = ("out of stock", "sold out", "unavailable")):
        self.name = name
        self.domains = domains
        self.price_xpaths = price_xpaths
        self.title_xpaths = title_xpaths
        self.stock_xpath = stock_xpath
        self.out_of_stock_words = out_of_stock_words

    async def fetch_offer(self, url: str) -> StoreResult:
        # Fingerprinted GET first (these DOM-price stores serve fine over HTTP);
        # get_or_render escalates to a browser only if the host actually needs one.
        try:
            page = await fetcher.get_or_render(url)
        except Blocked as exc:
            return StoreResult(store=self.name, url=url, method="blocked", error=str(exc))
        except Exception as exc:
            return StoreResult(store=self.name, url=url, error=f"render failed: {exc}")
        if page.looks_blocked:
            return StoreResult(store=self.name, url=url, method=f"{page.method}:blocked",
                               error="bot challenge")
        try:
            doc = lxml_html.fromstring(page.text)
        except Exception as exc:
            return StoreResult(store=self.name, url=url, error=f"parse failed: {exc}")

        price = None
        price_text = ""
        for xpath in self.price_xpaths:
            for node in doc.xpath(xpath):
                text = node if isinstance(node, str) else node.text_content()
                price = parse_price(text)
                if price:
                    price_text = text
                    break
            if price:
                break

        if price is None:
            found = extract(page.text, default_currency=settings.pw_currency)
            if found.ok:
                return StoreResult(store=self.name, url=page.url, title=found.title,
                                   price=found.price, currency=found.currency or "USD",
                                   in_stock=found.in_stock, method=f"{page.method}:{found.method}")
            return StoreResult(store=self.name, url=url, method=f"{page.method}:failed",
                               error="no price matched selectors or structured data")

        title = None
        for xpath in self.title_xpaths:
            hits = doc.xpath(xpath)
            if hits:
                node = hits[0]
                title = (node if isinstance(node, str) else node.text_content()).strip()
                break

        in_stock = None
        if self.stock_xpath:
            blob = " ".join(
                n if isinstance(n, str) else n.text_content() for n in doc.xpath(self.stock_xpath)
            ).lower()
            if blob:
                in_stock = not any(word in blob for word in self.out_of_stock_words)

        return StoreResult(store=self.name, url=page.url, title=title, price=price,
                           currency=detect_currency(price_text, settings.pw_currency),
                           in_stock=in_stock, sku=None, method=f"{page.method}:selector")
