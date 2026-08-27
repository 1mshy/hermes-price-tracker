"""Adapters backed by official retailer APIs.

Where a retailer publishes an API, use it: it is allowed, stable, rate-limited on
their terms, and immune to the bot-walls that make HTML scraping of the big-box
sites unreliable. Each adapter degrades to `requires_key` when unconfigured.
"""
from __future__ import annotations

import re
import time
from urllib.parse import quote

from lxml import html as lxml_html

from ..extract import extract
from ..fetch import fetcher
from ..money import detect_currency, parse_price
from ..settings import settings
from .base import StoreAdapter, StoreResult

_BESTBUY_FIELDS = "sku,name,salePrice,regularPrice,onlineAvailability,inStoreAvailability,url,manufacturer,modelNumber"


class BestBuyAdapter(StoreAdapter):
    """Best Buy Developer API — free key from developer.bestbuy.com."""

    name = "bestbuy"
    domains = ("bestbuy.com",)
    requires_key = "BESTBUY_API_KEY"

    @staticmethod
    def _sku(url: str) -> str | None:
        match = re.search(r"/(\d{7})\.p|skuId=(\d{7})", url)
        return next((g for g in (match.groups() if match else []) if g), None)

    async def fetch_offer(self, url: str) -> StoreResult:
        key = settings.bestbuy_api_key
        sku = self._sku(url)
        if not key:
            return StoreResult(store=self.name, url=url, method="needs-key",
                               error="BESTBUY_API_KEY not set (free at developer.bestbuy.com)")
        if not sku:
            return StoreResult(store=self.name, url=url, error="no SKU in Best Buy URL")
        endpoint = (f"https://api.bestbuy.com/v1/products(sku={sku})"
                    f"?apiKey={key}&format=json&show={_BESTBUY_FIELDS}")
        try:
            data = await fetcher.get_json(endpoint)
        except Exception as exc:
            return StoreResult(store=self.name, url=url, error=f"bestbuy api: {exc}")
        items = data.get("products") or []
        if not items:
            return StoreResult(store=self.name, url=url, error="SKU not found")
        item = items[0]
        return StoreResult(
            store=self.name, url=item.get("url") or url, title=item.get("name"),
            price=parse_price(item.get("salePrice")), currency="USD",
            in_stock=bool(item.get("onlineAvailability")), sku=str(item.get("sku")),
            method="bestbuy-api",
            extra={"regular_price": item.get("regularPrice"), "model": item.get("modelNumber")},
        )

    async def search(self, query: str, limit: int = 5) -> list[StoreResult]:
        key = settings.bestbuy_api_key
        if not key:
            return []
        terms = "&".join(f"search={quote(w)}" for w in query.split()[:6])
        endpoint = (f"https://api.bestbuy.com/v1/products(({terms}))"
                    f"?apiKey={key}&format=json&show={_BESTBUY_FIELDS}&pageSize={limit}&sort=salePrice.asc")
        try:
            data = await fetcher.get_json(endpoint)
        except Exception:
            return []
        return [
            StoreResult(store=self.name, url=item.get("url", ""), title=item.get("name"),
                        price=parse_price(item.get("salePrice")), currency="USD",
                        in_stock=bool(item.get("onlineAvailability")),
                        sku=str(item.get("sku")), method="bestbuy-api")
            for item in (data.get("products") or [])[:limit]
        ]


class EbayAdapter(StoreAdapter):
    """eBay Browse API (client-credentials OAuth)."""

    name = "ebay"
    domains = ("ebay.com",)
    requires_key = "EBAY_APP_ID + EBAY_CERT_ID"
    _token: tuple[str, float] | None = None

    async def _access_token(self) -> str | None:
        import base64
        app_id, cert_id = settings.ebay_app_id, getattr(settings, "ebay_cert_id", "")
        if not app_id or not cert_id:
            return None
        if EbayAdapter._token and EbayAdapter._token[1] > time.time() + 60:
            return EbayAdapter._token[0]
        basic = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()
        client = await fetcher.client()
        response = await client.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials",
                  "scope": "https://api.ebay.com/oauth/api_scope"},
        )
        if response.status_code != 200:
            return None
        payload = response.json()
        EbayAdapter._token = (payload["access_token"], time.time() + payload.get("expires_in", 7200))
        return EbayAdapter._token[0]

    async def fetch_offer(self, url: str) -> StoreResult:
        token = await self._access_token()
        item_id = re.search(r"/itm/(?:[^/]+/)?(\d{9,15})", url)
        if not token:
            # Keyless fallback: item pages ship schema.org JSON-LD and read
            # fine over the fingerprinted HTTP path. The API stays preferred
            # when configured — it is stable and returns the true buy-box offer.
            return await self._fetch_keyless(url, item_id.group(1) if item_id else None)
        if not item_id:
            return StoreResult(store=self.name, url=url, error="no item id in eBay URL")
        endpoint = f"https://api.ebay.com/buy/browse/v1/item/v1|{item_id.group(1)}|0"
        try:
            data = await fetcher.get_json(endpoint, headers={"Authorization": f"Bearer {token}"})
        except Exception as exc:
            return StoreResult(store=self.name, url=url, error=f"ebay api: {exc}")
        price = (data.get("price") or {})
        return StoreResult(store=self.name, url=data.get("itemWebUrl") or url,
                           title=data.get("title"), price=parse_price(price.get("value")),
                           currency=price.get("currency", "USD"),
                           in_stock=(data.get("estimatedAvailabilities") or [{}])[0]
                           .get("estimatedAvailabilityStatus") != "OUT_OF_STOCK",
                           sku=data.get("legacyItemId"), method="ebay-api")

    async def _fetch_keyless(self, url: str, item_id: str | None) -> StoreResult:
        try:
            page = await fetcher.get(url)
        except Exception as exc:                       # noqa: BLE001 — network
            return StoreResult(store=self.name, url=url, error=f"fetch failed: {exc}")
        if page.status != 200 or page.looks_blocked:
            return StoreResult(
                store=self.name, url=url, method="needs-key",
                error=(f"eBay page unavailable keylessly (HTTP {page.status}) — "
                       "set EBAY_APP_ID/EBAY_CERT_ID for the reliable API path"))
        found = extract(page.text)
        if not found.ok:
            return StoreResult(store=self.name, url=page.url, method="http:failed",
                               error="no structured price on eBay page "
                                     "(possibly an ended or variant listing)")
        return StoreResult(store=self.name, url=page.url, title=found.title,
                           price=found.price, currency=found.currency or "USD",
                           in_stock=found.in_stock, sku=item_id or found.sku,
                           method=f"http:{found.method}")

    async def search(self, query: str, limit: int = 5) -> list[StoreResult]:
        token = await self._access_token()
        if not token:
            return await self._search_keyless(query, limit)
        endpoint = (f"https://api.ebay.com/buy/browse/v1/item_summary/search"
                    f"?q={quote(query)}&limit={limit}&filter=conditions:{{NEW}}")
        try:
            data = await fetcher.get_json(endpoint, headers={"Authorization": f"Bearer {token}"})
        except Exception:
            return []
        results = []
        for item in (data.get("itemSummaries") or [])[:limit]:
            price = item.get("price") or {}
            results.append(StoreResult(store=self.name, url=item.get("itemWebUrl", ""),
                                       title=item.get("title"),
                                       price=parse_price(price.get("value")),
                                       currency=price.get("currency", "USD"),
                                       in_stock=True, sku=item.get("legacyItemId"),
                                       method="ebay-api"))
        return results

    async def _search_keyless(self, query: str, limit: int) -> list[StoreResult]:
        """Buy-It-Now, new-condition search over the public results page."""
        url = (f"https://www.ebay.com/sch/i.html?_nkw={quote(query)}"
               "&LH_BIN=1&LH_ItemCondition=1000")
        try:
            page = await fetcher.get(url)
        except Exception:                              # noqa: BLE001 — network
            return []
        if page.status != 200 or page.looks_blocked:
            return []
        return _parse_ebay_search(page.text, store=self.name, limit=limit)


def _parse_ebay_search(raw: str, store: str = "ebay", limit: int = 5) -> list[StoreResult]:
    """Result cards from the keyless search page (s-card__*/su-card markup).

    Auction noise is pre-filtered by the LH_BIN query; placeholder "Shop on
    eBay" tiles and cards without a price are skipped.
    """
    try:
        doc = lxml_html.fromstring(raw)
    except Exception:                                  # noqa: BLE001
        return []
    results: list[StoreResult] = []
    seen: set[str] = set()
    for link in doc.xpath('//a[contains(@href, "/itm/")]'):
        match = re.search(r"/itm/(\d{9,15})", link.get("href") or "")
        if not match or match.group(1) in seen:
            continue
        # Climb to the smallest ancestor that actually contains both a title
        # and a price node — class names churn, the structure does not.
        card = None
        for ancestor in link.iterancestors():
            classes = ancestor.get("class") or ""
            if "card" not in classes and "s-item" not in classes:
                continue
            if (ancestor.xpath('.//*[contains(@class, "__title")]')
                    and ancestor.xpath('.//*[contains(@class, "__price")]')):
                card = ancestor
                break
        if card is None:
            continue
        title = " ".join(
            card.xpath('.//*[contains(@class, "__title")]')[0].text_content().split())
        price_text = card.xpath('.//*[contains(@class, "__price")]')[0].text_content()
        price = parse_price(price_text)
        if not title or price is None or title.lower().startswith("shop on ebay"):
            continue
        seen.add(match.group(1))
        results.append(StoreResult(
            store=store, url=f"https://www.ebay.com/itm/{match.group(1)}",
            title=title, price=price,
            currency=detect_currency(price_text, "USD"),
            in_stock=True, sku=match.group(1), method="ebay-html",
            extra={"note": "keyless HTML search result — confirm with get_price "
                           "on the item URL before quoting as the final price"},
        ))
        if len(results) >= limit:
            break
    return results


class KeepaAmazon:
    """Amazon pricing via Keepa. Optional, paid, but the only dependable route."""

    @staticmethod
    async def lookup(asin: str, domain: int = 1) -> StoreResult | None:
        key = settings.keepa_api_key
        if not key:
            return None
        endpoint = (f"https://api.keepa.com/product?key={key}&domain={domain}"
                    f"&asin={asin}&stats=1&history=0")
        try:
            data = await fetcher.get_json(endpoint)
        except Exception:
            return None
        products = data.get("products") or []
        if not products:
            return None
        product = products[0]
        stats = product.get("stats") or {}
        current = stats.get("current") or []
        # Keepa indices: 0 = Amazon, 1 = new 3rd-party. Values are cents, -1 = unavailable.
        cents = next((c for c in (current[0:1] + current[1:2]) if isinstance(c, int) and c > 0), None)
        if cents is None:
            return None
        from decimal import Decimal
        return StoreResult(store="amazon", url=f"https://www.amazon.com/dp/{asin}",
                           title=product.get("title"), price=Decimal(cents) / 100,
                           currency="USD", in_stock=True, sku=asin, method="keepa-api")
