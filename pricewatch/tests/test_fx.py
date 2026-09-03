"""Exchange rates: ECB reference via Frankfurter, static table as the fallback.

Offline — the provider is replaced by a fixture shaped like the real payload
(`fixtures/frankfurter_usd.json`, trimmed from a live response).
"""
import datetime as dt
import json
import pathlib
from decimal import Decimal

import pytest

from pricewatch import fx, preferences, scheduler
from pricewatch.db import init_db
from pricewatch.fetch import fetcher
from pricewatch.money import REGION_CURRENCY, convert, region_for_currency, usd_sort_key
from pricewatch.settings import settings
from pricewatch.stores.base import StoreResult

FIXTURE = json.loads((pathlib.Path(__file__).parent / "fixtures" / "frankfurter_usd.json").read_text())


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Every test starts on the static table and never reaches the network."""
    init_db()
    fx._reset()

    async def refuse(url, **kwargs):
        raise AssertionError(f"unexpected network call: {url}")
    monkeypatch.setattr(fetcher, "get_json", refuse)
    yield
    fx._reset()


@pytest.fixture
def provider(monkeypatch):
    """Serve a payload (default: the fixture) in place of Frankfurter."""
    def _serve(payload=FIXTURE):
        calls = []

        async def fake_get_json(url, **kwargs):
            calls.append(url)
            return payload
        monkeypatch.setattr(fetcher, "get_json", fake_get_json)
        return calls
    return _serve


@pytest.fixture
def prefers():
    def _set(currency: str):
        preferences._state = (currency, region_for_currency(currency), "runtime-override")
    yield _set
    preferences.reload()


def _run(coro):
    import asyncio
    return asyncio.run(coro)


# ── the table ────────────────────────────────────────────────────────────
def test_static_table_covers_exactly_the_engines_currencies():
    assert set(fx.STATIC_USD_RATE) == set(REGION_CURRENCY.values())


def test_normalisation_inverts_units_per_usd_into_usd_per_unit():
    state = fx._normalise({"base": "USD", "date": "2026-09-01", "rates": {"CAD": 1.36}})
    assert state["as_of"] == "2026-09-01"
    assert state["rates"]["USD"] == "1"
    assert abs(Decimal(state["rates"]["CAD"]) - Decimal("0.735")) < Decimal("0.001")


def test_only_known_currencies_are_kept(provider):
    provider()
    out = _run(fx.refresh())
    assert out["ok"] is True
    assert out["currencies"] == sorted(fx.STATIC_USD_RATE)
    assert "BRL" not in out["rates"] and "MXN" not in out["rates"]


@pytest.mark.parametrize("payload", [
    {"base": "EUR", "date": "2026-09-02", "rates": {"USD": 1.16}},   # wrong base
    {"base": "USD", "rates": {"CAD": 1.39}},                         # no date
    {"base": "USD", "date": "2026-09-02"},                           # no rates
    {"base": "USD", "date": "2026-09-02", "rates": {"BRL": 5.1}},    # nothing usable
    "not even json",
])
def test_unusable_payload_is_refused_and_state_untouched(provider, payload):
    provider(payload)
    out = _run(fx.refresh())
    assert out["ok"] is False and out["error"]
    assert out["source"] == "static"
    assert fx.rate_to_usd("CAD") == Decimal("0.73")


# ── what money.py sees ───────────────────────────────────────────────────
def test_rate_to_usd_falls_back_to_static_when_nothing_loaded():
    assert fx.rate_to_usd("CAD") == Decimal("0.73")
    assert fx.rate_to_usd(None) == Decimal("1")
    assert fx.rate_to_usd("XYZ") is None
    assert fx.status()["source"] == "static"


def test_convert_and_sort_key_follow_a_loaded_live_rate(provider):
    provider()
    _run(fx.refresh())
    live_cad = Decimal(1) / Decimal("1.3925")
    assert abs(fx.rate_to_usd("CAD") - live_cad) < Decimal("0.000001")
    assert usd_sort_key(Decimal("100"), "CAD") == Decimal("100") * fx.rate_to_usd("CAD")
    assert convert(Decimal("100"), "USD", "CAD") == Decimal("139.25")
    # The ordering the static table was tuned for still holds on live rates.
    assert usd_sort_key(Decimal("96"), "CAD") < usd_sort_key(Decimal("99"), "USD")
    assert usd_sort_key(Decimal("142"), "CAD") > usd_sort_key(Decimal("99"), "USD")


def test_live_table_missing_a_currency_uses_static_for_that_one(provider):
    provider({"base": "USD", "date": "2026-09-02", "rates": {"CAD": 1.3925}})
    _run(fx.refresh())
    assert fx.status()["source"] == "live"
    assert fx.rate_to_usd("EUR") == Decimal("1.08")


# ── persistence and failure ──────────────────────────────────────────────
def test_refresh_persists_and_load_round_trips(provider):
    calls = provider()
    out = _run(fx.refresh())
    assert calls == [f"{settings.pw_fx_url}?base=USD"]
    assert out["source"] == "live" and out["as_of"] == "2026-09-02"

    fx._reset()
    assert fx.status()["source"] == "static"
    fx.load()                                          # as if restarted, no network
    after = fx.status()
    assert after["source"] == "live"
    assert after["as_of"] == out["as_of"] and after["fetched_at"] == out["fetched_at"]
    assert after["rates"] == out["rates"]


def test_failed_fetch_keeps_previous_live_table(provider, monkeypatch):
    provider()
    before = _run(fx.refresh())

    async def down(url, **kwargs):
        raise RuntimeError("HTTP 503 for " + url)
    monkeypatch.setattr(fetcher, "get_json", down)
    out = _run(fx.refresh())
    assert out["ok"] is False
    assert "503" in out["error"]
    assert out["source"] == "live" and out["as_of"] == before["as_of"]
    assert out["rates"] == before["rates"]
    assert fx.status()["last_error"] == out["error"]


def test_failed_fetch_with_nothing_loaded_reports_static(monkeypatch):
    async def down(url, **kwargs):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(fetcher, "get_json", down)
    out = _run(fx.refresh())
    assert out["ok"] is False and out["source"] == "static" and out["stale"] is True
    assert fx.rate_to_usd("CAD") == Decimal("0.73")


def test_disabled_is_a_noop(monkeypatch):
    monkeypatch.setattr(settings, "pw_fx_enabled", False)
    out = _run(fx.refresh())                           # get_json would raise if called
    assert out["ok"] is False and out["source"] == "static"
    assert out["enabled"] is False
    assert "PW_FX_ENABLED" in out["error"]


# ── freshness ────────────────────────────────────────────────────────────
def test_stale_without_rates_and_once_the_fetch_is_old(provider, monkeypatch):
    assert fx.status()["stale"] is True
    assert fx.status()["age_hours"] is None

    provider()
    _run(fx.refresh())
    fresh = fx.status()
    assert fresh["stale"] is False
    assert fresh["age_hours"] == 0.0

    fetched = dt.datetime.fromisoformat(fresh["fetched_at"])
    monkeypatch.setattr(fx, "_now", lambda: fetched + dt.timedelta(hours=40))
    old = fx.status()
    assert old["age_hours"] == 40.0
    assert old["stale"] is True
    assert old["source"] == "live"                      # still used, just flagged


def test_status_says_what_the_rates_are_for():
    st = fx.status()
    assert "never quote" in st["note"]
    assert st["currencies"] == sorted(st["rates"])
    assert st["rates"]["USD"] == 1.0


# ── the hint beside a foreign price ──────────────────────────────────────
def test_approx_in_preferred_names_the_static_fallback(prefers):
    prefers("CAD")
    hint = StoreResult(store="s", url="u", price=Decimal("100"), currency="USD").approx_in_preferred()
    assert hint["rate_source"] == "static" and hint["rate_as_of"] is None
    assert "static fallback" in hint["basis"] and "not a quote" in hint["basis"]
    assert hint["amount"] == pytest.approx(136.99)


def test_approx_in_preferred_names_the_ecb_day(prefers, provider):
    prefers("CAD")
    provider()
    _run(fx.refresh())
    hint = StoreResult(store="s", url="u", price=Decimal("100"), currency="USD").approx_in_preferred()
    assert hint["rate_source"] == "live" and hint["rate_as_of"] == "2026-09-02"
    assert "ECB reference rate for 2026-09-02" in hint["basis"]
    assert "not a quote" in hint["basis"]
    assert hint["amount"] == 139.25


def test_preferences_report_the_rate_day(prefers, provider):
    prefers("CAD")
    assert preferences.current()["rates_as_of"] is None
    assert preferences.current()["rates_source"] == "static"
    provider()
    _run(fx.refresh())
    assert preferences.current()["rates_as_of"] == "2026-09-02"
    assert preferences.current()["rates_source"] == "live"


# ── the refresh job ──────────────────────────────────────────────────────
def test_fx_cron_accepts_hourly_and_the_default(monkeypatch):
    default = settings.pw_fx_refresh_cron
    assert scheduler.fx_trigger() is not None
    monkeypatch.setattr(settings, "pw_fx_refresh_cron", "0 * * * *")
    assert str(scheduler.fx_trigger()) != str(scheduler._parse_cron(default))


def test_fx_cron_below_hourly_falls_back_to_default(monkeypatch, caplog):
    default = settings.pw_fx_refresh_cron
    for bad in ("*/5 * * * *", "* * * * *", "often please"):
        monkeypatch.setattr(settings, "pw_fx_refresh_cron", bad)
        with caplog.at_level("WARNING", logger="pricewatch.scheduler"):
            trigger = scheduler.fx_trigger()
        assert str(trigger) == str(scheduler._parse_cron(default))
        assert "PW_FX_REFRESH_CRON rejected" in caplog.text


def test_scheduled_refresh_never_raises(monkeypatch):
    async def explode():
        raise RuntimeError("boom")
    monkeypatch.setattr(fx, "refresh", explode)
    _run(scheduler.refresh_rates())                    # logs, does not propagate
