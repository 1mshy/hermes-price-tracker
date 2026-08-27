from decimal import Decimal

from pricewatch.money import usd_sort_key
from pricewatch.stores.base import StoreResult


def test_cad_does_not_outrank_cheaper_usd():
    # CAD 96 ≈ USD 70 → genuinely cheaper, keeps its lead.
    assert usd_sort_key(Decimal("96"), "CAD") < usd_sort_key(Decimal("99"), "USD")
    # But CAD 142 ≈ USD 104 must sort AFTER USD 99.
    assert usd_sort_key(Decimal("142"), "CAD") > usd_sort_key(Decimal("99"), "USD")


def test_eur_and_gbp_convert_upward():
    assert usd_sort_key(Decimal("100"), "EUR") > usd_sort_key(Decimal("100"), "USD")
    assert usd_sort_key(Decimal("100"), "GBP") > usd_sort_key(Decimal("100"), "EUR")


def test_unknown_currency_and_none_price():
    assert usd_sort_key(Decimal("50"), "XYZ") == Decimal("50")
    assert usd_sort_key(None, "USD") > Decimal("1000000")


def test_sorting_mixed_results():
    results = [
        StoreResult(store="ca", url="u1", price=Decimal("130"), currency="CAD"),
        StoreResult(store="us", url="u2", price=Decimal("99"), currency="USD"),
        StoreResult(store="eu", url="u3", price=Decimal("89"), currency="EUR"),
    ]
    results.sort(key=lambda r: usd_sort_key(r.price, r.currency))
    # CAD 130 ≈ $95 < EUR 89 ≈ $96 < USD 99
    assert [r.store for r in results] == ["ca", "eu", "us"]
