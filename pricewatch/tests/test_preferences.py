from decimal import Decimal

import pytest

from pricewatch import preferences
from pricewatch.db import init_db
from pricewatch.money import (convert, currency_for_host, currency_for_region,
                              region_for_currency, region_for_host)
from pricewatch.stores.aliexpress import _locale_cookie
from pricewatch.stores.base import StoreAdapter, StoreResult


@pytest.fixture
def prefers():
    """Pin a preference for one test, then restore the env default."""
    def _set(currency: str, region: str = ""):
        preferences._state = (currency, region or region_for_currency(currency),
                              "runtime-override")
    yield _set
    preferences.reload()


# ── region ↔ currency ────────────────────────────────────────────────────
def test_currency_and_region_imply_each_other():
    assert currency_for_region("CA") == "CAD"
    assert currency_for_region("DE") == "EUR"
    assert region_for_currency("CAD") == "CA"
    assert currency_for_region("XX", "USD") == "USD"


def test_region_from_hostname():
    assert region_for_host("ca.store.bambulab.com") == "CA"
    assert region_for_host("us.elegoo.com") == "US"
    assert region_for_host("amazon.ca") == "CA"
    assert region_for_host("amazon.co.uk") == "GB"        # not read as ".uk"
    assert region_for_host("www.sunlu.com") == "US"


def test_currency_from_hostname():
    assert currency_for_host("ca.store.bambulab.com") == "CAD"
    assert currency_for_host("eu.store.bambulab.com") == "EUR"
    assert currency_for_host("printedsolid.com") == "USD"
    assert currency_for_host("", "USD") == "USD"


# ── indicative conversion ────────────────────────────────────────────────
def test_convert_is_indicative_and_symmetric_ish():
    assert convert(Decimal("100"), "USD", "USD") == Decimal("100")
    cad = convert(Decimal("100"), "USD", "CAD")
    assert Decimal("130") < cad < Decimal("145")          # ~1/0.73
    assert convert(None, "USD", "CAD") is None
    assert convert(Decimal("10"), "USD", "XYZ") is None


# ── what the agent sees ──────────────────────────────────────────────────
def test_no_preference_leaves_results_untouched():
    result = StoreResult(store="west3d", url="u", price=Decimal("42"), currency="USD")
    assert "approx_in_preferred" not in result.as_dict()


def test_foreign_price_is_annotated_not_rewritten(prefers):
    prefers("CAD", "CA")
    payload = StoreResult(store="west3d", url="u", price=Decimal("100"),
                          currency="USD").as_dict()
    # The store's own figure is untouched…
    assert payload["price"] == 100.0
    assert payload["currency"] == "USD"
    # …and the CAD number beside it is explicitly not a quote.
    assert payload["approx_in_preferred"]["currency"] == "CAD"
    assert payload["approx_in_preferred"]["amount"] > 100.0
    assert "not a quote" in payload["approx_in_preferred"]["basis"]


def test_native_currency_needs_no_annotation(prefers):
    prefers("CAD", "CA")
    payload = StoreResult(store="bambulab", url="u", price=Decimal("999"),
                          currency="CAD").as_dict()
    assert "approx_in_preferred" not in payload


# ── storefront selection ─────────────────────────────────────────────────
def _adapter(*domains):
    adapter = StoreAdapter()
    adapter.domains = domains
    return adapter


def test_storefront_defaults_to_primary_domain():
    assert _adapter("us.store.bambulab.com", "ca.store.bambulab.com").storefront() \
        == "us.store.bambulab.com"


def test_storefront_prefers_the_users_region(prefers):
    prefers("CAD", "CA")
    assert _adapter("us.store.bambulab.com", "ca.store.bambulab.com").storefront() \
        == "ca.store.bambulab.com"


def test_storefront_falls_back_to_a_shared_currency(prefers):
    # No German storefront, but the euro one bills the right money.
    prefers("EUR", "DE")
    assert _adapter("us.store.bambulab.com", "eu.store.bambulab.com").storefront() \
        == "eu.store.bambulab.com"


def test_storefront_keeps_primary_when_no_regional_option(prefers):
    prefers("CAD", "CA")
    assert _adapter("us.elegoo.com", "elegoo.com").storefront() == "us.elegoo.com"


def test_aliexpress_locale_follows_the_preference(prefers):
    assert "c_tp=USD" in _locale_cookie() and "region=US" in _locale_cookie()
    prefers("CAD", "CA")
    assert "c_tp=CAD" in _locale_cookie()
    assert "region=CA" in _locale_cookie()


# ── setting it at runtime ────────────────────────────────────────────────
def test_set_and_reset_preference_round_trips():
    init_db()
    try:
        state = preferences.set_preference("CAD")
        assert state["preferred_currency"] == "CAD"
        assert state["preferred_region"] == "CA"          # derived
        assert state["source"] == "runtime-override"
        assert preferences.preferred_currency() == "CAD"

        preferences.reload()                              # as if restarted
        assert preferences.preferred_currency() == "CAD"  # persisted
    finally:
        preferences.set_preference("default")
    assert preferences.preferred_currency() == ""


def test_unsupported_currency_is_refused():
    with pytest.raises(preferences.LocaleError):
        preferences.set_preference("XYZ")
    with pytest.raises(preferences.LocaleError):
        preferences.set_preference("CAD", region="XX")
