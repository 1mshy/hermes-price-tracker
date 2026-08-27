import asyncio
import datetime as dt
from decimal import Decimal

from pricewatch import service
from pricewatch.db import init_db, session_scope
from pricewatch.models import Offer, PricePoint, Product, utcnow


def _seed(prices, current, currency="USD"):
    init_db()
    with session_scope() as session:
        product = Product(title="History Thing", match_key="history thing")
        session.add(product)
        session.flush()
        offer = Offer(product_id=product.id, store="example",
                      url=f"https://example.com/products/hist-{product.id}-{id(prices)}",
                      currency=currency, last_price=Decimal(str(current)),
                      in_stock=True, consecutive_errors=0, active=True)
        session.add(offer)
        session.flush()
        for index, price in enumerate(prices):
            session.add(PricePoint(
                offer_id=offer.id, price=Decimal(str(price)), currency=currency,
                observed_at=utcnow() - dt.timedelta(days=len(prices) - index)))
        return product.id


def test_summary_near_lowest():
    product_id = _seed([100, 95, 90], current=90)
    out = asyncio.run(service.price_history(product_id))
    summary = out["summary"]
    assert summary["lowest_seen"] == 90.0
    assert summary["highest_seen"] == 100.0
    assert summary["average"] == 95.0
    assert summary["observations"] == 3
    assert summary["verdict"] == "at or near the lowest recorded price"


def test_summary_above_average():
    product_id = _seed([80, 85, 90], current=95)
    out = asyncio.run(service.price_history(product_id))
    assert out["summary"]["verdict"] == "above the recorded average"


def test_summary_ignores_foreign_currency_points():
    product_id = _seed([100, 100], current=100)
    with session_scope() as session:
        offer = session.query(Offer).filter(
            Offer.product_id == product_id).first()
        session.add(PricePoint(offer_id=offer.id, price=Decimal("500"),
                               currency="CAD"))
    out = asyncio.run(service.price_history(product_id))
    summary = out["summary"]
    assert summary["currency"] == "USD"
    assert summary["highest_seen"] == 100.0     # the CAD 500 point is excluded


def test_missing_product():
    out = asyncio.run(service.price_history(999999))
    assert out["ok"] is False
