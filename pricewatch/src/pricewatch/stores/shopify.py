"""Shopify storefronts.

Shopify exposes an unauthenticated JSON view of every product:
  /products/<handle>.js            → single product, price in integer cents
  /products/<handle>.json          → same product, prices as decimal strings
  /search/suggest.json?q=…         → typeahead search with prices
  /cart.js                         → the market's own currency code
This is a documented storefront endpoint, so it is both far more reliable and far
lighter on the store than scraping rendered HTML. A large share of the 3D-printing
market (Bambu Lab, Elegoo, Creality, Anycubic, E3D, Micro Swiss, Printed Solid,
Polymaker, Slice, Proto-pasta, West3D, Fabreeko…) runs on Shopify.

Shopify Markets is the wrinkle. One storefront serves several markets behind
locale path prefixes, each with its own currency *and its own prices*:
shop.polymaker.com quotes USD 25.99 for the spool that /en-ca/ quotes at
CAD 36.99. So the prefix on a URL is load-bearing — it has to survive into
every endpoint built from that URL — and the market's currency is worth one
cheap /cart.js lookup rather than a guess from the hostname.
"""
from __future__ import annotations

import re
from decimal import Decimal
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .. import preferences
from ..fetch import fetcher
from ..money import (cents_to_decimal, currency_for_host, parse_price,
                     region_for_currency)
from ..settings import settings
from .base import StoreAdapter, StoreResult, host_of

_HANDLE_RE = re.compile(r"/products/([^/?#]+)")

# suggest.json decorates every product URL with the search that found it
# (?_pos=1&_psq=sunlu&_psid=<random>&_ss=e). The _psid differs on every call,
# so the same listing came back as a new offer each time coverage was widened.
_SEARCH_TRACKING_PARAMS = frozenset({"_pos", "_psq", "_psid", "_ss", "_sid"})


def _canonical_product_url(url: str) -> str:
    """Drop Shopify's search-attribution parameters and keep the rest — a
    ?variant= is part of what the listing is."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k not in _SEARCH_TRACKING_PARAMS]
    return urlunsplit(parts._replace(query=urlencode(kept), fragment=""))
#: A Markets locale segment: /en-ca, /fr-ca, /de, /en-eu.
_LOCALE_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$")

#: market root → what it actually bills in ("" once asked and answered badly)
_MARKET_CURRENCY: dict[str, str] = {}
#: storefront domain → locale prefix that bills in the shopper's currency
_PREFERRED_MARKET: dict[str, str] = {}

#: Locales worth trying for a region beyond the generic en-<cc> / <cc> pair.
_EXTRA_LOCALES = {"CA": ("fr-ca",), "CH": ("de-ch", "fr-ch"), "BE": ("nl-be", "fr-be")}

#: What every Shopify theme leaks into its own HTML.
_PLATFORM_MARKERS = ("cdn.shopify.com", "/cdn/shop/", "shopify.shop", "shopify-section")


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme or "https", parts.netloc, "", "", ""))


def _locale_prefix(url: str) -> str:
    """The Markets locale segment of a URL path, or "" when there is none.

    A false positive is harmless: the prefix is only ever echoed back into an
    endpoint on the same host, so at worst we mirror the path the user gave us.
    """
    parts = [p for p in urlsplit(url).path.split("/") if p]
    if len(parts) >= 2 and _LOCALE_RE.match(parts[0]):
        return "/" + parts[0]
    return ""


def _market_root(url: str) -> str:
    """Origin plus locale prefix — the base every endpoint must be built on."""
    return _origin(url) + _locale_prefix(url)


def _locale_candidates(region: str) -> tuple[str, ...]:
    code = (region or "").strip().lower()
    if not code:
        return ()
    return (f"en-{code}", code, *_EXTRA_LOCALES.get(code.upper(), ()))


async def market_currency(market_root: str, fallback: str) -> str:
    """What a market bills in, asked once and remembered.

    /products/<handle>.js reports bare cents and no currency at all, and the
    hostname is a poor stand-in once Markets is in play — the same .com
    storefront bills USD at / and CAD under /en-ca. cart.js is the one cheap
    endpoint that simply states the answer.
    """
    code = _MARKET_CURRENCY.get(market_root)
    if code is None:
        try:
            data = await fetcher.get_json(f"{market_root}/cart.js")
            found = str((data or {}).get("currency") or "").strip().upper()
        except Exception:                    # no cart.js, or not a market at all
            found = ""
        code = found if len(found) == 3 else ""
        _MARKET_CURRENCY[market_root] = code
    return code or fallback


async def preferred_market(domain: str, default_currency: str) -> str:
    """Locale prefix on `domain` that bills in the shopper's currency, or "".

    A Markets storefront sells the same spool to a Canadian in CAD and to an
    American in USD; searching the default market and converting afterwards
    would quote a number nobody is charged. Each candidate is one small
    request, validated against the market's own currency — so a wrong guess is
    inert — and the answer is cached for the life of the process.
    """
    wanted = preferences.preferred_currency()
    if not wanted or wanted == default_currency:
        return ""
    cached = _PREFERRED_MARKET.get(domain)
    if cached is not None:
        return cached
    prefix = ""
    region = preferences.preferred_region() or region_for_currency(wanted)
    for locale in _locale_candidates(region):
        if await market_currency(f"https://{domain}/{locale}", "") == wanted:
            prefix = f"/{locale}"
            break
    _PREFERRED_MARKET[domain] = prefix
    return prefix


def _pick_variant(product: dict, url: str, money) -> dict | None:
    variants = product.get("variants") or []
    if not variants:
        return None
    query = urlsplit(url).query
    match = re.search(r"variant=(\d+)", query)
    if match:
        for variant in variants:
            if str(variant.get("id")) == match.group(1):
                return variant
    available = [v for v in variants if v.get("available")]
    pool = available or variants
    # Cheapest available variant is the honest "starting at" price.
    return min(pool, key=lambda v: money(v.get("price")) or Decimal(10**12))


def _suggest_price(raw):
    """suggest.json returns cents on some themes and a formatted string on others."""
    if isinstance(raw, int) or (isinstance(raw, str) and raw.isdigit()):
        return cents_to_decimal(raw)
    return parse_price(raw)


class ShopifyAdapter(StoreAdapter):
    name = "shopify"

    def __init__(self, name: str | None = None, domains: tuple[str, ...] = ()):
        if name:
            self.name = name
        self.domains = domains

    def matches(self, url: str) -> bool:
        if self.domains:
            return super().matches(url)
        return False

    @staticmethod
    async def _product(root: str, handle: str) -> tuple[dict | None, bool, str]:
        """(payload, prices_are_cents, error).

        .js is canonical, but a handful of themes and app-protected stores
        answer it with a 404 or with HTML. The older .json view of the same
        product still works on those — same data, prices as decimal strings
        rather than cents, and no availability flag.
        """
        try:
            data = await fetcher.get_json(f"{root}/products/{handle}.js")
            if isinstance(data, dict) and data.get("variants") is not None:
                return data, True, ""
            error = "shopify .js returned no product"
        except Exception as exc:
            error = f"shopify .js failed: {exc}"
        try:
            payload = await fetcher.get_json(f"{root}/products/{handle}.json")
            product = (payload or {}).get("product")
            if isinstance(product, dict):
                return product, False, ""
        except Exception:
            pass
        return None, False, error

    async def fetch_offer(self, url: str) -> StoreResult:
        store = self.name if self.domains else host_of(url)
        match = _HANDLE_RE.search(urlsplit(url).path)
        if not match:
            return StoreResult(store=store, url=url, error="not a Shopify product URL")
        handle = match.group(1).removesuffix(".js").removesuffix(".json")
        # Keep the market the URL points at: /en-ca is a different price list,
        # not decoration, so dropping it silently quotes the wrong country.
        root = _market_root(url)
        data, in_cents, error = await self._product(root, handle)
        if data is None:
            return StoreResult(store=store, url=url, error=error)

        money = cents_to_decimal if in_cents else parse_price
        variant = _pick_variant(data, url, money) or {}
        available = variant.get("available", data.get("available"))
        return StoreResult(
            store=store,
            url=_canonical_product_url(url),
            title=data.get("title"),
            price=money(variant.get("price") or data.get("price")),
            currency=await market_currency(
                root, currency_for_host(host_of(url), settings.pw_currency)),
            # .json carries no availability flag; None says "unknown" rather
            # than reporting a stocked product as sold out.
            in_stock=bool(available) if available is not None else None,
            sku=variant.get("sku") or str(data.get("id") or "") or None,
            method="shopify-json" if in_cents else "shopify-json-legacy",
            extra={
                "variant": variant.get("title"),
                "compare_at": money(variant.get("compare_at_price")),
                "market": _locale_prefix(url) or None,
            },
        )

    async def search(self, query: str, limit: int = 5) -> list[StoreResult]:
        if not self.domains:
            return []
        domain = self.storefront()
        fallback = currency_for_host(domain, settings.pw_currency)
        root = f"https://{domain}"
        market = root + await preferred_market(domain, fallback)
        currency = await market_currency(market, fallback)
        endpoint = (
            f"{market}/search/suggest.json?q={quote(query)}"
            f"&resources[type]=product&resources[limit]={min(limit, 10)}"
        )
        try:
            data = await fetcher.get_json(endpoint)
        except Exception:
            return []
        products = (((data.get("resources") or {}).get("results") or {}).get("products")) or []
        results: list[StoreResult] = []
        for item in products[:limit]:
            url = item.get("url") or ""
            # suggest.json already returns market-prefixed paths, so these join
            # onto the bare origin — prefixing again would give /en-ca/en-ca/.
            if url.startswith("/"):
                url = root + url
            url = _canonical_product_url(url)
            results.append(
                StoreResult(
                    store=self.name,
                    url=url,
                    title=item.get("title"),
                    price=_suggest_price(item.get("price")),
                    currency=currency,
                    in_stock=item.get("available"),
                    method="shopify-suggest",
                )
            )
        return results


async def looks_like_shopify(url: str) -> bool:
    """Cheap probe used when a domain is not in the catalog."""
    try:
        page = await fetcher.get(f"{_origin(url)}/products.json?limit=1")
        if page.status == 200 and '"products"' in page.text[:2000]:
            return True
    except Exception:
        pass
    # Storefronts increasingly gate /products.json behind an app or a 404, so
    # fall back to the platform's own fingerprint in the page it just served.
    try:
        page = await fetcher.get(url)
    except Exception:
        return False
    body = page.text[:200_000].lower()
    return page.status == 200 and any(marker in body for marker in _PLATFORM_MARKERS)
