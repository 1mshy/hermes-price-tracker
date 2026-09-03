"""Back-in-stock alerts: stock transitions on offers, event-based firing."""
import asyncio
from decimal import Decimal

from pricewatch import service
from pricewatch.db import init_db, session_scope
from pricewatch.models import Product, Tracker
from pricewatch.stores.base import StoreResult


def _result(url, price="119", in_stock=True, store="west3d", currency="CAD"):
    return StoreResult(store=store, url=url, title="Bambu Lab X1C", price=Decimal(str(price)),
                       currency=currency, in_stock=in_stock, method="test")


def _product(session, title="Bambu Lab X1C"):
    product = Product(title=title, match_key=title.lower())
    session.add(product)
    session.flush()
    return product.id


def _tracker(pid, **kw):
    defaults = dict(product_id=pid, label="x1c", currency="CAD", alert_on_restock=True,
                    channels="", cooldown_hours=12, active=True)
    defaults.update(kw)
    return Tracker(**defaults)


def _capture_dispatch(monkeypatch):
    sent = []

    async def dispatch(alert, only=None):
        sent.append(alert)
        return {"ntfy": "sent"}
    monkeypatch.setattr(service, "dispatch", dispatch)
    return sent


def _sweep(pid, url, **kw):
    """Re-read one listing the way refresh_offers does, then evaluate."""
    with session_scope() as session:
        service._upsert_offer(session, pid, _result(url, **kw))
    return asyncio.run(service.evaluate_trackers())


def _fired(tid, alerts, kind="restock"):
    return [a for a in alerts if a["tracker_id"] == tid and a["kind"] == kind]


# ── recording transitions ────────────────────────────────────────────────
def test_upsert_records_a_restock_only_on_out_to_in():
    init_db()
    with session_scope() as session:
        pid = _product(session)
        url = f"https://west3d.example/p/restock-{pid}"

        offer = service._upsert_offer(session, pid, _result(url, in_stock=True))
        assert offer.stock_changed_at is not None       # None→True is a change…
        assert offer.last_restocked_at is None          # …but nothing was ever seen missing
        first_change = offer.stock_changed_at
        assert len(offer.points) == 1

        offer = service._upsert_offer(session, pid, _result(url, in_stock=True))
        assert offer.stock_changed_at == first_change   # same state: nothing recorded
        assert len(offer.points) == 1

        offer = service._upsert_offer(session, pid, _result(url, in_stock=False))
        assert offer.stock_changed_at > first_change
        assert offer.last_restocked_at is None
        assert len(offer.points) == 2                   # stock-only change → a point
        assert any(p.in_stock is False and p.price == Decimal("119") for p in offer.points)

        offer = service._upsert_offer(session, pid, _result(url, in_stock=True))
        assert offer.last_restocked_at == offer.stock_changed_at
        assert len(offer.points) == 3


def test_a_read_that_says_nothing_about_stock_keeps_the_last_known_state():
    # One bot-walled sweep between "sold out" and "back" must not turn the
    # restock into a first sighting.
    init_db()
    with session_scope() as session:
        pid = _product(session)
        url = f"https://west3d.example/p/blip-{pid}"
        service._upsert_offer(session, pid, _result(url, in_stock=False))
        offer = service._upsert_offer(
            session, pid, StoreResult(store="west3d", url=url, error="bot challenge"))
        assert offer.in_stock is False
        assert offer.consecutive_errors == 1
        offer = service._upsert_offer(session, pid, _result(url, in_stock=True))
        assert offer.last_restocked_at is not None


# ── firing ───────────────────────────────────────────────────────────────
def _watch(monkeypatch, in_stock=False, **tracker_kw):
    """A CAD restock watch on one west3d listing seeded in the given state."""
    init_db()
    sent = _capture_dispatch(monkeypatch)
    with session_scope() as session:
        pid = _product(session)
        url = f"https://west3d.example/p/{pid}"
        service._upsert_offer(session, pid, _result(url, in_stock=in_stock))
        tracker = _tracker(pid, **tracker_kw)
        session.add(tracker)
        session.flush()
        tid = tracker.id
    return pid, url, tid, sent


def test_restock_fires_once_per_return_and_again_after_the_next_cycle(monkeypatch):
    pid, url, tid, sent = _watch(monkeypatch, in_stock=False)
    assert not _fired(tid, asyncio.run(service.evaluate_trackers()))     # still sold out

    fired = _fired(tid, _sweep(pid, url, in_stock=True))
    assert len(fired) == 1
    assert fired[0]["store"] == "west3d"
    assert fired[0]["reasons"] == ["back in stock at west3d"]
    alert = next(a for a in sent if a.url == url)
    assert alert.title.startswith("📦") and alert.currency == "CAD"
    assert "**back in stock** at **west3d** — CA$119.00" in alert.body

    assert not _fired(tid, _sweep(pid, url, in_stock=True))              # same state: quiet
    assert not _fired(tid, _sweep(pid, url, in_stock=False))             # sold out again: quiet
    assert len(_fired(tid, _sweep(pid, url, in_stock=True))) == 1        # second return: fires

    events = [e for e in asyncio.run(service.list_alerts(limit=50))["alerts"]
              if e["tracker_id"] == tid]
    assert [e["kind"] for e in events] == ["restock", "restock"]
    assert events[0]["reason"] == "back in stock at west3d"
    row = next(r for r in asyncio.run(service.list_tracked()) if r["tracker_id"] == tid)
    assert row["last_restock_notified_at"] is not None
    assert row["last_notified_at"] is None                                # no price alert went out


def test_a_foreign_listing_coming_back_never_fires_a_cad_watch(monkeypatch):
    init_db()
    sent = _capture_dispatch(monkeypatch)
    with session_scope() as session:
        pid = _product(session)
        url = f"https://amazon.example/dp/{pid}"
        service._upsert_offer(session, pid, _result(url, store="amazon", currency="USD",
                                                    price="99.99", in_stock=False))
        tracker = _tracker(pid)
        session.add(tracker)
        session.flush()
        tid = tracker.id
    fired = _sweep(pid, url, store="amazon", currency="USD", price="99.99", in_stock=True)
    assert not _fired(tid, fired)
    assert not any(a.url == url for a in sent)


def test_the_cheapest_returned_listing_is_the_one_named(monkeypatch):
    init_db()
    _capture_dispatch(monkeypatch)
    with session_scope() as session:
        pid = _product(session)
        urls = {s: f"https://{s}.example/p/{pid}" for s in ("west3d", "bambulab")}
        for store, price, state in (("west3d", "129", False), ("bambulab", "119", False)):
            service._upsert_offer(session, pid, _result(urls[store], store=store, price=price,
                                                        in_stock=state))
        tracker = _tracker(pid)
        session.add(tracker)
        session.flush()
        tid = tracker.id
    with session_scope() as session:
        for store, price in (("west3d", "129"), ("bambulab", "119")):
            service._upsert_offer(session, pid, _result(urls[store], store=store, price=price))
    fired = _fired(tid, asyncio.run(service.evaluate_trackers()))
    assert len(fired) == 1
    assert (fired[0]["store"], fired[0]["price"]) == ("bambulab", 119.0)


def test_a_watch_with_a_target_and_a_restock_rule_reports_both(monkeypatch):
    pid, url, tid, sent = _watch(monkeypatch, in_stock=False, target_price=Decimal("150"))
    fired = [a for a in _sweep(pid, url, in_stock=True) if a["tracker_id"] == tid]
    assert sorted(a["kind"] for a in fired) == ["price", "restock"]
    restock = next(a for a in fired if a["kind"] == "restock")
    assert restock["reasons"] == ["back in stock at west3d",
                                  "at or below your target of CA$150.00"]
    alert = next(a for a in sent if a.url == url and a.title.startswith("📦"))
    assert "• at or below your target of CA$150.00" in alert.body
    price = next(a for a in sent if a.url == url and a.title.startswith("💸"))
    assert "back in stock" not in price.body


# ── registration ─────────────────────────────────────────────────────────
def test_a_watch_with_no_condition_is_refused_before_any_store_is_read(monkeypatch):
    async def never(*args, **kwargs):
        raise AssertionError("read a store for a watch that could never fire")
    monkeypatch.setattr(service, "_search_ranked", never)
    monkeypatch.setattr(service, "_fetch_for_tracking", never)
    for out in (asyncio.run(service.track_query("bambu lab x1c")),
                asyncio.run(service.track_url("https://west3d.example/p/x1c"))):
        assert out["ok"] is False
        assert "alert_on_restock=true" in out["error"]


def test_rest_refuses_a_conditionless_watch_with_422():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pricewatch import api

    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    with TestClient(app) as client:
        response = client.post("/api/trackers/url", json={"url": "https://west3d.example/p/x1c"})
    assert response.status_code == 422
    assert "alert_on_restock=true" in response.json()["detail"]["error"]


def test_track_query_notes_a_sold_out_baseline_listing(monkeypatch):
    init_db()
    listings = iter([
        [_result("https://west3d.example/p/oos-1", in_stock=False)],
        [_result("https://west3d.example/p/oos-2", in_stock=False)],
    ])

    async def ranked(query, **kwargs):
        found = next(listings)
        return found, len(found), query
    monkeypatch.setattr(service, "_search_ranked", ranked)

    out = asyncio.run(service.track_query("bambu lab x1c", drop_pct=10))
    assert out["ok"]
    assert out["offers"][0]["in_stock"] is False
    assert "currently out of stock at west3d" in out["note"]
    assert "set alert_on_restock=true" in out["note"]

    asked = asyncio.run(service.track_query("bambu lab x1c", alert_on_restock=True))
    assert asked["ok"] and "note" not in asked        # the user already asked for it


def test_track_url_notes_a_sold_out_listing(monkeypatch):
    init_db()

    async def fetch(url):
        return _result(url, in_stock=False), "tracking the regional listing"
    monkeypatch.setattr(service, "_fetch_for_tracking", fetch)

    out = asyncio.run(service.track_url("https://west3d.example/p/oos-url", target_price=100))
    assert out["ok"] and out["in_stock"] is False
    assert out["note"] == ("tracking the regional listing; currently out of stock at "
                           "west3d; set alert_on_restock=true to be told when it returns")


def test_update_tracker_toggles_the_restock_rule():
    init_db()
    with session_scope() as session:
        pid = _product(session)
        tracker = _tracker(pid, alert_on_restock=False, target_price=Decimal("100"))
        session.add(tracker)
        session.flush()
        tid = tracker.id
    asyncio.run(service.set_tracker(tid, alert_on_restock=True))
    with session_scope() as session:
        assert session.get(Tracker, tid).alert_on_restock is True
    asyncio.run(service.set_tracker(tid, alert_on_restock=False))
    with session_scope() as session:
        assert session.get(Tracker, tid).alert_on_restock is False


def test_enabling_the_rule_later_ignores_a_restock_that_already_happened(monkeypatch):
    # The listing sold out, came back, and only then did the user ask to be
    # told about restocks. That past return is not news; the next one is.
    init_db()
    _capture_dispatch(monkeypatch)
    with session_scope() as session:
        pid = _product(session)
        url = f"https://west3d.example/p/late-{pid}"
        service._upsert_offer(session, pid, _result(url, in_stock=False))
        service._upsert_offer(session, pid, _result(url, in_stock=True))
        tracker = _tracker(pid, alert_on_restock=False, target_price=Decimal("50"))
        session.add(tracker)
        session.flush()
        tid = tracker.id
    asyncio.run(service.set_tracker(tid, alert_on_restock=True))
    assert not _fired(tid, asyncio.run(service.evaluate_trackers()))
    assert not _fired(tid, _sweep(pid, url, in_stock=False))
    assert len(_fired(tid, _sweep(pid, url, in_stock=True))) == 1


# ── reporting ────────────────────────────────────────────────────────────
def test_list_trackers_reports_the_restock_rule_and_sold_out_listings():
    init_db()
    with session_scope() as session:
        pid = _product(session)
        service._upsert_offer(session, pid, _result(
            f"https://west3d.example/p/lt-{pid}", in_stock=False))
        service._upsert_offer(session, pid, _result(
            f"https://bambulab.example/p/lt-{pid}", store="bambulab", in_stock=True))
        service._upsert_offer(session, pid, _result(
            f"https://amazon.example/dp/lt-{pid}", store="amazon", currency="USD",
            price="99", in_stock=False))
        tracker = _tracker(pid)
        session.add(tracker)
        session.flush()
        tid = tracker.id
    row = next(r for r in asyncio.run(service.list_tracked()) if r["tracker_id"] == tid)
    assert row["alert_on_restock"] is True
    assert row["last_restock_notified_at"] is None
    assert row["out_of_stock_listings"] == 1            # the sold-out USD listing is foreign
    assert row["foreign_listings"] == 1
    assert all(o["stock_changed_at"] and o["last_restocked_at"] is None for o in row["offers"])


def test_history_statistics_do_not_count_stock_only_points():
    init_db()
    with session_scope() as session:
        pid = _product(session)
        url = f"https://west3d.example/p/hist-{pid}"
        for price, state in (("119", True), ("119", False), ("119", True), ("99", True)):
            service._upsert_offer(session, pid, _result(url, price=price, in_stock=state))
    out = asyncio.run(service.price_history(pid))
    assert len(out["points"]) == 4                                  # availability stays visible
    assert [p["in_stock"] for p in out["points"]].count(False) == 1
    assert out["summary"]["observations"] == 2
    assert out["summary"]["average"] == 109.0
