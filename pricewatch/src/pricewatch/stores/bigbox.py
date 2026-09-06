"""Big-box retailers that sit behind bot protection.

Each of these needs a real browser to return a product page at all, and some need
site-specific extraction because they publish no schema.org data. Where the
retailer offers an API (Best Buy, eBay) prefer `apis.py` instead — these adapters
are the fallback for when no key is configured.
"""
from __future__ import annotations

import json
import re
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import quote_plus

from lxml import html as lxml_html

from .. import matching, preferences
from ..extract import extract
from ..fetch import Blocked, fetcher
from ..money import (currency_for_host, currency_from_text, detect_currency, fmt, parse_price,
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

# Amazon's clip coupons live *beside* the buy box, not in it: the sticker
# price stays put and a checkbox takes the money off at checkout. A watch that
# only read the sticker sat through a CA$167.73 -> CA$49.99 coupon on a tracked
# listing (2026-09-02) without a flicker, while Reddit was full of it. These
# are the widgets the coupon renders into, on the desktop and the mobile page.
_COUPON_REGION_XPATHS = (
    "//div[@id='promoPriceBlockMessage_feature_div']",
    "//div[@id='couponsInBuybox_feature_div']",
    "//div[@id='vpcButton']",
    "//*[starts-with(@id,'couponText')]",
    "//*[starts-with(@id,'couponBadge')]",
    "//*[contains(@class,'couponLabelText')]",
    "//*[contains(@class,'promoPriceBlockMessage')]",
)
# The label wording varies ("Apply $40 coupon", "Save 20% with coupon",
# "$40 off coupon", French "coupon de 40 $"), so rather than enumerate
# phrasings, take the figure nearest the word "coupon" in the widget text.
_COUPON_WORD_RE = re.compile(r"coupon", re.I)
_COUPON_PCT_RE = re.compile(r"(\d{1,2}(?:[.,]\d)?)\s?%")
_COUPON_MONEY_RE = re.compile(
    r"(?:(?:CA|US|AU)?\$|€|£|CAD|USD|EUR|GBP)\s?(\d[\d,]*(?:\.\d{1,2})?)"
    r"|(\d[\d,]*(?:[.,]\d{1,2})?)\s?(?:\$|€|£)")
_COUPON_WINDOW = 70
_NOT_A_COUPON_RE = re.compile(r"subscribe|abonn", re.I)

# Time-boxed markdowns are already in the buy-box price; the badge is what
# says the figure will not last. Amazon ships the countdown variants as
# templates ("Limited time deal NO_OF_HOURS hours") beside the rendered one,
# so a fragment counts only when it is the bare phrase.
_DEAL_BADGE_RE = re.compile(
    r"^(limited[- ]time deal|lightning deal|deal of the day|today'?s deal|"
    r"prime (?:day|big deal days?) deal|black friday deal|cyber monday deal|"
    r"offre (?:à durée )?limitée)\W*$", re.I)
_DEAL_REGION_XPATHS = (
    "//div[@id='dealBadge_feature_div']",
    "//div[@id='corePriceDisplay_desktop_feature_div']",
    "//div[@id='corePrice_feature_div']",
)


def _region_text(doc, xpaths: tuple[str, ...]) -> list[str]:
    """Visible text fragments of the given regions, scripts and styles dropped.

    Amazon's page bundle declares its own UI strings in `a-state` script
    blocks ("{number} off coupon", "Lightning Deal"), so text_content() on
    a raw region would find a coupon on every page.
    """
    fragments: list[str] = []
    for xpath in xpaths:
        for region in doc.xpath(xpath):
            for junk in region.xpath(".//script|.//style|.//template"):
                junk.drop_tree()
            fragments.extend(t.strip() for t in region.itertext() if t and t.strip())
    return fragments


def find_coupon(doc, price: Decimal) -> dict | None:
    """The clip coupon on a product page, if any, as
    {"label", "effective", "amount" | "percent"}."""
    return coupon_in_text(" ".join(_region_text(doc, _COUPON_REGION_XPATHS)), price)


def coupon_in_text(text: str, price: Decimal | None) -> dict | None:
    """The coupon a run of visible text describes, against the sticker price.

    The figure closest to the word "coupon" wins, so "Save 5% with
    Subscribe & Save" further along the same row cannot pose as one. A
    money-off coupon larger than the price, or a percentage outside (0, 100),
    is a misread and is ignored rather than reported as a free product.
    """
    if not text or price is None:
        return None
    best: tuple[int, str, Decimal] | None = None          # (distance, kind, value)

    def consider(m, kind: str, value: Decimal | None, window: str, anchor: int) -> None:
        nonlocal best
        if value is None:
            return
        # "Save 5% with Subscribe & Save" is a subscription, whatever sits
        # next to it: the words between the figure and "coupon", and the
        # few right after the figure, tell.
        between = window[min(m.end(), anchor):max(m.start(), anchor)]
        if _NOT_A_COUPON_RE.search(between) or _NOT_A_COUPON_RE.search(window[m.start():m.end() + 40]):
            return
        distance = min(abs(m.start() - anchor), abs(m.end() - anchor))
        if best is None or distance < best[0]:
            best = (distance, kind, value)

    for word in _COUPON_WORD_RE.finditer(text):
        start = max(0, word.start() - _COUPON_WINDOW)
        window = text[start:word.end() + _COUPON_WINDOW]
        anchor = word.start() - start
        for m in _COUPON_PCT_RE.finditer(window):
            pct = parse_price(m.group(1).replace(",", "."))
            consider(m, "percent", pct if pct is not None and pct < 100 else None, window, anchor)
        for m in _COUPON_MONEY_RE.finditer(window):
            amount = parse_price(m.group(1) or m.group(2))
            consider(m, "amount", amount if amount is not None and amount < price else None,
                     window, anchor)
    if best is None:
        return None
    _, kind, value = best
    cent = Decimal("0.01")
    if kind == "percent":
        effective = (price * (Decimal(1) - value / 100)).quantize(cent, ROUND_HALF_UP)
        return {"percent": float(value), "label": f"{value.normalize():f}% off",
                "effective": effective}
    effective = (price - value).quantize(cent, ROUND_HALF_UP)
    return {"amount": str(value), "label": f"{value} off", "effective": effective}


def find_deal_badge(doc) -> str | None:
    """\"Limited time deal\" and its relatives, when rendered on the page."""
    for fragment in _region_text(doc, _DEAL_REGION_XPATHS):
        if "NO_OF_" in fragment:
            continue                                   # countdown template
        m = _DEAL_BADGE_RE.match(fragment)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip().capitalize()
    return None


def find_list_price(doc, price: Decimal) -> Decimal | None:
    """The struck-through \"Was:\" / \"List Price:\" figure beside the buy box."""
    for xpath in ("//div[@id='corePriceDisplay_desktop_feature_div']"
                  "//span[@data-a-strike='true']//span[@class='a-offscreen']/text()",
                  "//div[@id='corePrice_feature_div']"
                  "//span[@data-a-strike='true']//span[@class='a-offscreen']/text()"):
        for hit in doc.xpath(xpath):
            was = parse_price(hit)
            if was is not None and was > price:
                return was
    return None


def deal_note(price: Decimal, currency: str, coupon: dict | None, badge: str | None,
              list_price: Decimal | None) -> str | None:
    """One line for the alert and the tracker listing: what the figure rests on."""
    if coupon:
        return (f"{fmt(coupon['effective'], currency)} after the on-page coupon "
                f"({coupon['label']}); sticker price {fmt(price, currency)} — clip the "
                f"coupon on the product page before checkout. Amazon coupons are "
                f"time-boxed and can be account-specific.")
    if badge:
        if list_price:
            pct = (Decimal(1) - price / list_price) * 100
            return (f"{badge}: {fmt(price, currency)} is {pct:.0f}% below the "
                    f"{fmt(list_price, currency)} list price — time-boxed.")
        return f"{badge}: {fmt(price, currency)} is a time-boxed markdown."
    return None


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
        # Cards carry the coupon as text ("Save $40.00 with coupon"); the price
        # above is the sticker. get_price on the URL reports the price after it.
        card_text = " ".join(t.strip() for t in card.itertext() if t.strip())
        if _COUPON_WORD_RE.search(card_text):
            coupon = coupon_in_text(card_text, price)
            if coupon:
                extra["coupon"] = coupon["label"]
                extra["note"] += (f"; a {coupon['label']} clip coupon is shown on the card, "
                                  f"so checkout is about {coupon['effective']}")
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

    # Buy-box containers, then the price spans inside them that are not the
    # struck-through list price. On a deal page the first `a-offscreen` copy
    # is blank (the digits sit in a-price-whole/-fraction) and the next one
    # is \"Was: $349.99\" — which is what got reported as the price.
    _PRICE_CONTAINERS = (
        "//div[@id='corePriceDisplay_desktop_feature_div']",
        "//div[@id='corePrice_feature_div']",
        "//div[@id='apex_desktop']",
    )
    _PRICE_SPAN = ("//span[contains(concat(' ', normalize-space(@class), ' '), ' a-price ') "
                   "and not(@data-a-strike='true')]")
    _PRICE_XPATHS = (                                  # older page layouts
        "//span[@id='priceblock_ourprice']/text()",
        "//span[@id='priceblock_dealprice']/text()",
        "//span[@id='priceblock_saleprice']/text()",
    )

    @staticmethod
    def _span_price(span) -> tuple[Decimal | None, str]:
        """(price, text) from an `a-price` span: the a-offscreen copy when it
        is filled in, else the visible symbol/whole/fraction pieces."""
        off = " ".join(t.strip() for t in span.xpath(".//span[@class='a-offscreen']/text()")
                       if t.strip())
        if off:
            return parse_price(off), off
        symbol = "".join(span.xpath(".//span[@class='a-price-symbol']/text()")).strip()
        whole = "".join(span.xpath(".//span[@class='a-price-whole']/text()")).strip()
        fraction = "".join(span.xpath(".//span[@class='a-price-fraction']/text()")).strip()
        if not whole:
            return None, ""
        text = f"{symbol}{whole}.{fraction or '00'}"
        return parse_price(text), text

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
        # The whole page, not a prefix: on deal pages the buy-box JSON sits
        # past the 800 KB mark, behind the badge and countdown markup.
        m = _PRICE_AMOUNT_RE.search(page_text)
        if m:
            price = parse_price(m.group(1))
            price_text = m.group(2)
        if price is None:
            for container in self._PRICE_CONTAINERS:
                for span in doc.xpath(container + self._PRICE_SPAN):
                    price, price_text = self._span_price(span)
                    if price:
                        break
                if price:
                    break
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
        # What the sticker rests on: a clip coupon (the price the user actually
        # pays is lower), a deal badge (the price will not last), a list price.
        coupon = find_coupon(doc, price)
        badge = find_deal_badge(doc)
        list_price = find_list_price(doc, price)
        note = deal_note(price, currency, coupon, badge, list_price)
        if note:
            extra["deal_note"] = note
        if badge:
            extra["deal"] = badge
        if list_price:
            extra["list_price"] = str(list_price)
        if coupon:
            # The tracker measures `price`, so `price` is what checkout charges;
            # the sticker stays beside it so the alert can say both.
            extra["coupon"] = coupon["label"]
            extra["sticker_price"] = str(price)
            price = coupon["effective"]
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
