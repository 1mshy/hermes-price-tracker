"""Durable, quiet product intake: retailer trouble never loses a user's watch."""
import asyncio
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from pricewatch import db, service
from pricewatch.api import router
from pricewatch.models import Base, Offer, Product, Tracker
from pricewatch.stores.base import StoreResult


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/watch.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(service.preferences, "preferred_currency", lambda: "CAD")
    async def unexpected_notification(*args, **kwargs):
        raise AssertionError("Intake and failed lookups must not send notifications")
    monkeypatch.setattr(service, "dispatch", unexpected_notification)
    yield
    engine.dispose()


def listing(url="https://shop.example/products/printer", price="100"):
    return StoreResult(store="example", url=url, title="Example Printer X1",
                       price=Decimal(price), currency="CAD", in_stock=True, method="test")


async def finish_intake():
    tasks = list(service._intake_tasks.values())
    if tasks:
        await asyncio.gather(*tasks)


def test_saves_immediately_before_a_slow_failed_lookup_and_keeps_retrying(monkeypatch):
    async def scenario():
        release = asyncio.Event()
        async def unavailable(url):
            await release.wait()
            return StoreResult(store="example", url=url, error="Retailer blocked request"), None
        monkeypatch.setattr(service, "_fetch_for_tracking", unavailable)
        saved = await asyncio.wait_for(service.add_watch("https://shop.example/products/printer"), .5)
        assert saved["ok"] and saved["status"] == "pending"
        assert saved["drop_pct"] == 5
        with db.session_scope() as session:
            assert session.get(Tracker, saved["tracker_id"]).baseline_price is None
            assert session.get(Product, saved["product_id"]).watch_input.endswith("/printer")
        release.set()
        await finish_intake()
        row, = await service.list_tracked()
        assert row["status"] == "retrying" and row["current_best"] is None
        assert row["offers"][0]["error"] == "Retailer blocked request"
        async def recovered(url):
            return listing(url)
        monkeypatch.setattr(service, "fetch_offer", recovered)
        sweep = await service.refresh_offers()
        row, = await service.list_tracked()
        assert sweep["updated"] == 1
        assert row["status"] == "watching" and row["baseline_price"] == 100
        assert row["current_best"]["currency"] == "CAD"
        await service.stop_full_sweep()
    asyncio.run(scenario())


def test_unmatched_name_survives_restart_and_is_discovered_by_sweep(monkeypatch):
    async def no_match(*args, **kwargs):
        return [], 0, args[0]
    monkeypatch.setattr(service, "_search_ranked", no_match)
    async def save():
        saved = await service.add_watch("Example Printer X1", target_price=80)
        await finish_intake()
        await service.stop_full_sweep()
        return saved
    saved = asyncio.run(save())
    with db.session_scope() as session:
        assert session.get(Product, saved["product_id"]).discovery_error
        assert session.scalar(select(Offer)) is None
    async def match(*args, **kwargs):
        return [listing()], 1, args[0]
    monkeypatch.setattr(service, "_search_ranked", match)
    async def resumed():
        result = await service.refresh_offers()
        row, = await service.list_tracked()
        assert result["discovered"] == 1
        assert row["tracker_id"] == saved["tracker_id"]
        assert row["status"] == "watching" and row["baseline_price"] == 100
        assert row["target_price"] == 80 and row["drop_pct"] is None
        await service.stop_full_sweep()
    asyncio.run(resumed())


def test_concurrent_duplicates_keep_one_watch_and_preserve_settings(monkeypatch):
    monkeypatch.setattr(service, "_start_intake", lambda product_id: None)
    async def scenario():
        one, two = await asyncio.gather(
            service.add_watch("Example Printer X1", target_price=80),
            service.add_watch("  example   printer x1  ", target_price=20))
        assert one["tracker_id"] == two["tracker_id"]
        assert one["target_price"] == two["target_price"] == 80
        assert two["existing"] is True
        await service.set_tracker(one["tracker_id"], active=False)
        repeated = await service.add_watch("Example Printer X1")
        assert repeated["status"] == "paused"
        assert len(await service.list_tracked()) == 1
    asyncio.run(scenario())


def test_url_tracking_parameters_deduplicate_but_variants_do_not(monkeypatch):
    monkeypatch.setattr(service, "_start_intake", lambda product_id: None)
    async def scenario():
        first = await service.add_watch("shop.example/products/printer?variant=blue&utm_source=email")
        repeated = await service.add_watch("https://shop.example/products/printer?variant=blue#details")
        other = await service.add_watch("https://shop.example/products/printer?variant=red")
        assert repeated["tracker_id"] == first["tracker_id"]
        assert other["tracker_id"] != first["tracker_id"]
        assert len(await service.list_tracked()) == 2
    asyncio.run(scenario())


def test_api_needs_only_a_product_name_and_rejects_invalid_rules(monkeypatch):
    monkeypatch.setattr(service, "_start_intake", lambda product_id: None)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    with TestClient(app) as client:
        response = client.post("/api/trackers", json={"input": "Example Printer X1"})
        assert response.status_code == 200
        assert response.json()["drop_pct"] == 5
        for payload in ({"input": " "}, {"input": "ftp://shop.example/x"},
                        {"input": "X1", "drop_pct": 0}, {"input": "X1", "target_price": -1}):
            assert client.post("/api/trackers", json=payload).status_code == 422


def test_shutdown_cancels_discovery_without_losing_saved_request(monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(service, "_search_ranked", blocked)
        saved = await service.add_watch("Example Printer X1")
        await entered.wait()
        await service.stop_full_sweep()
        assert not service._intake_tasks
        with db.session_scope() as session:
            assert session.get(Tracker, saved["tracker_id"]) is not None
    asyncio.run(scenario())


def test_url_adopts_actual_currency_including_after_restart(monkeypatch):
    monkeypatch.setattr(service, "_start_intake", lambda product_id: None)
    async def foreign_quote(url):
        result = listing(url)
        result.currency = "USD"
        return result
    monkeypatch.setattr(service, "fetch_offer", foreign_quote)
    async def scenario():
        saved = await service.add_watch("https://shop.example/products/printer")
        assert await service.backfill_tracker_currency() == 0
        await service.refresh_offers()
        row, = await service.list_tracked()
        assert row["tracker_id"] == saved["tracker_id"]
        assert row["currency"] == "USD" and row["baseline_price"] == 100
        assert row["status"] == "watching"
        await service.stop_full_sweep()
    asyncio.run(scenario())


def test_rest_patch_distinguishes_omitted_rule_from_explicit_null(monkeypatch):
    monkeypatch.setattr(service, "_start_intake", lambda product_id: None)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    with TestClient(app) as client:
        watch = client.post("/api/trackers", json={"input": "Example Printer X1", "target_price": 80}).json()
        tid = watch["tracker_id"]
        assert client.patch(f"/api/trackers/{tid}", json={"label": "Printer"}).status_code == 200
        assert client.get("/api/trackers").json()["trackers"][0]["target_price"] == 80
        assert client.patch(f"/api/trackers/{tid}", json={"target_price": None, "drop_pct": 10}).status_code == 200
        row = client.get("/api/trackers").json()["trackers"][0]
        assert row["target_price"] is None and row["drop_pct"] == 10
        assert client.patch(f"/api/trackers/{tid}", json={"drop_pct": None, "alert_on_restock": True}).status_code == 200
        row = client.get("/api/trackers").json()["trackers"][0]
        assert row["drop_pct"] is None and row["alert_on_restock"]


def test_failed_sweep_preserves_history_but_never_alerts_on_stale_price(monkeypatch):
    async def scenario():
        async def initial(url):
            return listing(url), None
        monkeypatch.setattr(service, "_fetch_for_tracking", initial)
        saved = await service.add_watch("https://shop.example/products/printer", target_price=120)
        await finish_intake()
        async def unavailable(url):
            raise RuntimeError("network unavailable")
        monkeypatch.setattr(service, "fetch_offer", unavailable)
        result = await service.refresh_offers()
        row, = await service.list_tracked()
        assert result["failed"] == 1 and result["alerts"] == []
        assert row["status"] == "retrying"
        assert row["current_best"]["price"] == 100 and row["current_best"]["stale"]
        assert row["tracker_id"] == saved["tracker_id"]
        await service.stop_full_sweep()
    asyncio.run(scenario())


def test_existing_blocked_watch_finds_another_retailer(monkeypatch):
    with db.session_scope() as session:
        product = Product(title="Example Printer X1", match_key="printer")
        session.add(product)
        session.flush()
        session.add(Tracker(product_id=product.id, label=product.title, drop_pct=5,
                            baseline_price=Decimal("100"), currency="CAD"))
        session.add(Offer(product_id=product.id, store="example", url="https://old.example/printer",
                          last_price=Decimal("100"), currency="CAD", consecutive_errors=2,
                          last_error="blocked"))
    async def unavailable(url):
        return StoreResult(store="example", url=url, currency="CAD", error="blocked")
    async def alternative(*args, **kwargs):
        return [listing()], 1, args[0]
    monkeypatch.setattr(service, "fetch_offer", unavailable)
    monkeypatch.setattr(service, "_search_ranked", alternative)
    async def scenario():
        result = await service.refresh_offers()
        row, = await service.list_tracked()
        assert result["discovered"] == 1
        assert row["status"] == "watching" and len(row["offers"]) == 2
        assert row["current_best"]["url"] == "https://shop.example/products/printer"
        await service.stop_full_sweep()
    asyncio.run(scenario())


def test_sold_out_discount_does_not_trigger_price_alert(monkeypatch):
    async def unavailable_stock(url):
        result = listing(url)
        result.in_stock = False
        return result, None
    monkeypatch.setattr(service, "_fetch_for_tracking", unavailable_stock)
    async def scenario():
        await service.add_watch("https://shop.example/products/printer", target_price=120)
        await finish_intake()
        assert await service.evaluate_trackers() == []
        row, = await service.list_tracked()
        assert row["offers"][0]["in_stock"] is False
        assert row["current_best"]["price"] == 100
        await service.stop_full_sweep()
    asyncio.run(scenario())
