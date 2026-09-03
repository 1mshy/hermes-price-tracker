"""Indicative exchange rates: an ECB reference rate, with a static fallback.

Prices are never converted for display. A rate serves exactly two things —
ordering mixed-currency results (money.usd_sort_key) and the
approx_in_preferred hint beside a foreign listing (money.convert) — and both
used a hand-maintained table until now. That table stays as the fallback;
when the network allows, the ECB reference rate published by Frankfurter
(free, keyless) takes over, so the approximation is a rate of the day rather
than of whenever the table was last touched. Which one is in force is always
reported (status(), the hint's rate_source), never hidden.

The live table is cached in the settings table so a restart starts from the
last good fetch, and a failed refresh keeps whatever was there before.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from decimal import Decimal, InvalidOperation

from .fetch import fetcher
from .settings import settings

log = logging.getLogger(__name__)

_SETTING_KEY = "fx_rates"
PROVIDER = "frankfurter"
#: Bounds the fetcher's own retries; a rate refresh is never worth stalling
#: an API caller for longer than this.
_REFRESH_TIMEOUT = 30.0

#: USD per one unit of each currency — the fallback, and the definition of
#: which currencies the engine can reason about at all (money.REGION_CURRENCY
#: maps regions onto exactly these; money imports this module, not the other
#: way round, which is why the list lives here).
STATIC_USD_RATE: dict[str, Decimal] = {
    "USD": Decimal("1"), "CAD": Decimal("0.73"), "EUR": Decimal("1.08"),
    "GBP": Decimal("1.27"), "AUD": Decimal("0.65"), "JPY": Decimal("0.0066"),
    "CHF": Decimal("1.12"), "SEK": Decimal("0.095"), "PLN": Decimal("0.25"),
    "CZK": Decimal("0.043"),
}

_NOTE = ("USD per unit of each currency, used only to order mixed-currency "
         "results and for the approx_in_preferred hint — never quote a "
         "converted figure as a store's price")

#: The last good live table, or None while only the static one is available:
#: {"provider", "base", "as_of", "fetched_at", "rates": {code: "0.7181", …}}.
#: Rates are kept as Decimal strings so the JSON copy round-trips exactly.
_state: dict | None = None
_last_error: str | None = None


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ── persistence ──────────────────────────────────────────────────────────
def _validated(raw) -> dict | None:
    """A stored or normalised table, or None if it is not one we can use."""
    if not isinstance(raw, dict) or not isinstance(raw.get("rates"), dict):
        return None
    if not raw.get("as_of") or not raw.get("fetched_at"):
        return None
    try:
        rates = {str(k).upper(): str(Decimal(str(v))) for k, v in raw["rates"].items()}
    except InvalidOperation:
        return None
    return {"provider": str(raw.get("provider") or PROVIDER), "base": "USD",
            "as_of": str(raw["as_of"]), "fetched_at": str(raw["fetched_at"]),
            "rates": rates}


def _saved() -> dict | None:
    try:
        from .db import session_scope
        from .models import Setting
        with session_scope() as session:
            row = session.get(Setting, _SETTING_KEY)
            return _validated(json.loads(row.value)) if row else None
    except Exception:                                  # noqa: BLE001
        log.debug("no saved exchange rates", exc_info=True)
        return None


def _save(state: dict) -> None:
    from .db import session_scope
    from .models import Setting
    # Compact: Setting.value is a 500-char column and the table is ~250.
    value = json.dumps(state, separators=(",", ":"), sort_keys=True)
    with session_scope() as session:
        row = session.get(Setting, _SETTING_KEY)
        if row is None:
            session.add(Setting(key=_SETTING_KEY, value=value))
        else:
            row.value = value


def load() -> None:
    """Pick up the last persisted table without touching the network (startup)."""
    global _state
    _state = _saved()


def _reset() -> None:
    """Forget everything in memory so the next read is static again (tests)."""
    global _state, _last_error
    _state, _last_error = None, None


# ── the provider ─────────────────────────────────────────────────────────
def _normalise(payload) -> dict:
    """Frankfurter's table into ours.

    With base=USD Frankfurter reports units of foreign currency per dollar
    (CAD 1.39); the engine's table is dollars per unit (CAD 0.72), so every
    rate is inverted here. Currencies the engine has no storefront for are
    dropped — a rate for a currency nothing can be quoted in is noise.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("rates"), dict):
        raise ValueError("unexpected payload: no 'rates' object")
    if str(payload.get("base", "")).upper() != "USD":
        raise ValueError(f"expected a USD base, got {payload.get('base')!r}")
    as_of = str(payload.get("date") or "").strip()
    if not as_of:
        raise ValueError("payload carries no 'date'")

    rates = {"USD": "1"}
    for code, per_usd in payload["rates"].items():
        code = str(code).upper()
        if code not in STATIC_USD_RATE or code == "USD":
            continue
        try:
            per_usd = Decimal(str(per_usd))
        except InvalidOperation:
            continue
        if per_usd <= 0:
            continue
        # Six places keeps four significant digits even for JPY (0.006266).
        rates[code] = str((Decimal(1) / per_usd).quantize(Decimal("0.000001")))
    if len(rates) < 2:
        raise ValueError("payload has no currency the engine knows")
    return {"provider": PROVIDER, "base": "USD", "as_of": as_of,
            "fetched_at": _now().replace(microsecond=0).isoformat(),
            "rates": rates}


async def refresh() -> dict:
    """Fetch today's table, swap it in and persist it. Never raises: on any
    failure the previous table (live or static) stays in force, and the
    returned status says what happened."""
    global _state, _last_error
    if not settings.pw_fx_enabled:
        return {"ok": False, "error": "exchange-rate refresh disabled (PW_FX_ENABLED=false)",
                **status()}
    # base only, no symbols filter: a currency Frankfurter does not carry
    # would turn the whole request into a 404 instead of one missing rate.
    url = f"{settings.pw_fx_url}?base=USD"
    try:
        payload = await asyncio.wait_for(fetcher.get_json(url), timeout=_REFRESH_TIMEOUT)
        fresh = _normalise(payload)
        await asyncio.to_thread(_save, fresh)
    except Exception as exc:                            # noqa: BLE001 — network is messy
        _last_error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        log.warning("exchange-rate refresh failed (%s); keeping %s rates",
                    _last_error, status()["source"])
        return {"ok": False, "error": _last_error, **status()}
    _state, _last_error = fresh, None
    log.info("exchange rates refreshed: ECB reference for %s (%d currencies)",
             fresh["as_of"], len(fresh["rates"]))
    return {"ok": True, **status()}


# ── what the rest of the engine reads ────────────────────────────────────
def rate_to_usd(currency: str | None) -> Decimal | None:
    """USD per one unit of `currency`: live if loaded, else static, else None.

    A live table missing one currency still falls back to the static rate for
    that currency alone — a stale approximation beats losing the ordering.
    """
    code = (currency or "USD").strip().upper()
    if _state is not None:
        live = _state["rates"].get(code)
        if live is not None:
            return Decimal(live)
    return STATIC_USD_RATE.get(code)


def status() -> dict:
    live = _state is not None
    age_hours = None
    if live:
        fetched = dt.datetime.fromisoformat(_state["fetched_at"])
        age_hours = round((_now() - fetched).total_seconds() / 3600, 2)
    table = _state["rates"] if live else STATIC_USD_RATE
    rates = {code: float(rate) for code, rate in table.items()}
    return {
        "source": "live" if live else "static",
        "provider": _state["provider"] if live else None,
        "as_of": _state["as_of"] if live else None,
        "fetched_at": _state["fetched_at"] if live else None,
        "age_hours": age_hours,
        "stale": age_hours is None or age_hours > settings.pw_fx_stale_hours,
        "enabled": settings.pw_fx_enabled,
        "last_error": _last_error,
        "currencies": sorted(rates),
        "rates": rates,
        "note": _NOTE,
    }
