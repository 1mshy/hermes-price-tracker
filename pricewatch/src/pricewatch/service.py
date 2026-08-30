"""Application logic: register products, refresh prices, decide when to shout."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import matching, preferences
from .db import in_db, session_scope
from .models import AlertEvent, Offer, PricePoint, Product, Tracker, utcnow
from .money import fmt
from .notify import Alert, dispatch
from .stores.base import StoreResult
from .stores.registry import ADAPTERS, fetch_offer, search_stores, store_key_for_url

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


# ─────────────────────────── registration ────────────────────────────────
async def track_url(url: str, *, target_price: float | None = None, drop_pct: float | None = None,
                    label: str = "", channels: str = "", cooldown_hours: int = 12) -> dict:
    """Start tracking a single product URL."""
    result = await fetch_offer(url)
    if not result.ok:
        return {"ok": False, "error": result.error or "could not read a price", "url": url,
                "store": result.store, "method": result.method}

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
            channels=channels,
            cooldown_hours=cooldown_hours,
        )
        session.add(tracker)
        session.flush()
        return {"ok": True, "product_id": product.id, "tracker_id": tracker.id,
                "offer_id": offer.id, "title": product.title,
                "price": float(result.price), "currency": result.currency,
                "store": result.store, "method": result.method}

    return await in_db(write)


async def track_query(description: str, *, stores: list[str] | None = None,
                      target_price: float | None = None, drop_pct: float | None = None,
                      max_offers: int = 8, threshold: float = 70.0,
                      channels: str = "", cooldown_hours: int = 12) -> dict:
    """Search stores for a described product and track every plausible match."""
    ranked, searched, query_used = await _search_ranked(
        description, stores=stores, limit_per_store=3, threshold=threshold)
    if not ranked:
        return {"ok": False, "error": "no store listing matched that description",
                "searched": searched, "search_terms_tried": query_used}

    keep = [r for r in ranked if _is_trackable(r.store)][:max_offers]
    if not keep:
        return {"ok": False,
                "error": ("matches found only on search-only stores that cannot be "
                          "tracked — compare_prices shows their current prices; "
                          "track a specific URL at a supported store instead"),
                "matches": [r.as_dict() for r in ranked[:5]],
                "search_terms_tried": query_used}
    cheapest = min(keep, key=lambda r: r.price)

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
            channels=channels,
            cooldown_hours=cooldown_hours,
        )
        session.add(tracker)
        session.flush()
        return {"ok": True, "product_id": product.id, "tracker_id": tracker.id,
                "title": product.title, "offers": [
                    {"store": o.store, "url": o.url, "price": float(o.last_price or 0)}
                    for o in offers
                ],
                "best_price": float(cheapest.price), "best_store": cheapest.store,
                "search_terms_used": query_used}

    return await in_db(write)


async def add_offer(product_id: int, url: str) -> dict:
    """Attach another store's listing to an existing tracked product."""
    result = await fetch_offer(url)
    if not result.ok:
        return {"ok": False, "error": result.error or "could not read a price", "url": url}

    def write(session: Session) -> dict:
        product = session.get(Product, product_id)
        if product is None:
            return {"ok": False, "error": f"no product {product_id}"}
        offer = _upsert_offer(session, product_id, result)
        return {"ok": True, "offer_id": offer.id, "store": offer.store,
                "price": float(result.price), "product": product.title}

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
    """Insert-or-update an offer and append a price point when the price moved."""
    offer = session.scalar(select(Offer).where(Offer.url == result.url))
    if offer is None:
        offer = Offer(product_id=product_id, store=result.store, url=result.url)
        session.add(offer)

    previous = offer.last_price
    offer.title = result.title or offer.title
    offer.sku = result.sku or offer.sku
    offer.currency = result.currency or offer.currency
    offer.in_stock = result.in_stock
    offer.method = result.method
    offer.last_checked_at = utcnow()

    if result.price is not None:
        offer.last_price = result.price
        offer.lowest_price = (result.price if offer.lowest_price is None
                              else min(offer.lowest_price, result.price))
        offer.last_error = None
        offer.consecutive_errors = 0
        if previous is None or previous != result.price:
            session.add(PricePoint(offer=offer, price=result.price,
                                   currency=offer.currency, in_stock=result.in_stock))
    else:
        offer.last_error = result.error
        offer.consecutive_errors += 1

    session.flush()
    return offer


# ───────────────────────────── refreshing ────────────────────────────────
async def refresh_offers(offer_ids: list[int] | None = None) -> dict:
    """Re-read every active offer, then evaluate alert rules."""
    def read(session: Session) -> list[tuple[int, int, str]]:
        query = select(Offer.id, Offer.product_id, Offer.url).where(Offer.active.is_(True))
        if offer_ids:
            query = query.where(Offer.id.in_(offer_ids))
        return [tuple(row) for row in session.execute(query).all()]

    targets = await in_db(read)
    if not targets:
        return {"checked": 0, "updated": 0, "failed": 0, "alerts": []}

    semaphore = asyncio.Semaphore(_SWEEP_CONCURRENCY)

    async def one(offer_id: int, product_id: int, url: str) -> tuple[int, int, StoreResult]:
        async with semaphore:
            return offer_id, product_id, await fetch_offer(url)

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
            else:
                failed += 1

    await in_db(write)
    alerts = await evaluate_trackers()
    return {"checked": len(targets), "updated": updated, "failed": failed, "alerts": alerts}


# ────────────────────────── alerting rules ───────────────────────────────
def _best_offer(session: Session, product_id: int) -> Offer | None:
    offers = session.scalars(
        select(Offer).where(Offer.product_id == product_id, Offer.active.is_(True))
    ).all()
    priced = [o for o in offers if o.last_price is not None and o.in_stock is not False]
    if not priced:
        priced = [o for o in offers if o.last_price is not None]
    return min(priced, key=lambda o: o.last_price) if priced else None


def _reasons(tracker: Tracker, price: Decimal) -> list[str]:
    hits = []
    if tracker.target_price is not None and price <= tracker.target_price:
        hits.append(f"at or below your target of {fmt(tracker.target_price)}")
    if tracker.drop_pct and tracker.baseline_price:
        threshold = tracker.baseline_price * (Decimal(1) - Decimal(str(tracker.drop_pct)) / 100)
        if price <= threshold:
            pct = (1 - price / tracker.baseline_price) * 100
            hits.append(f"down {pct:.1f}% from {fmt(tracker.baseline_price)} "
                        f"(you asked for {tracker.drop_pct:g}%)")
    return hits


def _should_notify(tracker: Tracker, price: Decimal) -> bool:
    """Respect the cooldown, unless the price has fallen further since last time."""
    if tracker.last_notified_at is None:
        return True
    last_at = tracker.last_notified_at
    if last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=dt.timezone.utc)
    if utcnow() - last_at >= dt.timedelta(hours=tracker.cooldown_hours):
        return True
    return tracker.last_notified_price is not None and price < tracker.last_notified_price


async def evaluate_trackers() -> list[dict]:
    """Find trackers whose conditions are met and send their notifications."""
    def read(session: Session) -> list[dict]:
        pending = []
        for tracker in session.scalars(select(Tracker).where(Tracker.active.is_(True))).all():
            offer = _best_offer(session, tracker.product_id)
            if offer is None or offer.last_price is None:
                continue
            hits = _reasons(tracker, offer.last_price)
            if not hits or not _should_notify(tracker, offer.last_price):
                continue
            product = session.get(Product, tracker.product_id)
            pending.append({
                "tracker_id": tracker.id, "offer_id": offer.id,
                "label": tracker.label or (product.title if product else ""),
                "title": product.title if product else tracker.label,
                "price": offer.last_price, "currency": offer.currency,
                "store": offer.store, "url": offer.url,
                "baseline": tracker.baseline_price,
                "reasons": hits, "channels": tracker.channels,
            })
        return pending

    pending = await in_db(read)
    sent: list[dict] = []

    for item in pending:
        only = [c.strip() for c in item["channels"].split(",") if c.strip()] or None
        alert = Alert(
            title=f"💸 {item['title'][:120]}",
            body=(f"**{fmt(item['price'], item['currency'])}** at **{item['store']}**\n"
                  + "\n".join(f"• {r}" for r in item["reasons"])),
            url=item["url"],
            price=float(item["price"]),
            old_price=float(item["baseline"]) if item["baseline"] else None,
            currency=item["currency"],
            store=item["store"],
        )
        outcome = await dispatch(alert, only=only)
        delivered = any(v == "sent" for v in outcome.values())

        def write(session: Session, item=item, outcome=outcome, delivered=delivered) -> None:
            tracker = session.get(Tracker, item["tracker_id"])
            if tracker is None:
                return
            tracker.last_notified_at = utcnow()
            tracker.last_notified_price = item["price"]
            session.add(AlertEvent(
                tracker_id=tracker.id, offer_id=item["offer_id"], price=item["price"],
                reason="; ".join(item["reasons"])[:300],
                channels=",".join(outcome.keys()), delivered=delivered,
                detail="; ".join(f"{k}={v}" for k, v in outcome.items())[:2000],
            ))

        await in_db(write)
        sent.append({"tracker_id": item["tracker_id"], "title": item["title"],
                     "price": float(item["price"]), "store": item["store"],
                     "reasons": item["reasons"], "channels": outcome})
        if not delivered:
            log.warning("tracker %s matched but no channel delivered: %s",
                        item["tracker_id"], outcome)
    return sent


# ─────────────────────────────── queries ─────────────────────────────────
def _offer_dict(offer: Offer) -> dict:
    return {"id": offer.id, "store": offer.store, "url": offer.url, "title": offer.title,
            "price": float(offer.last_price) if offer.last_price is not None else None,
            "lowest": float(offer.lowest_price) if offer.lowest_price is not None else None,
            "currency": offer.currency, "in_stock": offer.in_stock, "method": offer.method,
            "last_checked": offer.last_checked_at.isoformat() if offer.last_checked_at else None,
            "error": offer.last_error,
            "consecutive_errors": offer.consecutive_errors}


async def list_tracked() -> list[dict]:
    def read(session: Session) -> list[dict]:
        rows = []
        for tracker in session.scalars(select(Tracker).order_by(Tracker.id)).all():
            product = session.get(Product, tracker.product_id)
            offers = session.scalars(
                select(Offer).where(Offer.product_id == tracker.product_id)).all()
            best = _best_offer(session, tracker.product_id)
            rows.append({
                "tracker_id": tracker.id, "product_id": tracker.product_id,
                "label": tracker.label, "title": product.title if product else None,
                "active": tracker.active,
                "target_price": float(tracker.target_price) if tracker.target_price else None,
                "drop_pct": tracker.drop_pct,
                "baseline_price": float(tracker.baseline_price) if tracker.baseline_price else None,
                "current_best": (
                    {"price": float(best.last_price), "store": best.store, "url": best.url}
                    if best and best.last_price is not None else None),
                "channels": tracker.channels or "all configured",
                "last_notified_at": (tracker.last_notified_at.isoformat()
                                     if tracker.last_notified_at else None),
                "offers": [_offer_dict(o) for o in offers],
            })
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
        if points:
            currency = best.currency if best else points[0].currency
            relevant = [p.price for p in points if p.currency == currency] \
                or [p.price for p in points]
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
            rows.append({
                "id": event.id,
                "tracker_id": event.tracker_id,
                "label": tracker.label if tracker else None,
                "price": float(event.price),
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


async def set_tracker(tracker_id: int, **changes) -> dict:
    def write(session: Session) -> dict:
        tracker = session.get(Tracker, tracker_id)
        if tracker is None:
            return {"ok": False, "error": f"no tracker {tracker_id}"}
        for field in ("target_price", "drop_pct", "channels", "cooldown_hours", "active", "label"):
            if field in changes and changes[field] is not None:
                value = changes[field]
                if field == "target_price":
                    value = Decimal(str(value))
                setattr(tracker, field, value)
        if changes.get("reset_baseline"):
            best = _best_offer(session, tracker.product_id)
            if best and best.last_price is not None:
                tracker.baseline_price = best.last_price
        return {"ok": True, "tracker_id": tracker.id}
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
                  threshold: float = 65.0) -> dict:
    """One-shot price comparison — no tracking, just what it costs right now."""
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
