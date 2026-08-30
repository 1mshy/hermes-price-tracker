import asyncio
import datetime as dt
from decimal import Decimal

from pricewatch import service
from pricewatch.db import init_db, session_scope
from pricewatch.models import AlertEvent, Offer, Product, Tracker, utcnow
from pricewatch.stores.base import StoreResult


def _tracker(**kw):
    defaults = dict(product_id=1, label="t", target_price=None, drop_pct=None,
                    baseline_price=None, channels="", cooldown_hours=12,
                    active=True, last_notified_at=None, last_notified_price=None)
    defaults.update(kw)
    return Tracker(**defaults)


# ── alert rules ──────────────────────────────────────────────────────────
def test_target_price_reason():
    tracker = _tracker(target_price=Decimal("50"))
    assert service._reasons(tracker, Decimal("49.99"))
    assert not service._reasons(tracker, Decimal("50.01"))


def test_drop_pct_reason():
    tracker = _tracker(drop_pct=15.0, baseline_price=Decimal("100"))
    assert service._reasons(tracker, Decimal("84.00"))
    assert not service._reasons(tracker, Decimal("90.00"))


def test_both_conditions_both_reported():
    tracker = _tracker(target_price=Decimal("90"), drop_pct=10.0,
                       baseline_price=Decimal("100"))
    assert len(service._reasons(tracker, Decimal("85"))) == 2


def test_cooldown_blocks_repeat_at_same_price():
    tracker = _tracker(target_price=Decimal("50"),
                       last_notified_at=utcnow(),
                       last_notified_price=Decimal("45"))
    assert not service._should_notify(tracker, Decimal("45"))


def test_cooldown_bypassed_when_price_falls_further():
    tracker = _tracker(target_price=Decimal("50"),
                       last_notified_at=utcnow(),
                       last_notified_price=Decimal("45"))
    assert service._should_notify(tracker, Decimal("44"))


def test_cooldown_expiry_allows_repeat():
    tracker = _tracker(target_price=Decimal("50"),
                       last_notified_at=utcnow() - dt.timedelta(hours=13),
                       last_notified_price=Decimal("45"))
    assert service._should_notify(tracker, Decimal("45"))


def test_naive_last_notified_treated_as_utc():
    tracker = _tracker(target_price=Decimal("50"),
                       last_notified_at=dt.datetime.utcnow(),   # naive on purpose
                       last_notified_price=Decimal("45"))
    assert not service._should_notify(tracker, Decimal("45"))


# ── offer upserts & price history ───────────────────────────────────────
def _result(price, url="https://example.com/products/x"):
    return StoreResult(store="example", url=url, title="Example Thing",
                       price=Decimal(str(price)), currency="USD",
                       in_stock=True, method="test")


def test_upsert_records_price_points_only_on_change():
    init_db()
    with session_scope() as session:
        product = Product(title="Example Thing", match_key="example thing")
        session.add(product)
        session.flush()
        pid = product.id
        url = f"https://example.com/products/change-{pid}"

        offer = service._upsert_offer(session, pid, _result(100, url))
        assert len(offer.points) == 1
        offer = service._upsert_offer(session, pid, _result(100, url))
        assert len(offer.points) == 1          # unchanged price → no new point
        offer = service._upsert_offer(session, pid, _result(90, url))
        assert len(offer.points) == 2
        assert offer.lowest_price == Decimal("90")


def test_offer_dict_exposes_error_streak():
    offer = Offer(product_id=1, store="example", url="https://example.com/x",
                  currency="USD", last_price=Decimal("10"),
                  last_error="bot challenge", consecutive_errors=4, active=True)
    row = service._offer_dict(offer)
    assert row["consecutive_errors"] == 4
    assert row["error"] == "bot challenge"


def test_upsert_failure_keeps_last_price_and_counts_errors():
    init_db()
    with session_scope() as session:
        product = Product(title="Example Thing", match_key="example thing")
        session.add(product)
        session.flush()
        url = f"https://example.com/products/fail-{product.id}"
        service._upsert_offer(session, product.id, _result(100, url))
        bad = StoreResult(store="example", url=url, error="bot challenge")
        offer = service._upsert_offer(session, product.id, bad)
        assert offer.last_price == Decimal("100")
        assert offer.consecutive_errors == 1
        assert offer.last_error == "bot challenge"


# ── alert history listing ───────────────────────────────────────────────
def test_list_alerts_round_trip():
    init_db()
    with session_scope() as session:
        product = Product(title="Alerty", match_key="alerty")
        session.add(product)
        session.flush()
        offer = Offer(product_id=product.id, store="example",
                      url=f"https://example.com/products/alert-{product.id}",
                      currency="USD", last_price=Decimal("42"),
                      consecutive_errors=0, active=True)
        tracker = _tracker(product_id=product.id, label="Alerty watch",
                           target_price=Decimal("45"))
        session.add_all([offer, tracker])
        session.flush()
        session.add(AlertEvent(tracker_id=tracker.id, offer_id=offer.id,
                               price=Decimal("42"), reason="below target",
                               channels="discord", delivered=False,
                               detail="discord=error: not configured"))

    out = asyncio.run(service.list_alerts(limit=5))
    top = out["alerts"][0]
    assert top["label"] == "Alerty watch"
    assert top["price"] == 42.0
    assert top["delivered"] is False
    assert top["store"] == "example"


# ── condition guards on what gets tracked ────────────────────────────────
def _offer(store, price, title, **extra):
    return StoreResult(store=store, url=f"https://{store}.example/dp/{price}", title=title,
                       price=Decimal(str(price)), currency="CAD", in_stock=True, extra=extra)


def _track(monkeypatch, results):
    init_db()

    async def ranked(query, **kwargs):
        return results, len(results), query

    monkeypatch.setattr(service, "_search_ranked", ranked)
    return asyncio.run(service.track_query("logitech mx master 3s", drop_pct=10))


def test_a_renewed_unit_never_becomes_the_tracking_baseline(monkeypatch):
    # The refurb is cheapest, so an unguarded min() would make every future
    # "drop" a comparison against a different product.
    out = _track(monkeypatch, [
        _offer("amazon", "114.99", "Logitech MX Master 3S (Renewed)", condition="renewed"),
        _offer("amazon", "139.99", "Logitech MX Master 3S Wireless Mouse"),
    ])
    assert out["ok"]
    assert out["best_price"] == 139.99
    assert "Renewed" not in out["title"]
    assert [o["price"] for o in out["offers"]] == [139.99]


def test_refurbs_are_still_tracked_when_nothing_else_is_listed(monkeypatch):
    out = _track(monkeypatch, [
        _offer("amazon", "114.99", "Logitech MX Master 3S (Renewed)", condition="renewed"),
    ])
    assert out["ok"]
    assert out["best_price"] == 114.99


def test_listings_without_a_condition_flag_are_treated_as_new(monkeypatch):
    out = _track(monkeypatch, [
        _offer("amazon", "129.99", "Logitech MX Master 3S Wireless Mouse"),
        _offer("bestbuy", "139.99", "Logitech MX Master 3S"),
    ])
    assert out["best_price"] == 129.99
    assert len(out["offers"]) == 2
