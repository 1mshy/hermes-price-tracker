"""Application logic: register products, refresh prices, decide when to shout."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import math
import re
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import matching, preferences
from .db import in_db, session_scope
from .models import AlertEvent, Offer, PricePoint, Product, Tracker, utcnow
from .money import fmt, usd_sort_key
from .notify import Alert, dispatch
from .stores.base import StoreResult, host_of
from .stores.catalog import keys_for_country
from .stores.registry import ADAPTERS, fetch_offer, resolve, search_stores, store_key_for_url

log = logging.getLogger(__name__)

# A single sweep should not hammer every store at once.
_SWEEP_CONCURRENCY = 6


async def _search_ranked(query: str, *, stores: list[str] | None,
                         limit_per_store: int, threshold: float) -> tuple[list, int, str]:
    """Fan out a store search and rank the hits against the full query.

    Store search endpoints (Shopify suggest, Woo Store API, retail APIs) are
    AND-ish — every extra token narrows the result set — so a fully specified
    description often returns nothing even when the product is listed. When
    that happens, retry once with the compact core (leading brand/product
    tokens + model designators) while still ranking against the full
    description. Returns (ranked, searched_count, query_used).
    """
    found = await search_stores(query, store_keys=stores, limit_per_store=limit_per_store)
    ranked = matching.rank(query, found, key=lambda r: r.title, threshold=threshold)
    if ranked:
        return ranked, len(found), query

    core = matching.search_terms(query)
    if core and core.split() != matching.normalise(query).split():
        found = await search_stores(core, store_keys=stores, limit_per_store=limit_per_store)
        ranked = matching.rank(query, found, key=lambda r: r.title, threshold=threshold)
        return ranked, len(found), core
    return ranked, len(found), query


def _is_trackable(store_key: str) -> bool:
    """Search-only marketplaces (e.g. AliExpress) can be compared but not
    swept — creating offers for them would only accumulate fetch errors."""
    adapter = ADAPTERS.get(store_key)
    return adapter is None or getattr(adapter, "trackable", True)


async def _fetch_for_tracking(url: str) -> tuple[StoreResult, str | None]:
    """Read a listing that is about to be registered, on the user's regional
    storefront when the store runs one.

    A pasted amazon.com link is the same ASIN on amazon.ca, and only the .ca
    page bills a Canadian shopper in CAD; the .com page shows them a converted
    figure that flips between CAD and USD from one read to the next — no
    basis for an alert. The URL as given is the fallback when the regional
    listing does not resolve. Returns the result and a note on what was
    substituted, if anything.
    """
    adapter = await resolve(url)
    regional = adapter.regional_url(url)
    if regional and regional != url:
        result = await fetch_offer(regional)
        if result.ok:
            return result, (f"tracking the {host_of(regional)} listing rather than the "
                            f"{host_of(url)} one — that is the storefront that bills in "
                            f"{result.currency}")
    return await fetch_offer(url), None


def _missing_condition(target_price: float | None, drop_pct: float | None,
                       alert_on_restock: bool) -> dict | None:
    """A watch with nothing to fire on would sit silent forever. Refuse it
    before any store is read, so the agent goes back and asks."""
    if target_price is None and drop_pct is None and not alert_on_restock:
        return {"ok": False, "error": ("a watch needs target_price, drop_pct, or "
                                       "alert_on_restock=true — ask the user which they want")}
    return None


def _out_of_stock_note(store: str) -> str:
    return (f"currently out of stock at {store}; set alert_on_restock=true to be told "
            f"when it returns")


# ─────────────────────────── registration ────────────────────────────────
_intake_tasks: dict[int, asyncio.Task] = {}
_intake_lock: asyncio.Lock | None = None
_intake_loop: asyncio.AbstractEventLoop | None = None
_discovery_gate: asyncio.Semaphore | None = None
_discovery_loop: asyncio.AbstractEventLoop | None = None


def _watch_input(value: str) -> tuple[str, str]:
    """Normalise pasted links without losing variant or seller parameters."""
    value = value.strip()
    if not value or len(value) > 1000 or "\n" in value or "\r" in value:
        raise ValueError("Enter one product link or name (up to 1,000 characters).")
    if re.match(r"^(?:www\.)?[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(?:[/?:#]|$)", value):
        value = "https://" + value
    if "://" in value:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise ValueError("Use an http or https product link without login details.")
        params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                  if not k.lower().startswith("utm_")
                  and k.lower() not in {"fbclid", "gclid", "ref", "ref_", "tag"}]
        value = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/",
                           urlencode(sorted(params)), ""))
        return value, "url:" + value
    value = " ".join(value.split())
    return value, "query:" + value.casefold()


def _watch_status(tracker: Tracker, product: Product, offers: list[Offer]) -> tuple[str, str]:
    if not tracker.active:
        return "paused", "Paused"
    live = [o for o in offers if o.active and
            (not tracker.currency or (o.currency or "").upper() == tracker.currency.upper())]
    if any(o.last_price is not None and not o.last_error for o in live):
        return "watching", "Watching for your price or stock alert"
    if any(o.last_price is not None for o in live):
        return "retrying", "Last price saved; retrying the retailer automatically"
    if product.discovery_checked_at or any(o.last_checked_at for o in live):
        return "retrying", "Saved; looking for a readable matching listing automatically"
    return "pending", "Saved; checking retailers in the background"


def _start_intake(product_id: int, *, alternatives: bool = False) -> asyncio.Task:
    """Keep a strong reference; sweeps resume unfinished work after restart."""
    existing = _intake_tasks.get(product_id)
    if existing is not None and not existing.done():
        return existing
    global _discovery_gate, _discovery_loop
    loop = asyncio.get_running_loop()
    if _discovery_loop is not loop:
        _discovery_loop, _discovery_gate = loop, asyncio.Semaphore(2)

    async def run() -> None:
        async with _discovery_gate:
            await _discover_product(product_id, alternatives=alternatives)

    task = asyncio.create_task(run())
    _intake_tasks[product_id] = task

    def done(completed: asyncio.Task) -> None:
        if _intake_tasks.get(product_id) is completed:
            _intake_tasks.pop(product_id, None)
        if not completed.cancelled() and completed.exception() is not None:
            log.warning("product %s discovery will retry: %s", product_id, completed.exception())

    task.add_done_callback(done)
    return task


async def add_watch(input: str, *, target_price: float | None = None,
                    drop_pct: float | None = None, alert_on_restock: bool = False,
                    stores: list[str] | None = None, channels: str = "",
                    cooldown_hours: int = 12) -> dict:
    """Save first, then research. With no rule supplied, watch for a 5% drop."""
    try:
        value, key = _watch_input(input)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if target_price is not None and (not math.isfinite(target_price) or target_price <= 0):
        return {"ok": False, "error": "Target price must be greater than zero."}
    if drop_pct is not None and (not math.isfinite(drop_pct) or not 0 < drop_pct < 100):
        return {"ok": False, "error": "Price drop must be greater than 0 and less than 100%."}
    if cooldown_hours < 1:
        return {"ok": False, "error": "Alert cooldown must be at least one hour."}
    if stores and any(key not in ADAPTERS for key in stores):
        return {"ok": False, "error": "An unknown store was selected."}
    if target_price is None and drop_pct is None and not alert_on_restock:
        drop_pct = 5.0

    def write(session: Session) -> dict:
        product = session.scalar(select(Product).where(Product.watch_key == key))
        if product is None and key.startswith("url:"):
            offer = session.scalar(select(Offer).where(Offer.url == value))
            product = offer.product if offer else None
        if product is None and key.startswith("query:"):
            # A user may paste the exact title of a product already added by
            # link. Match words exactly; fuzzy deduplication can merge variants.
            product = next((p for p in session.scalars(select(Product))
                            if " ".join(p.title.split()).casefold() == value.casefold()), None)
        if product is None:
            product = Product(title=value[:500], match_key=matching.match_key(value),
                              watch_input=value, watch_key=key,
                              watch_stores=json.dumps(stores) if stores else None)
            session.add(product)
            session.flush()
        tracker = session.scalar(select(Tracker).where(Tracker.product_id == product.id)
                                 .order_by(Tracker.active.desc(), Tracker.id))
        existing = tracker is not None
        if tracker is None:
            tracker = Tracker(product_id=product.id, label=value[:200],
                              target_price=Decimal(str(target_price)) if target_price is not None else None,
                              drop_pct=drop_pct, alert_on_restock=alert_on_restock,
                              channels=channels, cooldown_hours=cooldown_hours,
                              currency=(preferences.preferred_currency() or None)
                              if key.startswith("query:") else None)
            session.add(tracker)
            session.flush()
        if key.startswith("url:") and not product.offers:
            session.add(Offer(product_id=product.id, store=store_key_for_url(value) or host_of(value),
                              url=value, currency=tracker.currency or ""))
            session.flush()
            session.expire(product, ["offers"])
        _initial_baselines(session, product.id)
        status, status_note = _watch_status(tracker, product, product.offers)
        return {"ok": True, "product_id": product.id, "tracker_id": tracker.id,
                "title": product.title, "status": status, "existing": existing,
                "target_price": float(tracker.target_price) if tracker.target_price is not None else None,
                "drop_pct": tracker.drop_pct, "alert_on_restock": tracker.alert_on_restock,
                "note": ("Already on your watchlist; existing alert settings kept." if existing else
                         status_note + (" Alerts on a 5% price drop." if drop_pct == 5.0 else "."))}

    global _intake_lock, _intake_loop
    loop = asyncio.get_running_loop()
    if _intake_loop is not loop:
        _intake_loop, _intake_lock = loop, asyncio.Lock()
    async with _intake_lock:
        result = await in_db(write)
    if result["status"] in {"pending", "retrying"} and not result["existing"]:
        _start_intake(result["product_id"])
    return result


def _initial_baselines(session: Session, product_id: int) -> None:
    """A queued watch gets its baseline only after a real price is observed."""
    offers = session.scalars(select(Offer).where(Offer.product_id == product_id,
                                                Offer.active.is_(True))).all()
    priced = [o for o in offers if o.last_price is not None and not o.last_error]
    for tracker in session.scalars(select(Tracker).where(Tracker.product_id == product_id)).all():
        if tracker.baseline_price is not None:
            continue
        relevant = [o for o in priced if not tracker.currency or o.currency == tracker.currency]
        if relevant:
            best = min(relevant, key=lambda o: usd_sort_key(o.last_price, o.currency))
            tracker.baseline_price, tracker.currency = best.last_price, best.currency


async def _discover_product(product_id: int, *, alternatives: bool = False) -> None:
    def read(session: Session) -> tuple[str, list[str] | None] | None:
        product = session.get(Product, product_id)
        if not product or not any(t.active for t in product.trackers):
            return None
        value = product.title if alternatives else product.watch_input
        if not value:
            return None
        return value, json.loads(product.watch_stores) if product.watch_stores else None

    loaded = await in_db(read)
    if loaded is None:
        return
    value, stores = loaded
    is_url = value.startswith(("http://", "https://"))
    results: list[StoreResult] = []
    error = None
    try:
        if is_url:
            result, _ = await _fetch_for_tracking(value)
            results = [result]
            if not result.ok:
                error = result.error or "Retailer price is not readable yet"
        else:
            ranked, _, _ = await _search_ranked(value, stores=stores, limit_per_store=3, threshold=74.0)
            trackable = [r for r in ranked if r.ok and _is_trackable(r.store)]
            as_new = [r for r in trackable if not r.extra.get("condition") and not r.extra.get("marketplace")]
            results = (as_new or trackable)[:8]
            if not results:
                error = "No readable matching listing yet"
    except Exception as exc:
        log.info("product %s lookup deferred: %s", product_id, exc)
        error = "Retailer lookup is temporarily unavailable"

    def write(session: Session) -> None:
        product = session.get(Product, product_id)
        if product is None or not any(t.active for t in product.trackers):
            return
        product.discovery_checked_at, product.discovery_error = utcnow(), error
        accepted = []
        for result in results:
            # An offer URL belongs to one product; never silently attach its
            # price history to a second, unrelated product.
            collision = session.scalar(select(Offer).where(Offer.url == result.url))
            if collision is not None and collision.product_id != product_id:
                continue
            if is_url and result.url != value:
                placeholder = session.scalar(select(Offer).where(Offer.product_id == product_id,
                                                                 Offer.url == value))
                if placeholder and placeholder.last_price is None and collision is None:
                    placeholder.url = result.url
            accepted.append(_upsert_offer(session, product_id, result))
        good = [o for o in accepted if o.last_price is not None and not o.last_error]
        if good:
            if is_url and product.title == value and good[0].title:
                product.title = good[0].title
                product.match_key = matching.match_key(product.title)
                for tracker in product.trackers:
                    if tracker.label == value[:200]:
                        tracker.label = product.title[:200]
            _initial_baselines(session, product_id)
        elif results and not accepted:
            product.discovery_error = "Matching listings are already on another watch"

    await in_db(write)


async def track_url(url: str, *, target_price: float | None = None, drop_pct: float | None = None,
                    alert_on_restock: bool = False, label: str = "", channels: str = "",
                    cooldown_hours: int = 12) -> dict:
    """Start tracking a single product URL."""
    refused = _missing_condition(target_price, drop_pct, alert_on_restock)
    if refused:
        return refused
    result, note = await _fetch_for_tracking(url)
    if not result.ok:
        return {"ok": False, "error": result.error or "could not read a price", "url": url,
                "store": result.store, "method": result.method}
    if result.extra.get("marketplace"):
        # The watch follows that seller's price, not the retailer's own — worth
        # saying at registration, not only in the search hit it came from.
        note = "; ".join(filter(None, (
            note, result.extra.get("note") or "third-party marketplace listing")))

    def write(session: Session) -> dict:
        product = Product(
            title=result.title or url,
            match_key=matching.match_key(result.title),
        )
        session.add(product)
        session.flush()
        offer = _upsert_offer(session, product.id, result)
        tracker = Tracker(
            product_id=product.id,
            label=label or (result.title or url)[:200],
            target_price=Decimal(str(target_price)) if target_price is not None else None,
            drop_pct=drop_pct,
            baseline_price=result.price,
            currency=(result.currency or "").upper() or None,
            alert_on_restock=alert_on_restock,
            channels=channels,
            cooldown_hours=cooldown_hours,
        )
        session.add(tracker)
        session.flush()
        out = {"ok": True, "product_id": product.id, "tracker_id": tracker.id,
               "offer_id": offer.id, "title": product.title, "url": offer.url,
               "price": float(result.price), "currency": result.currency,
               "in_stock": result.in_stock,
               "store": result.store, "method": result.method}
        notes = [note] if note else []
        if result.in_stock is False and not alert_on_restock:
            notes.append(_out_of_stock_note(result.store))
        # A watch created on a coupon price has that as its baseline; say so,
        # or the first sweep after the coupon lapses looks like a price rise.
        if result.extra.get("deal_note"):
            notes.append(result.extra["deal_note"])
        if notes:
            out["note"] = "; ".join(notes)
        return out

    return await in_db(write)


async def track_query(description: str, *, stores: list[str] | None = None,
                      target_price: float | None = None, drop_pct: float | None = None,
                      alert_on_restock: bool = False, max_offers: int = 8,
                      threshold: float = 70.0, channels: str = "",
                      cooldown_hours: int = 12) -> dict:
    """Search stores for a described product and track every plausible match."""
    refused = _missing_condition(target_price, drop_pct, alert_on_restock)
    if refused:
        return refused
    ranked, searched, query_used = await _search_ranked(
        description, stores=stores, limit_per_store=3, threshold=threshold)
    if not ranked:
        return {"ok": False, "error": "no store listing matched that description",
                "searched": searched, "search_terms_tried": query_used}

    trackable = [r for r in ranked if _is_trackable(r.store)]
    # A renewed or open-box unit undercuts the new price by enough to always win
    # the `min` below — and that pick is permanent: it sets the baseline every
    # later drop is measured against, names the product, and becomes the
    # cheapest offer the sweep re-reads. One refurb here quietly turns the whole
    # tracker into a refurb tracker; a third-party marketplace seller (bestbuy.ca
    # flags them) does the same with somebody else's price. Kept only when that
    # is all there is.
    as_new = [r for r in trackable
              if not r.extra.get("condition") and not r.extra.get("marketplace")]
    keep = (as_new or trackable)[:max_offers]
    if not keep:
        return {"ok": False,
                "error": ("matches found only on search-only stores that cannot be "
                          "tracked — compare_prices shows their current prices; "
                          "track a specific URL at a supported store instead"),
                "matches": [r.as_dict() for r in ranked[:5]],
                "search_terms_tried": query_used}
    # The watch's thresholds are written in the currency of the listing it is
    # created from, so a listing in the user's own currency sets the baseline
    # whenever there is one; beyond that, rank at an indicative rate so
    # CAD 119 is not "more" than USD 99.99.
    preferred = preferences.preferred_currency()
    cheapest = min(keep, key=lambda r: (
        bool(preferred) and (r.currency or "").upper() != preferred,
        usd_sort_key(r.price, r.currency)))

    def write(session: Session) -> dict:
        product = Product(
            title=cheapest.title or description,
            match_key=matching.match_key(cheapest.title or description),
            notes=f"created from description: {description}"[:2000],
        )
        session.add(product)
        session.flush()
        offers = [_upsert_offer(session, product.id, r) for r in keep]
        tracker = Tracker(
            product_id=product.id,
            label=(cheapest.title or description)[:200],
            target_price=Decimal(str(target_price)) if target_price is not None else None,
            drop_pct=drop_pct,
            baseline_price=cheapest.price,
            currency=(cheapest.currency or "").upper() or None,
            alert_on_restock=alert_on_restock,
            channels=channels,
            cooldown_hours=cooldown_hours,
        )
        session.add(tracker)
        session.flush()
        out = {"ok": True, "product_id": product.id, "tracker_id": tracker.id,
               "title": product.title, "currency": tracker.currency, "offers": [
                   {"store": o.store, "url": o.url, "price": float(o.last_price or 0),
                    "currency": o.currency, "in_stock": o.in_stock}
                   for o in offers
               ],
               "best_price": float(cheapest.price), "best_store": cheapest.store,
               "search_terms_used": query_used}
        if cheapest.in_stock is False and not alert_on_restock:
            out["note"] = _out_of_stock_note(cheapest.store)
        return out

    return await in_db(write)


async def add_offer(product_id: int, url: str) -> dict:
    """Attach another store's listing to an existing tracked product."""
    result, note = await _fetch_for_tracking(url)
    if not result.ok:
        return {"ok": False, "error": result.error or "could not read a price", "url": url}

    def write(session: Session) -> dict:
        product = session.get(Product, product_id)
        if product is None:
            return {"ok": False, "error": f"no product {product_id}"}
        offer = _upsert_offer(session, product_id, result)
        out = {"ok": True, "offer_id": offer.id, "store": offer.store, "url": offer.url,
               "price": float(result.price), "currency": offer.currency,
               "product": product.title}
        notes = [note] if note else []
        watched_in = sorted({t.currency for t in product.trackers if t.currency})
        if watched_in and (offer.currency or "").upper() not in watched_in:
            notes.append(f"this listing bills in {offer.currency} while the watch on this "
                         f"product is in {'/'.join(watched_in)} — it is shown for reference "
                         f"and never triggers the alert")
        if notes:
            out["note"] = "; ".join(notes)
        return out

    return await in_db(write)


async def find_more_stores(product_id: int, *, threshold: float = 74.0,
                           max_new: int = 6, stores: list[str] | None = None) -> dict:
    """Broaden coverage: search the catalog for the same product elsewhere."""
    def read(session: Session) -> tuple[str, set[str]] | None:
        product = session.get(Product, product_id)
        if product is None:
            return None
        existing = {o.url for o in product.offers}
        return product.title, existing

    loaded = await in_db(read)
    if loaded is None:
        return {"ok": False, "error": f"no product {product_id}"}
    title, existing = loaded

    found = await search_stores(matching.search_terms(title) or title,
                                store_keys=stores, limit_per_store=3)
    fresh = [r for r in found if r.url not in existing and _is_trackable(r.store)]
    ranked = matching.rank(title, fresh, key=lambda r: r.title, threshold=threshold)[:max_new]

    def write(session: Session) -> dict:
        added = [_upsert_offer(session, product_id, r) for r in ranked]
        return {"ok": True, "added": [{"store": o.store, "url": o.url,
                                       "price": float(o.last_price or 0)} for o in added]}

    return await in_db(write)


def _upsert_offer(session: Session, product_id: int, result: StoreResult) -> Offer:
    """Insert-or-update an offer and append a price point when the price or
    the stock state moved."""
    offer = session.scalar(select(Offer).where(Offer.url == result.url))
    if offer is None:
        offer = Offer(product_id=product_id, store=result.store, url=result.url)
        session.add(offer)

    previous = offer.last_price
    if result.ok and result.store and (previous is None or offer.store == host_of(offer.url)):
        offer.store = result.store
    offer.title = result.title or offer.title
    offer.sku = result.sku or offer.sku
    offer.currency = result.currency or offer.currency
    offer.method = result.method
    offer.last_checked_at = utcnow()

    # A read that says nothing about stock (a bot wall, a 404, a store that
    # does not report it) leaves what we knew in place, like last_price. If
    # it cleared the flag instead, one blocked sweep between "sold out" and
    # "back" would turn a real restock into a first sighting and lose it.
    stock_changed = result.in_stock is not None and result.in_stock != offer.in_stock
    if stock_changed:
        if offer.in_stock is False and result.in_stock:
            offer.last_restocked_at = offer.last_checked_at
        offer.stock_changed_at = offer.last_checked_at
        offer.in_stock = result.in_stock

    if result.price is not None:
        offer.last_price = result.price
        offer.lowest_price = (result.price if offer.lowest_price is None
                              else min(offer.lowest_price, result.price))
        offer.last_error = None
        offer.consecutive_errors = 0
        # Rewritten on every good read: a coupon that lapsed leaves no note.
        offer.deal_note = result.extra.get("deal_note") or None
        # A stock-only point keeps availability visible in the history.
        if previous is None or previous != result.price or stock_changed:
            session.add(PricePoint(offer=offer, price=result.price,
                                   currency=offer.currency, in_stock=offer.in_stock))
    else:
        offer.last_error = result.error
        offer.consecutive_errors = (offer.consecutive_errors or 0) + 1

    session.flush()
    return offer


# ───────────────────────────── refreshing ────────────────────────────────
_full_sweep: asyncio.Task | None = None
_last_full_sweep: dict | None = None


def last_full_sweep() -> dict | None:
    """When the last full sweep finished and what it found, however it was
    started (the schedule or POST /api/refresh). None until one has run."""
    return _last_full_sweep


async def stop_full_sweep() -> None:
    """Finish cancellation before shutdown closes the shared HTTP clients."""
    task = _full_sweep
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    intake = list(_intake_tasks.values())
    for task in intake:
        task.cancel()
    if intake:
        await asyncio.gather(*intake, return_exceptions=True)
    _intake_tasks.clear()


async def refresh_offers(offer_ids: list[int] | None = None) -> dict:
    """Re-read every active offer (or just `offer_ids`), then evaluate alert rules.

    A full sweep means "make sure every listing has been re-read now", so a
    second caller while one is in flight joins it instead of starting another:
    after the laptop wakes, the scheduler's catch-up and the host wake hook's
    POST /api/refresh arrive within seconds of each other, and 40 retailers
    do not need to hear from us twice.
    """
    global _full_sweep
    if offer_ids:
        return await _refresh_offers(offer_ids)
    if _full_sweep is not None and not _full_sweep.done():
        return await asyncio.shield(_full_sweep)
    task = asyncio.create_task(_refresh_offers(None))
    _full_sweep = task

    def finished(completed: asyncio.Task) -> None:
        global _full_sweep
        if _full_sweep is completed:
            _full_sweep = None
        # Retrieve failures even if every HTTP caller disconnected during wake.
        if not completed.cancelled() and completed.exception() is not None:
            log.error("background sweep failed: %s", completed.exception())

    task.add_done_callback(finished)
    return await asyncio.shield(task)


async def _refresh_offers(offer_ids: list[int] | None) -> dict:
    def read(session: Session) -> list[tuple[int, int, str]]:
        query = select(Offer.id, Offer.product_id, Offer.url).where(Offer.active.is_(True))
        if offer_ids:
            query = query.where(Offer.id.in_(offer_ids))
        else:
            busy = [pid for pid, task in _intake_tasks.items() if not task.done()]
            if busy:
                query = query.where(Offer.product_id.not_in(busy))
        return [tuple(row) for row in session.execute(query).all()]

    def done(summary: dict) -> dict:
        global _last_full_sweep
        if offer_ids is None:
            _last_full_sweep = {"at": utcnow().isoformat(), "checked": summary["checked"],
                                "updated": summary["updated"], "failed": summary["failed"],
                                "alerts": len(summary["alerts"])}
        return summary

    targets = await in_db(read)

    semaphore = asyncio.Semaphore(_SWEEP_CONCURRENCY)

    async def one(offer_id: int, product_id: int, url: str) -> tuple[int, int, StoreResult]:
        async with semaphore:
            try:
                result = await fetch_offer(url)
            except Exception as exc:
                log.info("offer %s lookup deferred: %s", offer_id, exc)
                result = StoreResult(store=store_key_for_url(url) or host_of(url), url=url,
                                     currency="", error="Retailer lookup is temporarily unavailable")
            return offer_id, product_id, result

    outcomes = await asyncio.gather(*(one(*t) for t in targets), return_exceptions=True)

    updated = failed = 0
    def write(session: Session) -> None:
        nonlocal updated, failed
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                failed += 1
                continue
            offer_id, product_id, result = outcome
            existing = session.get(Offer, offer_id)
            if existing is None:
                continue
            # keep the stored URL stable even if the store redirected us
            result.url = existing.url
            _upsert_offer(session, product_id, result)
            if result.ok:
                updated += 1
                _initial_baselines(session, product_id)
            else:
                failed += 1

    await in_db(write)
    discovered = 0
    if offer_ids is None:
        # An empty search is durable work too. Retry every sweep; revisit
        # resolved name searches daily to find additional retailers.
        def pending_discovery(session: Session) -> list[tuple[int, bool]]:
            pending = []
            for product in session.scalars(select(Product)):
                if not any(t.active for t in product.trackers):
                    continue
                age = (utcnow() - _utc(product.discovery_checked_at)
                       if product.discovery_checked_at else dt.timedelta.max)
                healthy = any(o.active and o.last_price is not None and not o.last_error
                              for o in product.offers)
                name_watch = bool(product.watch_input and not product.watch_input.startswith(("http://", "https://")))
                if name_watch and (age >= dt.timedelta(days=1) or not healthy):
                    pending.append((product.id, False))
                elif (not healthy and age >= dt.timedelta(days=1)
                      and not product.title.startswith(("http://", "https://"))
                      and any((o.consecutive_errors or 0) >= 3 for o in product.offers)):
                    # Existing watches need useful recovery too: keep retrying
                    # their URLs and look for another retailer once a day.
                    pending.append((product.id, True))
            return pending

        pending = await in_db(pending_discovery)
        if pending:
            await asyncio.gather(*(_start_intake(pid, alternatives=alternatives)
                                   for pid, alternatives in pending), return_exceptions=True)
            discovered = len(pending)
    alerts = await evaluate_trackers()
    return done({"checked": len(targets), "updated": updated, "failed": failed,
                 "discovered": discovered, "alerts": alerts})


# ────────────────────────── alerting rules ───────────────────────────────
def _best_offer(session: Session, product_id: int, currency: str | None = None,
                *, fresh_only: bool = False) -> Offer | None:
    """Cheapest live listing — within one currency when asked, because a
    watch's thresholds only mean something against prices in their own money."""
    offers = session.scalars(
        select(Offer).where(Offer.product_id == product_id, Offer.active.is_(True))
    ).all()
    if currency:
        offers = [o for o in offers if (o.currency or "").upper() == currency.upper()]
    if fresh_only:
        offers = [o for o in offers if not o.last_error and o.in_stock is not False]
    priced = [o for o in offers if o.last_price is not None and o.in_stock is not False]
    if not priced:
        priced = [o for o in offers if o.last_price is not None]
    return min(priced, key=lambda o: o.last_price) if priced else None


def _tracker_currency(session: Session, tracker: Tracker) -> str | None:
    """The money a watch's thresholds are in. Rows from before watches carried
    one fall back to the user's preference, then to the cheapest listing's."""
    if tracker.currency:
        return tracker.currency.upper()
    preferred = preferences.preferred_currency()
    if preferred:
        return preferred
    best = _best_offer(session, tracker.product_id)
    return (best.currency or "").upper() or None if best else None


def _reasons(tracker: Tracker, price: Decimal, currency: str | None = None) -> list[str]:
    currency = currency or tracker.currency or "USD"
    hits = []
    if tracker.target_price is not None and price <= tracker.target_price:
        hits.append(f"at or below your target of {fmt(tracker.target_price, currency)}")
    if tracker.drop_pct and tracker.baseline_price:
        threshold = tracker.baseline_price * (Decimal(1) - Decimal(str(tracker.drop_pct)) / 100)
        if price <= threshold:
            pct = (1 - price / tracker.baseline_price) * 100
            hits.append(f"down {pct:.1f}% from {fmt(tracker.baseline_price, currency)} "
                        f"(you asked for {tracker.drop_pct:g}%)")
    return hits


def _utc(value: dt.datetime) -> dt.datetime:
    """SQLite hands timezone-aware columns back naive; they were written in UTC."""
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def _should_notify(tracker: Tracker, price: Decimal) -> bool:
    """Respect the cooldown, unless the price has fallen further since last time.

    Once the cooldown is over a repeat still needs the price to have moved:
    the same figure every twelve hours is not news, and it is exactly what
    went out twice for one unchanged listing.
    """
    if tracker.last_notified_at is None or tracker.last_notified_price is None:
        return True
    if price < tracker.last_notified_price:
        return True
    cooled = utcnow() - _utc(tracker.last_notified_at) >= dt.timedelta(hours=tracker.cooldown_hours)
    return cooled and price != tracker.last_notified_price


def _restocked_offer(session: Session, tracker: Tracker, currency: str | None) -> Offer | None:
    """Cheapest listing in the watch's currency that came back in stock since
    the watch last said so.

    A restock is an event, not a level, so there is no cooldown to respect:
    the watermark is the last restock told, and the next alert needs a later
    out→in transition. A foreign listing never counts, for the same reason
    it never trips a price threshold.
    """
    offers = session.scalars(
        select(Offer).where(Offer.product_id == tracker.product_id, Offer.active.is_(True))
    ).all()
    since = tracker.last_restock_notified_at
    fresh = [
        o for o in offers
        if o.in_stock is True and o.last_restocked_at is not None and o.last_price is not None
        and not o.last_error
        and (not currency or (o.currency or "").upper() == currency.upper())
        and (since is None or _utc(o.last_restocked_at) > _utc(since))
    ]
    return min(fresh, key=lambda o: o.last_price) if fresh else None


def _pending(kind: str, tracker: Tracker, product: Product | None, offer: Offer,
             reasons: list[str]) -> dict:
    """Everything the send loop needs, detached from the session it came from."""
    return {
        "kind": kind, "tracker_id": tracker.id, "offer_id": offer.id,
        "label": tracker.label or (product.title if product else ""),
        "title": product.title if product else tracker.label,
        "price": offer.last_price, "currency": offer.currency,
        "store": offer.store, "url": offer.url,
        "baseline": tracker.baseline_price,
        "reasons": reasons, "channels": tracker.channels,
        "deal_note": offer.deal_note,
    }


_evaluation_lock: asyncio.Lock | None = None
_evaluation_loop: asyncio.AbstractEventLoop | None = None


async def evaluate_trackers() -> list[dict]:
    """Serialize decisions and delivery so overlapping refreshes cannot double-send."""
    global _evaluation_lock, _evaluation_loop
    loop = asyncio.get_running_loop()
    if _evaluation_loop is not loop:
        _evaluation_loop, _evaluation_lock = loop, asyncio.Lock()
    async with _evaluation_lock:
        return await _evaluate_trackers()


async def _evaluate_trackers() -> list[dict]:
    """Find trackers whose conditions are met and send their notifications.

    A watch can carry a price rule and a restock rule at once. Each fires on
    its own terms and comes back as its own entry, tagged by `kind`.
    """
    def read(session: Session) -> list[dict]:
        pending = []
        for tracker in session.scalars(select(Tracker).where(Tracker.active.is_(True))).all():
            # Only listings in the watch's own currency are measured against
            # its thresholds. A cheaper foreign listing is not a drop.
            currency = _tracker_currency(session, tracker)
            product = session.get(Product, tracker.product_id)
            offer = _best_offer(session, tracker.product_id, currency, fresh_only=True)
            if offer is not None and offer.last_price is not None:
                hits = _reasons(tracker, offer.last_price, currency)
                if hits and _should_notify(tracker, offer.last_price):
                    pending.append(_pending("price", tracker, product, offer, hits))
            if tracker.alert_on_restock:
                restocked = _restocked_offer(session, tracker, currency)
                if restocked is not None:
                    # A price condition the returned listing also meets is
                    # worth a line: "back, and under your target" is the answer.
                    pending.append(_pending(
                        "restock", tracker, product, restocked,
                        _reasons(tracker, restocked.last_price, currency)))
        return pending

    pending = await in_db(read)
    sent: list[dict] = []

    for item in pending:
        only = [c.strip() for c in item["channels"].split(",") if c.strip()] or None
        restock = item["kind"] == "restock"
        amount = fmt(item["price"], item["currency"])
        headline = (f"**back in stock** at **{item['store']}** — {amount}" if restock
                    else f"**{amount}** at **{item['store']}**")
        # The leading reason doubles as the audit trail's kind marker.
        reasons = ([f"back in stock at {item['store']}"] if restock else []) + item["reasons"]
        # The note rides along: "CA$49.99" without "after the coupon — clip
        # it" sends the user to a page whose sticker still says CA$167.73.
        lines = [headline, *(f"• {r}" for r in item["reasons"])]
        if item.get("deal_note"):
            lines.append(f"• {item['deal_note']}")
        alert = Alert(
            title=f"{'📦' if restock else '💸'} {item['title'][:120]}",
            body="\n".join(lines),
            url=item["url"],
            price=float(item["price"]),
            old_price=float(item["baseline"]) if item["baseline"] else None,
            currency=item["currency"],
            store=item["store"],
        )
        outcome = await dispatch(alert, only=only)
        delivered = any(v == "sent" for v in outcome.values())
        # The cooldown starts only once a channel has taken the message. When
        # every configured channel failed (DNS not back after a laptop resume,
        # ntfy down) the watch stays un-notified, so the next sweep sends the
        # alert again instead of sitting out twelve hours on a push nobody got.
        # With no channel configured there is nothing to retry into, so the
        # stamp still goes on — otherwise every sweep would re-record the hit.
        stamp = delivered or not outcome

        def write(session: Session, item=item, reasons=reasons, outcome=outcome,
                  delivered=delivered, stamp=stamp) -> None:
            tracker = session.get(Tracker, item["tracker_id"])
            if tracker is None:
                return
            if stamp and item["kind"] == "restock":
                tracker.last_restock_notified_at = utcnow()
            elif stamp:
                tracker.last_notified_at = utcnow()
                tracker.last_notified_price = item["price"]
            session.add(AlertEvent(
                tracker_id=tracker.id, offer_id=item["offer_id"], price=item["price"],
                reason="; ".join(reasons)[:300],
                channels=",".join(outcome.keys()), delivered=delivered,
                detail="; ".join(f"{k}={v}" for k, v in outcome.items())[:2000],
            ))

        await in_db(write)
        sent.append({"kind": item["kind"], "tracker_id": item["tracker_id"],
                     "title": item["title"], "price": float(item["price"]),
                     "store": item["store"], "reasons": reasons, "channels": outcome,
                     "deal_note": item.get("deal_note")})
        if not delivered:
            log.warning("tracker %s matched but no channel delivered: %s%s",
                        item["tracker_id"], outcome,
                        "; will retry on the next sweep" if outcome else "")
    return sent


# ─────────────────────────────── queries ─────────────────────────────────
def _offer_dict(offer: Offer) -> dict:
    return {"id": offer.id, "store": offer.store, "url": offer.url, "title": offer.title,
            "price": float(offer.last_price) if offer.last_price is not None else None,
            "lowest": float(offer.lowest_price) if offer.lowest_price is not None else None,
            "currency": offer.currency, "in_stock": offer.in_stock, "method": offer.method,
            "last_checked": offer.last_checked_at.isoformat() if offer.last_checked_at else None,
            "stock_changed_at": offer.stock_changed_at.isoformat() if offer.stock_changed_at else None,
            "last_restocked_at": offer.last_restocked_at.isoformat() if offer.last_restocked_at else None,
            "deal_note": offer.deal_note,
            "error": offer.last_error,
            "consecutive_errors": offer.consecutive_errors}


async def list_tracked() -> list[dict]:
    def read(session: Session) -> list[dict]:
        rows = []
        for tracker in session.scalars(select(Tracker).order_by(Tracker.id)).all():
            product = session.get(Product, tracker.product_id)
            offers = session.scalars(
                select(Offer).where(Offer.product_id == tracker.product_id)).all()
            currency = _tracker_currency(session, tracker)
            best = (_best_offer(session, tracker.product_id, currency, fresh_only=True)
                    or _best_offer(session, tracker.product_id, currency))
            foreign = [o for o in offers
                       if currency and (o.currency or "").upper() != currency]
            # Same one-currency rule as the alert: a foreign listing that is
            # sold out is not something this watch will ever report back.
            out_of_stock = [o for o in offers
                            if o.in_stock is False and o not in foreign]
            status, status_note = _watch_status(tracker, product, offers)
            row = {
                "tracker_id": tracker.id, "product_id": tracker.product_id,
                "label": tracker.label, "title": product.title if product else None,
                "active": tracker.active,
                "status": status, "status_note": status_note,
                "watch_input": product.watch_input,
                "discovery_checked_at": (product.discovery_checked_at.isoformat()
                                         if product.discovery_checked_at else None),
                "discovery_error": product.discovery_error,
                "currency": currency,
                "target_price": float(tracker.target_price) if tracker.target_price else None,
                "drop_pct": tracker.drop_pct,
                "baseline_price": float(tracker.baseline_price) if tracker.baseline_price else None,
                "alert_on_restock": bool(tracker.alert_on_restock),
                "current_best": (
                    {"price": float(best.last_price), "currency": best.currency,
                     "store": best.store, "url": best.url,
                     "stale": bool(best.last_error),
                     "last_checked": best.last_checked_at.isoformat() if best.last_checked_at else None,
                     **({"deal_note": best.deal_note} if best.deal_note else {})}
                    if best and best.last_price is not None else None),
                "out_of_stock_listings": len(out_of_stock),
                "channels": tracker.channels or "all configured",
                "last_notified_at": (tracker.last_notified_at.isoformat()
                                     if tracker.last_notified_at else None),
                "last_restock_notified_at": (tracker.last_restock_notified_at.isoformat()
                                             if tracker.last_restock_notified_at else None),
                "offers": [_offer_dict(o) for o in offers],
            }
            if foreign:
                row["foreign_listings"] = len(foreign)
                row["note"] = (f"{len(foreign)} listing(s) bill in another currency than "
                               f"this watch ({currency}); shown for reference, never "
                               f"measured against its thresholds")
            rows.append(row)
        return rows
    return await in_db(read)


async def price_history(product_id: int, limit: int = 200) -> dict:
    def read(session: Session) -> dict:
        product = session.get(Product, product_id)
        if product is None:
            return {"ok": False, "error": f"no product {product_id}"}
        points = session.scalars(
            select(PricePoint).join(Offer).where(Offer.product_id == product_id)
            .order_by(PricePoint.observed_at.desc()).limit(limit)
        ).all()

        # "Is this actually a good deal?" deserves a direct answer, not a
        # list of numbers. Stats stick to one currency so a CAD point never
        # skews a USD average.
        best = _best_offer(session, product_id)
        summary = None

        def distinct(subset: list[PricePoint]) -> list[Decimal]:
            """One observation per price change per offer. A stock-only point
            repeats the price it was recorded at, and counting it would pad
            the average and the observation count with non-events."""
            last: dict[int, Decimal] = {}
            prices = []
            for p in subset:
                if last.get(p.offer_id) != p.price:
                    prices.append(p.price)
                last[p.offer_id] = p.price
            return prices

        if points:
            currency = best.currency if best else points[0].currency
            relevant = distinct([p for p in points if p.currency == currency]) \
                or distinct(points)
            current = (best.last_price if best and best.last_price is not None
                       else relevant[0])
            lowest, highest = min(relevant), max(relevant)
            average = sum(relevant) / len(relevant)
            if current <= lowest * Decimal("1.02"):
                verdict = "at or near the lowest recorded price"
            elif current <= average:
                verdict = "below the recorded average"
            else:
                verdict = "above the recorded average"
            summary = {
                "current_best": float(current), "currency": currency,
                "lowest_seen": float(lowest), "highest_seen": float(highest),
                "average": round(float(average), 2),
                "observations": len(relevant),
                "verdict": verdict,
            }

        return {"ok": True, "product_id": product_id, "title": product.title,
                "summary": summary,
                "points": [{"offer_id": p.offer_id, "price": float(p.price),
                            "currency": p.currency, "in_stock": p.in_stock,
                            "at": p.observed_at.isoformat()} for p in points]}
    return await in_db(read)


async def list_alerts(limit: int = 30) -> dict:
    """Recent fired alerts, newest first — the audit trail for 'did it work?'."""
    def read(session: Session) -> list[dict]:
        events = session.scalars(
            select(AlertEvent).order_by(AlertEvent.id.desc()).limit(limit)).all()
        rows = []
        for event in events:
            tracker = session.get(Tracker, event.tracker_id)
            offer = session.get(Offer, event.offer_id) if event.offer_id else None
            if event.reason.startswith("agent:"):
                kind = "agent"
            elif event.reason.startswith("back in stock"):
                kind = "restock"
            else:
                kind = "price"
            rows.append({
                "id": event.id,
                "tracker_id": event.tracker_id,
                "label": tracker.label if tracker else None,
                "kind": kind,
                # An agent note need not be about a figure; zero there is "none".
                "price": (float(event.price)
                          if kind != "agent" or event.price else None),
                "reason": event.reason,
                "store": offer.store if offer else None,
                "url": offer.url if offer else None,
                "channels_tried": event.channels,
                "delivered": event.delivered,
                "detail": event.detail,
                "at": event.created_at.isoformat(),
            })
        return rows
    return {"alerts": await in_db(read)}


async def push_note(title: str, message: str, *, url: str = "", tracker_id: int | None = None,
                    price: float | None = None, currency: str = "",
                    channels: str = "") -> dict:
    """A notification the agent authored, sent the way the engine's own alerts
    are and, when it concerns a watch, recorded beside them.

    The scheduled Reddit sweeps used to push through a hand-typed curl to
    ntfy. That worked, but nothing recorded it, so three days after a coupon
    deal nobody could say what had been sent, or when. Now the same channels,
    the same audit trail: list_alerts shows these as kind "agent".
    """
    title = " ".join(title.split())[:200]
    message = message.strip()
    if not title or not message:
        return {"ok": False, "error": "title and message are both required"}
    only = [c.strip() for c in channels.split(",") if c.strip()] or None
    alert = Alert(
        title=f"🛎️ {title}",
        body=message[:3500],
        url=url.strip() or None,
        price=price,
        currency=(currency or preferences.preferred_currency() or "USD").upper(),
    )
    outcome = await dispatch(alert, only=only)
    delivered = any(v == "sent" for v in outcome.values())

    recorded = False
    if tracker_id is not None:
        def write(session: Session) -> bool:
            tracker = session.get(Tracker, tracker_id)
            if tracker is None:
                return False
            session.add(AlertEvent(
                tracker_id=tracker.id, offer_id=None,
                price=Decimal(str(price)) if price is not None else Decimal(0),
                reason=f"agent: {title}"[:300],
                channels=",".join(outcome.keys()), delivered=delivered,
                detail=("; ".join(f"{k}={v}" for k, v in outcome.items())
                        + "\n" + message)[:2000],
            ))
            return True
        recorded = await in_db(write)

    out = {"ok": delivered, "delivered": delivered, "channels": outcome,
           "recorded_on_tracker": tracker_id if recorded else None}
    if not outcome:
        out["error"] = ("no notification channel is configured — set NTFY_TOPIC (or "
                        "Discord/Signal/WhatsApp) in .env; nothing reached the user")
    elif not delivered:
        out["error"] = "every configured channel failed; see channels for the reason"
    if tracker_id is not None and not recorded:
        out["note"] = f"tracker {tracker_id} does not exist, so nothing was recorded"
    return out


async def set_tracker(tracker_id: int, **changes) -> dict:
    def write(session: Session) -> dict:
        tracker = session.get(Tracker, tracker_id)
        if tracker is None:
            return {"ok": False, "error": f"no tracker {tracker_id}"}
        for field in ("target_price", "drop_pct", "alert_on_restock", "channels",
                      "cooldown_hours", "active", "label"):
            if field in changes and (changes[field] is not None or field in {"target_price", "drop_pct"}):
                value = changes[field]
                if field == "target_price" and value is not None:
                    value = Decimal(str(value))
                if field == "alert_on_restock" and value and not tracker.alert_on_restock:
                    # Switching the rule on starts the clock now. A listing that
                    # came back last week and is still in stock is not news the
                    # user asked for; only a return after this moment is.
                    tracker.last_restock_notified_at = utcnow()
                setattr(tracker, field, value)
        if changes.get("reset_baseline"):
            best = _best_offer(session, tracker.product_id, _tracker_currency(session, tracker))
            if best and best.last_price is not None:
                tracker.baseline_price = best.last_price
        return {"ok": True, "tracker_id": tracker.id}
    return await in_db(write)


async def backfill_tracker_currency() -> int:
    """Give watches created before they carried a currency the one their
    thresholds were written in.

    That is the listing the watch was created from — the offer whose price is
    the baseline — then the user's preferred currency, then whatever the
    cheapest listing bills in. Runs at start-up; a no-op once every row is set.
    """
    def write(session: Session) -> int:
        fixed = 0
        for tracker in session.scalars(select(Tracker).where(Tracker.currency.is_(None))).all():
            product = session.get(Product, tracker.product_id)
            if product and product.watch_input and tracker.baseline_price is None:
                continue  # queued watches adopt a currency on the first real read
            offers = session.scalars(select(Offer).where(
                Offer.product_id == tracker.product_id).order_by(Offer.id)).all()
            currency = None
            if tracker.baseline_price is not None:
                currency = next((o.currency for o in offers if tracker.baseline_price in
                                 (o.last_price, o.lowest_price)), None)
            if not currency:
                currency = preferences.preferred_currency()
            if not currency and offers:
                currency = min(offers, key=lambda o: usd_sort_key(o.last_price, o.currency)).currency
            if currency:
                tracker.currency = currency.upper()
                fixed += 1
        return fixed
    return await in_db(write)


async def delete_tracker(tracker_id: int) -> dict:
    def write(session: Session) -> dict:
        tracker = session.get(Tracker, tracker_id)
        if tracker is None:
            return {"ok": False, "error": f"no tracker {tracker_id}"}
        session.delete(tracker)
        return {"ok": True, "deleted": tracker_id}
    return await in_db(write)


async def compare(query: str, stores: list[str] | None = None, limit_per_store: int = 3,
                  threshold: float = 65.0, country: str | None = None) -> dict:
    """One-shot price comparison — no tracking, just what it costs right now.

    `country` is the shortcut for "only my own market": without an explicit
    `stores` list it expands to every catalogued store selling there, so a CAD
    shopper is not handed eight American stores and a conversion each.
    """
    if country and not stores:
        stores = keys_for_country(country)
        if not stores:
            return {"query": query, "count": 0, "results": [], "stores_searched": [],
                    "note": f"no catalogued store sells in {country.strip().upper()} — "
                            "list_stores shows each store's country"}
    ranked, _, query_used = await _search_ranked(
        query, stores=stores, limit_per_store=limit_per_store, threshold=threshold)
    preferred = preferences.preferred_currency()
    out = {"query": query, "search_terms_used": query_used, "count": len(ranked),
           "results": [r.as_dict() for r in ranked],
           "stores_searched": stores or sorted(ADAPTERS)}
    currencies = {r.currency for r in ranked if r.currency}
    if preferred:
        out["preferred_currency"] = preferred
        out["results_in_preferred_currency"] = sum(
            1 for r in ranked if (r.currency or "").upper() == preferred)
    if len(currencies) > 1:
        note = (f"results span {', '.join(sorted(currencies))}; ordering uses an "
                "indicative conversion — always quote each price in its own currency")
        if preferred:
            note += (f". Lead with the {preferred} listings and say plainly when a "
                     f"store bills in something else")
        out["note"] = note
    return out
