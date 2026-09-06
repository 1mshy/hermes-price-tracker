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


def test_cooldown_expiry_allows_a_repeat_only_once_the_price_has_moved():
    # The same figure every twelve hours is not news — that is the double
    # push the README promises will not happen ("a flat price stays quiet").
    tracker = _tracker(target_price=Decimal("50"),
                       last_notified_at=utcnow() - dt.timedelta(hours=13),
                       last_notified_price=Decimal("45"))
    assert not service._should_notify(tracker, Decimal("45"))
    assert service._should_notify(tracker, Decimal("46"))      # moved, still qualifies
    assert service._should_notify(tracker, Decimal("44"))


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


# ── one currency per watch ───────────────────────────────────────────────
# The bug: an amazon.com listing read USD 99.99 onto a watch whose CAD 119
# baseline came from West3D, and "down 16%" was pushed twice. Digits from
# two currencies are never compared.
from pricewatch import preferences
from pricewatch.models import Tracker as _Tracker


def _seed(session, offers, title="Sunlu AMS heater"):
    product = Product(title=title, match_key=title.lower())
    session.add(product)
    session.flush()
    for i, (store, price, currency) in enumerate(offers):
        session.add(Offer(product_id=product.id, store=store,
                          url=f"https://{store}.example/p/{product.id}-{i}",
                          currency=currency, last_price=Decimal(str(price)),
                          in_stock=True, consecutive_errors=0, active=True))
    session.flush()
    return product.id


def _capture_dispatch(monkeypatch):
    sent = []

    async def dispatch(alert, only=None):
        sent.append(alert)
        return {"ntfy": "sent"}
    monkeypatch.setattr(service, "dispatch", dispatch)
    return sent


def test_best_offer_stays_within_the_watch_currency():
    init_db()
    with session_scope() as session:
        pid = _seed(session, [("west3d", "119", "CAD"), ("amazon", "99.99", "USD")])
        assert service._best_offer(session, pid, "CAD").store == "west3d"
        assert service._best_offer(session, pid, "USD").store == "amazon"
        assert service._best_offer(session, pid, "EUR") is None
        assert service._best_offer(session, pid).store == "amazon"    # unscoped: raw digits


def test_a_cheaper_foreign_listing_does_not_fire_a_cad_watch(monkeypatch):
    init_db()
    sent = _capture_dispatch(monkeypatch)
    with session_scope() as session:
        pid = _seed(session, [("west3d", "119", "CAD"), ("amazon", "99.99", "USD")])
        tracker = _tracker(product_id=pid, label="SUNLU AMS Heater", currency="CAD",
                           drop_pct=15.0, baseline_price=Decimal("119"))
        session.add(tracker)
        session.flush()
        tid = tracker.id
    fired = asyncio.run(service.evaluate_trackers())
    assert tid not in {f["tracker_id"] for f in fired}
    assert not any(a.store == "amazon" and a.price == 99.99 for a in sent)


def test_a_drop_in_the_watch_currency_fires_and_names_the_currency(monkeypatch):
    init_db()
    sent = _capture_dispatch(monkeypatch)
    with session_scope() as session:
        pid = _seed(session, [("west3d", "99", "CAD"), ("amazon", "99.99", "USD")])
        tracker = _tracker(product_id=pid, currency="CAD", drop_pct=15.0,
                           baseline_price=Decimal("119"))
        session.add(tracker)
        session.flush()
        tid = tracker.id
    fired = [f for f in asyncio.run(service.evaluate_trackers()) if f["tracker_id"] == tid]
    assert fired and fired[0]["store"] == "west3d"
    alert = next(a for a in sent if a.store == "west3d" and a.price == 99.0)
    assert alert.currency == "CAD"
    assert "CA$99.00" in alert.body and "CA$119.00" in alert.body


def test_reasons_are_written_in_the_watch_currency():
    tracker = _tracker(target_price=Decimal("120"), currency="CAD")
    assert service._reasons(tracker, Decimal("119")) == ["at or below your target of CA$120.00"]


def test_list_trackers_reports_the_currency_and_counts_foreign_listings():
    init_db()
    with session_scope() as session:
        pid = _seed(session, [("west3d", "119", "CAD"), ("amazon", "99.99", "USD")])
        tracker = _tracker(product_id=pid, label="foreign-count", currency="CAD",
                           target_price=Decimal("80"))
        session.add(tracker)
        session.flush()
        tid = tracker.id
    row = next(r for r in asyncio.run(service.list_tracked()) if r["tracker_id"] == tid)
    assert row["currency"] == "CAD"
    assert row["current_best"]["store"] == "west3d"
    assert row["current_best"]["currency"] == "CAD"
    assert row["foreign_listings"] == 1
    assert "never measured" in row["note"]


def test_backfill_gives_old_watches_the_currency_of_their_baseline_listing():
    init_db()
    with session_scope() as session:
        pid = _seed(session, [("amazon", "99.99", "USD"), ("west3d", "119", "CAD")])
        tracker = _tracker(product_id=pid, baseline_price=Decimal("119"), drop_pct=15.0)
        session.add(tracker)
        session.flush()
        tid = tracker.id
        assert tracker.currency is None
    asyncio.run(service.backfill_tracker_currency())
    with session_scope() as session:
        assert session.get(_Tracker, tid).currency == "CAD"


def _two_listings(usd_price="79.99"):
    return [
        StoreResult(store="amazon", url="https://amazon.example/dp/1", title="Sunlu AMS Heater",
                    price=Decimal(usd_price), currency="USD", in_stock=True),
        StoreResult(store="west3d", url="https://west3d.example/p/1", title="Sunlu AMS Heater",
                    price=Decimal("119"), currency="CAD", in_stock=True),
    ]


def test_track_query_baselines_on_a_listing_in_the_users_currency(monkeypatch):
    preferences._state = ("CAD", "CA", "runtime-override")
    try:
        out = _track(monkeypatch, _two_listings())
    finally:
        preferences.reload()
    assert out["ok"]
    assert (out["currency"], out["best_store"], out["best_price"]) == ("CAD", "west3d", 119.0)
    assert len(out["offers"]) == 2                     # the USD listing is still watched
    assert {o["currency"] for o in out["offers"]} == {"CAD", "USD"}


def test_track_query_without_a_preference_compares_at_an_indicative_rate(monkeypatch):
    # USD 79.99 really is cheaper than CAD 119; CAD 119 would beat USD 99.99.
    preferences._state = ("", "", "env")
    try:
        cheap_usd = _track(monkeypatch, _two_listings("79.99"))
        cheap_cad = _track(monkeypatch, _two_listings("99.99"))
    finally:
        preferences.reload()
    assert (cheap_usd["best_store"], cheap_usd["currency"]) == ("amazon", "USD")
    assert (cheap_cad["best_store"], cheap_cad["currency"]) == ("west3d", "CAD")


# ── pasted links land on the regional storefront ─────────────────────────
def _amazon_by_host(missing_on_ca=False):
    seen = []

    async def fetch(url):
        seen.append(url)
        on_ca = url.split("/")[2].endswith(".ca")
        if on_ca and missing_on_ca:
            return StoreResult(store="amazon", url=url, method="http:not-found",
                               error="no longer listed on Amazon (404)")
        return StoreResult(store="amazon", url=url, title="SUNLU AMS Heater",
                           price=Decimal("167.73") if on_ca else Decimal("99.99"),
                           currency="CAD" if on_ca else "USD", in_stock=True, method="test")
    return fetch, seen


def test_tracking_a_com_link_registers_the_regional_listing(monkeypatch):
    init_db()
    fetch, seen = _amazon_by_host()
    monkeypatch.setattr(service, "fetch_offer", fetch)
    monkeypatch.setattr(preferences, "preferred_region", lambda: "CA")
    monkeypatch.setattr(preferences, "preferred_currency", lambda: "CAD")
    out = asyncio.run(service.track_url("https://www.amazon.com/dp/B0FQVHHBQV", target_price=120))
    assert out["ok"]
    assert out["url"] == "https://www.amazon.ca/dp/B0FQVHHBQV"
    assert (out["currency"], out["price"]) == ("CAD", 167.73)
    assert "amazon.ca" in out["note"] and "CAD" in out["note"]
    assert seen == ["https://www.amazon.ca/dp/B0FQVHHBQV"]      # .com never read


def test_the_pasted_link_is_kept_when_the_regional_listing_is_missing(monkeypatch):
    init_db()
    fetch, seen = _amazon_by_host(missing_on_ca=True)
    monkeypatch.setattr(service, "fetch_offer", fetch)
    monkeypatch.setattr(preferences, "preferred_region", lambda: "CA")
    monkeypatch.setattr(preferences, "preferred_currency", lambda: "CAD")
    out = asyncio.run(service.track_url("https://www.amazon.com/dp/B0FQVHHBQ0", target_price=120))
    assert out["ok"]
    assert out["url"] == "https://www.amazon.com/dp/B0FQVHHBQ0"
    assert out["currency"] == "USD"
    assert "note" not in out


def test_adding_a_foreign_listing_to_a_watch_says_it_will_not_alert(monkeypatch):
    init_db()
    fetch, _ = _amazon_by_host()
    monkeypatch.setattr(service, "fetch_offer", fetch)
    monkeypatch.setattr(preferences, "preferred_region", lambda: "")
    monkeypatch.setattr(preferences, "preferred_currency", lambda: "")
    with session_scope() as session:
        pid = _seed(session, [("west3d", "119", "CAD")])
        session.add(_tracker(product_id=pid, currency="CAD", target_price=Decimal("80")))
    out = asyncio.run(service.add_offer(pid, "https://www.amazon.com/dp/B0FQVHHBQV"))
    assert out["ok"] and out["currency"] == "USD"
    assert "never triggers" in out["note"]


# ── deal notes: coupons ride along with the price ───────────────────────
def test_upsert_stores_and_clears_the_deal_note():
    init_db()
    with session_scope() as session:
        product = Product(title="Sunlu AMS heater", match_key="sunlu ams heater")
        session.add(product)
        session.flush()
        url = f"https://www.amazon.ca/dp/COUPON{product.id}"
        with_coupon = _result("49.99", url)
        with_coupon.extra["deal_note"] = "CA$49.99 after the on-page coupon (117.74 off)"
        offer = service._upsert_offer(session, product.id, with_coupon)
        assert offer.deal_note.startswith("CA$49.99 after")
        assert service._offer_dict(offer)["deal_note"] == offer.deal_note
        offer = service._upsert_offer(session, product.id, _result("167.73", url))
        assert offer.deal_note is None                 # the coupon lapsed


def test_alert_body_carries_the_deal_note(monkeypatch):
    init_db()
    sent = _capture_dispatch(monkeypatch)
    note = "CA$49.99 after the on-page coupon (117.74 off); sticker price CA$167.73 — clip the coupon"
    with session_scope() as session:
        product = Product(title="Sunlu AMS heater", match_key="sunlu ams heater")
        session.add(product)
        session.flush()
        session.add(Offer(product_id=product.id, store="amazon",
                          url=f"https://www.amazon.ca/dp/NOTE{product.id}", currency="CAD",
                          last_price=Decimal("49.99"), in_stock=True, deal_note=note,
                          consecutive_errors=0, active=True))
        tracker = _tracker(product_id=product.id, currency="CAD", target_price=Decimal("80"))
        session.add(tracker)
        session.flush()
        tid = tracker.id
    fired = [f for f in asyncio.run(service.evaluate_trackers()) if f["tracker_id"] == tid]
    assert fired and fired[0]["deal_note"] == note
    alert = next(a for a in sent if a.price == 49.99)
    assert "clip the coupon" in alert.body
    assert "CA$49.99" in alert.body


# ── agent-authored pushes share the channels and the audit trail ────────
def test_push_note_records_an_agent_event(monkeypatch):
    init_db()
    sent = _capture_dispatch(monkeypatch)
    with session_scope() as session:
        product = Product(title="Sunlu AMS heater", match_key="sunlu ams heater")
        session.add(product)
        session.flush()
        tracker = _tracker(product_id=product.id, label="SUNLU AMS Heater", currency="CAD",
                           target_price=Decimal("80"))
        session.add(tracker)
        session.flush()
        tid = tracker.id
    out = asyncio.run(service.push_note(
        "Sunlu AMS heater: $49.99 coupon on amazon.ca",
        "r/3dbargains says the clip coupon is back; not visible from here yet.",
        url="https://www.amazon.ca/dp/B0FQVHHBQV", tracker_id=tid, price=49.99))
    assert out["ok"] and out["delivered"] and out["recorded_on_tracker"] == tid
    assert sent[-1].title.endswith("Sunlu AMS heater: $49.99 coupon on amazon.ca")
    assert sent[-1].url == "https://www.amazon.ca/dp/B0FQVHHBQV"
    newest = asyncio.run(service.list_alerts(limit=1))["alerts"][0]
    assert newest["kind"] == "agent" and newest["tracker_id"] == tid
    assert newest["reason"].startswith("agent: Sunlu AMS heater")
    assert newest["price"] == 49.99 and newest["delivered"] is True
    assert "not visible from here" in newest["detail"]


def test_push_note_without_a_watch_is_sent_but_not_recorded(monkeypatch):
    init_db()
    _capture_dispatch(monkeypatch)
    before = len(asyncio.run(service.list_alerts(limit=500))["alerts"])
    out = asyncio.run(service.push_note("hello", "world"))
    assert out["ok"] and out["recorded_on_tracker"] is None
    assert len(asyncio.run(service.list_alerts(limit=500))["alerts"]) == before


def test_push_note_says_so_when_no_channel_is_configured(monkeypatch):
    async def nothing(alert, only=None):
        return {}
    monkeypatch.setattr(service, "dispatch", nothing)
    out = asyncio.run(service.push_note("hello", "world"))
    assert out["ok"] is False and "no notification channel" in out["error"]
    assert asyncio.run(service.push_note("", "world"))["ok"] is False
