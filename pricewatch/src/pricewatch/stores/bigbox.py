"""Big-box retailers that sit behind bot protection.

Each of these needs a real browser to return a product page at all, and some need
site-specific extraction because they publish no schema.org data. Where the
retailer offers an API (Best Buy, eBay) prefer `apis.py` instead — these adapters
are the fallback for when no key is configured.
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote_plus

from lxml import html as lxml_html

from .. import matching, preferences
from ..extract import extract
from ..fetch import Blocked, fetcher
from ..money import (currency_for_host, currency_from_text, detect_currency, parse_price,
                     region_for_host)
from ..settings import settings
from .apis import KeepaAmazon
from .base import StoreAdapter, StoreResult, host_of

ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})")

# The regional marketplaces bill in one currency each, whatever symbol their
# markup uses, so the TLD settles it. amazon.com is deliberately absent: it
# shows a visitor abroad the USD price converted into their own money, so
# there the label beside the figure is the only honest source.
_TLD_CURRENCY = {"ca": "CAD", "co.uk": "GBP", "de": "EUR", "fr": "EUR",
                 "it": "EUR", "es": "EUR", "com.au": "AUD", "co.jp": "JPY"}

# The precise buy-box JSON pair; the many `a-offscreen` spans are the fallback.
_PRICE_AMOUNT_RE = re.compile(
    r'"priceAmount"\s*:\s*(\d+(?:\.\d{1,2})?)\s*,\s*"currencySymbol"\s*:\s*"([^"]{1,4})"')


# Paid placements announce themselves inside the title itself; left in place
# the prefix drags every sponsored card's similarity score around.
_SPONSORED_RE = re.compile(r"^\s*sponsored ad\s*[-\u2013\u2014:]\s*", re.I)

# A search for a product returns the product padded out with things that merely
# fit it — "Case for X", "Mouse Feet Compatible with X". Those titles contain
# every query token, so similarity alone scores them level with the real thing
# and a top-3 cut comes back as three accessories. Demoted, not dropped: the
# query may itself be asking for one.
_ACCESSORY_RE = re.compile(
    r"\b(compatible with|replacements? for|for use with|screen protector|"
    r"carrying case|travel case|dust cover|mouse feet|grip tape|decal|sticker)\b", re.I)

# "Renewed"/"Refurbished" units undercut the new price by enough to look like a
# deal that is not one, so mark them rather than let them pass as a buy box.
# A bare "used" is deliberately absent: it shows up in ordinary copy ("used by
# professionals"), and this flag now decides what gets tracked, so a false
# positive costs a real listing.
_CONDITION_RE = re.compile(r"\b(renewed|refurbished|pre-?owned|open box)\b", re.I)


def _looks_like_accessory(title: str, query_tokens: set[str]) -> bool:
    """Is this something *for* the product asked about, rather than the product?

    A keyword list never keeps up with how many ways Amazon phrases this, but
    the position of the query's own words gives it away: "Grip Tape **for
    Logitech MX Master 3S**" carries them in its tail, while "Logitech MX
    Master 3S **for Business**" is the product itself with a qualifier.
    """
    if _ACCESSORY_RE.search(title):
        return True
    parts = re.split(r"\bfor\b", title, maxsplit=1, flags=re.I)
    if len(parts) != 2 or not query_tokens:
        return False
    head = set(matching.normalise(parts[0]).split())
    tail = set(matching.normalise(parts[1]).split())
    return len(query_tokens & tail) > len(query_tokens & head)


def parse_amazon_search(raw: str, storefront: str = "amazon.com",
                        currency: str = "USD", limit: int = 60) -> list[StoreResult]:
    """Listings from an Amazon `/s?k=` results page.

    Each card already carries everything a comparison needs — ASIN, full title,
    buy-box price, list price — so no per-product fetch is required to shortlist.
    The full title is only on the h2's `aria-label`: the h2's own spans start
    with a separate brand node, so reading its text yields just "Logitech".
    """
    try:
        doc = lxml_html.fromstring(raw)
    except Exception:                                  # noqa: BLE001 — bad markup
        return []
    results: list[StoreResult] = []
    seen: set[str] = set()
    for card in doc.xpath("//div[@data-component-type='s-search-result']"):
        asin = (card.get("data-asin") or "").strip()
        if not asin or asin in seen:
            continue
        title = next((t.strip() for t in card.xpath(".//h2/@aria-label") if t.strip()), None)
        if not title:
            title = next((t.strip() for t in
                          card.xpath(".//a[contains(@class,'s-line-clamp')]//span/text()")
                          if t.strip()), None)
        # Exact class match: the strikethrough list price is `a-price a-text-price`.
        price_text = next((t for t in
                           card.xpath(".//span[@class='a-price']/span[@class='a-offscreen']/text()")
                           if t.strip()), None)
        price = parse_price(price_text) if price_text else None
        if not title or price is None:
            continue                                   # accessory strips, ad slots
        sponsored = bool(_SPONSORED_RE.match(title))
        title = _SPONSORED_RE.sub("", title)
        extra: dict = {"note": "search-result price — Amazon lists the cheapest "
                               "variant, so confirm with get_price on the URL "
                               "before quoting it as final"}
        if sponsored:
            extra["sponsored"] = True
        was = next((t for t in card.xpath(".//span[@data-a-strike='true']//text()")
                    if parse_price(t) is not None), None)
        if was:
            extra["list_price"] = str(parse_price(was))
        condition = _CONDITION_RE.search(title)
        if condition:
            extra["condition"] = condition.group(1).lower()
        rating = card.xpath(".//span[@class='a-icon-alt']/text()")
        if rating:
            extra["rating"] = rating[0].strip()
        seen.add(asin)
        results.append(StoreResult(
            store="amazon", url=f"https://www.{storefront}/dp/{asin}",
            title=title, price=price, currency=currency, in_stock=True,
            sku=asin, method="http:amazon-search", extra=extra))
        if len(results) >= limit:
            break
    return results


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

    def _currency_for(self, url: str, price_text: str) -> str:
        """Which money the buy-box figure is in.

        Regional marketplaces: the TLD. amazon.com: the label on the figure
        itself, because from a Canadian address the same listing reads
        "CAD 138.83" on one fetch and "$99.99" on the next — a converted
        display, not a price change. Reading the label keeps both honest.
        """
        host = host_of(url)
        for tld, code in _TLD_CURRENCY.items():
            if host == "amazon." + tld or host.endswith(".amazon." + tld):
                return code
        return detect_currency(price_text, "USD")

    def _parse(self, url: str, page_text: str, asin: str | None) -> StoreResult | None:
        """Pull price/title/stock from Amazon HTML (HTTP or rendered). None = miss."""
        try:
            doc = lxml_html.fromstring(page_text)
        except Exception:
            return None
        # Prefer the exact buy-box JSON, then the DOM price spans. Keep the
        # text the figure came with: on .com it is the only currency evidence.
        price = None
        price_text = ""
        m = _PRICE_AMOUNT_RE.search(page_text[:600_000])
        if m:
            price = parse_price(m.group(1))
            price_text = m.group(2)
        if price is None:
            for xpath in self._PRICE_XPATHS:
                for hit in doc.xpath(xpath):
                    price = parse_price(hit)
                    if price:
                        price_text = hit
                        break
                if price:
                    break
        if price is None:
            return None
        title = doc.xpath("//span[@id='productTitle']/text()")
        availability = " ".join(doc.xpath("//div[@id='availability']//text()")).strip().lower()
        host = host_of(url)
        currency = self._currency_for(url, price_text)
        native = currency_for_host(host, "USD")
        extra: dict = {}
        if currency != native:
            extra["note"] = (
                f"{host} showed this price in {currency} because the visitor is abroad; "
                f"the listing bills in {native}, and the converted figure changes from one "
                f"read to the next — track it on the regional storefront instead")
        return StoreResult(
            store=self.name, url=url,
            title=(title[0].strip() if title else None), price=price,
            currency=currency,
            in_stock=("unavailable" not in availability and "out of stock" not in availability),
            sku=asin, extra=extra,
        )

    def regional_url(self, url: str) -> str | None:
        """amazon.com/dp/ASIN → amazon.ca/dp/ASIN for a Canadian shopper.

        ASINs are shared across marketplaces, so this is a hostname swap;
        whether the listing exists there is for the fetch to find out. Only
        for a country Amazon runs a marketplace in — a euro-zone shopper keeps
        whatever link they pasted.
        """
        region = preferences.preferred_region()
        if not region:
            return None
        target = self.storefront()
        if region_for_host(target) != region:
            return None
        asin = ASIN_RE.search(url)
        if not asin or host_of(url) == target:
            return None
        return f"https://www.{target}/dp/{asin.group(1)}"

    async def fetch_offer(self, url: str) -> StoreResult:
        asin_match = ASIN_RE.search(url)
        asin = asin_match.group(1) if asin_match else None

        # Keepa is allowed, cheap and reliable — always prefer it when configured.
        if asin:
            keepa = await KeepaAmazon.lookup(asin, host=host_of(url))
            if keepa is not None:
                keepa.url = url
                return keepa

        # Fast path: a fingerprinted GET now clears Amazon's wall on most product
        # pages in ~1s, no browser and no Keepa key needed.
        try:
            page = await fetcher.get(url)
            # ASINs get retired constantly. A 404 is a settled answer, not a
            # wall, so say so instead of paying for a browser that will only
            # fetch the same "Page Not Found".
            if page.status == 404:
                return StoreResult(store=self.name, url=url, method="http:not-found",
                                   error="no longer listed on Amazon (404) — the "
                                         "ASIN was retired or the URL is wrong")
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

    async def search(self, query: str, limit: int = 5) -> list[StoreResult]:
        """Keyless search over the public results page.

        Amazon publishes no free product API — Keepa answers by ASIN, not by
        keyword — so without this the store could only ever be *read* from a
        URL the user already had, never *found* from a description. The results
        page is server-rendered HTML that a fingerprinted GET returns in about
        a second, which makes the whole catalogue searchable with no key.
        """
        host = self.storefront() or self.domains[0]
        url = f"https://www.{host}/s?k={quote_plus(query)}"
        try:
            page = await fetcher.get(url)
        except Exception:                              # noqa: BLE001 — network
            return []
        if page.status != 200 or page.looks_blocked:
            return []
        found = parse_amazon_search(page.text, storefront=host,
                                    currency=self._currency_for(url, page.text))
        # Amazon orders the page by its own interests: the first cards are paid
        # placements and loose category matches. Rank against the query before
        # truncating, or a limit of 3 returns three ads and drops the product
        # that was actually asked for.
        ranked = matching.rank(query, found, key=lambda r: r.title, threshold=0.0)
        # Unless the user is shopping for an accessory themselves, in which case
        # the same shape of title is exactly what they asked for.
        if not _ACCESSORY_RE.search(query) and not re.search(r"\bfor\b", query, re.I):
            tokens = set(matching.normalise(query).split())
            # Stable, so relevance order survives inside each group.
            ranked.sort(key=lambda r: _looks_like_accessory(r.title or "", tokens))
        return ranked[:limit]


class WalmartAdapter(StoreAdapter):
    name = "walmart"
    domains = ("walmart.com", "walmart.ca")

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

        # walmart.ca is the same Next.js PDP billing CAD; when the payload
        # omits the currency the domain is the answer, not USD.
        host = host_of(page.url)
        native = currency_for_host(host, settings.pw_currency)
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
                        price=price, currency=current.get("currencyUnit") or native,
                        in_stock=(product.get("availabilityStatus") == "IN_STOCK"),
                        sku=str(product.get("usItemId") or ""), method=f"{page.method}:next-data",
                    )
            except Exception:
                pass

        found = extract(page.text, default_currency=native, host=host)
        if found.ok:
            return StoreResult(store=self.name, url=page.url, title=found.title, price=found.price,
                               currency=found.currency or native, in_stock=found.in_stock,
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

        # A bare "$" on newegg.ca is CAD: the hostname decides what the
        # glyph means, both for the selector hit and the structured fallback —
        # and a price node with no glyph at all (themes often put the "$" in
        # a sibling span) is the host's money too, not the global default.
        host = host_of(page.url)
        native = currency_for_host(host, settings.pw_currency)
        if price is None:
            found = extract(page.text, default_currency=native, host=host)
            if found.ok:
                return StoreResult(store=self.name, url=page.url, title=found.title,
                                   price=found.price, currency=found.currency or native,
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
                           currency=currency_from_text(price_text, host, native),
                           in_stock=in_stock, sku=None, method=f"{page.method}:selector")
