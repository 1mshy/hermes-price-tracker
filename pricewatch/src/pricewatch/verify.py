"""Site verification harness.

For every store in the catalog: discover a real product URL, read the price the
same way the tracker would, and report exactly what happened. Product handles go
stale constantly, so URLs are discovered live (catalog JSON → sitemap → homepage
link scrape) instead of being hardcoded.

    docker compose run --rm pricewatch python -m pricewatch.verify
    …                                        python -m pricewatch.verify --tag 3dprinting --json report.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from urllib.parse import urljoin, urlsplit

from .fetch import fetcher
from .stores.catalog import CATALOG
from .stores.registry import ADAPTERS, fetch_offer, searchable

# How a product URL looks at stores that need link-scraping to find one.
PRODUCT_PATTERNS: dict[str, str] = {
    "amazon": r"/dp/[A-Z0-9]{10}",
    "ebay": r"/itm/\d{9,15}",
    "bestbuy": r"/site/[^\s\"']*\d{7}\.p",
    "walmart": r"/ip/[^\s\"']*\d{6,}",
    "target": r"/p/[^\s\"']*A-\d+",
    "newegg": r"/p/[A-Z0-9\-]{8,}",
    "microcenter": r"/product/\d+/",
    "bhphoto": r"/c/product/\d+-REG/",
    "adorama": r"/[a-z0-9]+\.html",
    "matterhackers": r"/store/l/[^\s\"']+/sk/[A-Z0-9]+",
    "digikey": r"/en/products/detail/[^\s\"']+",
    "mouser": r"/ProductDetail/[^\s\"']+",
    "3djake": r"/[a-z0-9\-]+/[a-z0-9\-]+",
    "bestbuyca": r"/product/[^\s\"']+/\d{8}",
    "canadacomputers": r"/en/[^\s\"']+/\d+/[^\s\"']+\.html",
    "memoryexpress": r"/Products/MX\d+",
}
GENERIC_PATTERN = r"/(?:products?|item|p|dp)/[A-Za-z0-9][^\s\"'<>]{3,80}"


class _Walled(Exception):
    """Discovery met a bot wall no permitted rung could clear — the store is
    not "without products", it is unreachable from here."""


def _root(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme or 'https'}://{parts.netloc}"


async def _from_shopify_catalog(host: str) -> str | None:
    for path in (f"https://{host}/products.json?limit=3",):
        try:
            page = await fetcher.get(path)
            if page.status == 200 and page.text.lstrip().startswith("{"):
                products = json.loads(page.text).get("products") or []
                if products:
                    return f"https://{host}/products/{products[0]['handle']}"
        except Exception:
            pass
    return None


async def _from_sitemap(host: str) -> str | None:
    for path in ("/sitemap_products_1.xml?from=1&to=999999999", "/sitemap.xml"):
        try:
            page = await fetcher.get(f"https://{host}{path}")
            if page.status != 200:
                continue
            hits = re.findall(r"<loc>\s*([^<\s]+/products/[^<\s]+)\s*</loc>", page.text)
            if hits:
                return hits[len(hits) // 2]      # middle entry: less likely to be a test product
        except Exception:
            pass
    return None


async def _from_woo(host: str) -> str | None:
    try:
        data = await fetcher.get_json(f"https://{host}/wp-json/wc/store/v1/products?per_page=1")
        if isinstance(data, list) and data:
            return data[0].get("permalink")
    except Exception:
        pass
    return None


async def _from_homepage(host: str, key: str, allow_browser: bool) -> str | None:
    pattern = PRODUCT_PATTERNS.get(key, GENERIC_PATTERN)
    home = f"https://{host}/"
    try:
        page = await fetcher.get(home)
        if (page.looks_blocked or len(page.text) < 2000) and allow_browser:
            page = await fetcher.render(home)
    except Exception:
        if not allow_browser:
            return None
        try:
            page = await fetcher.render(home)
        except Exception:
            return None
    if page.looks_blocked:
        raise _Walled(f"{home} answered a bot challenge")

    root = _root(page.url)
    for match in re.finditer(rf'href=["\']([^"\']*{pattern}[^"\']*)["\']', page.text):
        href = match.group(1)
        if any(skip in href for skip in ("/cart", "/account", "javascript:", "#", "/cdn/", "/static/")):
            continue
        absolute = urljoin(root, href)
        # Stay on the store's own domain — homepages link out to CDNs and marketplaces.
        if urlsplit(absolute).netloc.removeprefix("www.") != urlsplit(root).netloc.removeprefix("www."):
            continue
        return absolute
    return None


async def discover_product_url(entry: dict, allow_browser: bool) -> tuple[str | None, str]:
    """Return (url, how_it_was_found)."""
    host = entry["domains"][0]
    if entry.get("probe"):
        return entry["probe"], "catalog-seed"
    if entry["kind"] == "shopify":
        for finder, label in ((_from_shopify_catalog, "products.json"), (_from_sitemap, "sitemap")):
            found = await finder(host)
            if found:
                return found, label
    if entry["kind"] == "woo":
        found = await _from_woo(host)
        if found:
            return found, "woo-api"
    for finder, label in ((_from_sitemap, "sitemap"), ):
        found = await finder(host)
        if found:
            return found, label
    walled = False
    try:
        found = await _from_homepage(host, entry["key"], allow_browser)
    except _Walled:
        walled, found = True, None
    if found:
        return found, "homepage-scrape"
    # Last resort: a searchable store can name its own product page (works for
    # marketplaces like eBay whose homepage carries no direct item links).
    if searchable(entry["key"]):
        try:
            hits = await ADAPTERS[entry["key"]].search(
                entry.get("probe_query", "3d printer filament"), limit=3)
            if hits:
                return hits[0].url, "adapter-search"
        except Exception:
            pass
    return None, ("blocked" if walled else "not-found")


def _classify(result, url: str | None, how: str = "") -> str:
    if url is None:
        return "BLOCKED" if how == "blocked" else "NO-URL"
    if result is None:
        return "ERROR"
    if result.ok:
        return "OK"
    error = (result.error or "").lower()
    method = (result.method or "").lower()
    if "needs-key" in method or "not set" in error:
        return "NEEDS-KEY"
    if "blocked" in method or "challenge" in error or "403" in error:
        return "BLOCKED"
    if "unsupported" in method:
        return "LIMITED"      # search-only store: compare works, tracking does not
    return "FAIL"


async def verify_store(entry: dict, allow_browser: bool, timeout: float) -> dict:
    started = time.monotonic()
    url = None
    try:
        url, how = await asyncio.wait_for(
            discover_product_url(entry, allow_browser), timeout=timeout)
        result = await asyncio.wait_for(fetch_offer(url), timeout=timeout) if url else None
    except asyncio.TimeoutError:
        return {"key": entry["key"], "label": entry["label"], "kind": entry["kind"],
                "tags": entry["tags"], "status": "TIMEOUT", "url": url, "discovery": "timeout",
                "seconds": round(time.monotonic() - started, 1)}
    except Exception as exc:
        return {"key": entry["key"], "label": entry["label"], "kind": entry["kind"],
                "tags": entry["tags"], "status": "ERROR", "url": url,
                "error": f"{type(exc).__name__}: {exc}"[:160],
                "seconds": round(time.monotonic() - started, 1)}

    status = _classify(result, url, how)
    return {
        "key": entry["key"], "label": entry["label"], "kind": entry["kind"],
        "tags": entry["tags"], "status": status, "url": url, "discovery": how,
        "price": float(result.price) if result and result.price is not None else None,
        "currency": result.currency if result else None,
        "in_stock": result.in_stock if result else None,
        "method": result.method if result else None,
        "title": (result.title or "")[:70] if result else None,
        "error": (result.error or "")[:160] if result and result.error else None,
        "searchable": searchable(entry["key"]),
        "seconds": round(time.monotonic() - started, 1),
    }


ICONS = {"OK": "✅", "LIMITED": "🔎", "BLOCKED": "🚫", "NEEDS-KEY": "🔑", "FAIL": "❌",
         "TIMEOUT": "⏱", "ERROR": "💥", "NO-URL": "❓"}


async def run(tags: list[str] | None, only: list[str] | None, allow_browser: bool,
              concurrency: int, timeout: float) -> list[dict]:
    entries = [
        e for e in CATALOG
        if (not tags or any(t in e["tags"] for t in tags))
        and (not only or e["key"] in only)
    ]
    semaphore = asyncio.Semaphore(concurrency)

    async def guarded(entry: dict) -> dict:
        async with semaphore:
            return await verify_store(entry, allow_browser, timeout)

    return await asyncio.gather(*(guarded(e) for e in entries))


def render_table(rows: list[dict]) -> str:
    lines = [
        f"{'':2} {'STORE':<22} {'CATEGORY':<12} {'STATUS':<10} {'PRICE':>14}  {'METHOD':<22} NOTE",
        "─" * 122,
    ]
    order = {"OK": 0, "LIMITED": 0.5, "NEEDS-KEY": 1, "BLOCKED": 2, "FAIL": 3, "TIMEOUT": 4, "ERROR": 5, "NO-URL": 6}
    for row in sorted(rows, key=lambda r: (order.get(r["status"], 9), r["key"])):
        # The currency is the half of a price that goes wrong silently, so
        # it is printed with the figure rather than hidden in the JSON.
        price = (f"{row['price']:,.2f} {row.get('currency') or ''}".strip()
                 if row.get("price") is not None else "—")
        note = row.get("error") or (row.get("title") or "")
        lines.append(
            f"{ICONS.get(row['status'], '?'):2} {row['label']:<22.22} "
            f"{','.join(row['tags']):<12.12} {row['status']:<10} {price:>14}  "
            f"{(row.get('method') or '—'):<22.22} {note[:38]}"
        )
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    summary = "  ".join(f"{ICONS.get(k, '?')} {k}={v}" for k, v in sorted(counts.items()))
    ok = counts.get("OK", 0)
    lines += ["─" * 122, f"{ok}/{len(rows)} stores returned a live price.   {summary}"]
    return "\n".join(lines)


async def main_async(args) -> int:
    rows = await run(args.tag, args.store, not args.no_browser, args.concurrency, args.timeout)
    print(render_table(rows))
    if args.json:
        with open(args.json, "w") as handle:
            json.dump(rows, handle, indent=2)
        print(f"\nwrote {args.json}")
    await fetcher.aclose()
    hard_failures = [r for r in rows if r["status"] in ("FAIL", "ERROR", "NO-URL")]
    return 1 if args.strict and hard_failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify store price extraction end to end.")
    parser.add_argument("--tag", action="append", help="filter: tech | 3dprinting | components")
    parser.add_argument("--store", action="append", help="filter by store key (repeatable)")
    parser.add_argument("--no-browser", action="store_true", help="skip Chromium fallback")
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--json", help="also write the raw report to this path")
    parser.add_argument("--strict", action="store_true", help="exit non-zero on hard failures")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
