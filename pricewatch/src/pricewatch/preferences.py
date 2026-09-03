"""What the user wants to be quoted in, and where they shop.

Starts from PW_PREFERRED_CURRENCY / PW_REGION but can be changed live (by the
agent via the set_preferred_currency MCP tool, or PATCH /api/locale) and is
stored in the settings table, so a container restart keeps it — same pattern
as the sweep schedule in scheduler.py.

Nothing here converts money for display. A preference changes which storefront
the engine searches and how a result is labelled; every price is still reported
in the currency the store actually charges.
"""
from __future__ import annotations

import logging

from . import fx
from .money import REGION_CURRENCY, currency_for_region, region_for_currency
from .settings import settings

log = logging.getLogger(__name__)

_CURRENCY_KEY = "preferred_currency"
_REGION_KEY = "preferred_region"

#: Currencies the engine can reason about — anything outside this set has no
#: indicative rate, so it could neither sort nor annotate results honestly.
SUPPORTED = tuple(sorted(set(REGION_CURRENCY.values())))

#: (currency, region, source) or None until the DB has been consulted once.
_state: tuple[str, str, str] | None = None


class LocaleError(ValueError):
    """The requested currency or region is not one the engine supports."""


def _saved() -> tuple[str, str] | None:
    try:
        from .db import session_scope
        from .models import Setting
        with session_scope() as session:
            currency = session.get(Setting, _CURRENCY_KEY)
            if currency is None:
                return None
            region = session.get(Setting, _REGION_KEY)
            return currency.value, (region.value if region else "")
    except Exception:                                  # noqa: BLE001
        log.debug("no saved currency preference", exc_info=True)
        return None


def _save(currency: str | None, region: str | None) -> None:
    """Persist an override; None removes it (revert to the env default)."""
    from .db import session_scope
    from .models import Setting
    with session_scope() as session:
        for key, value in ((_CURRENCY_KEY, currency), (_REGION_KEY, region)):
            row = session.get(Setting, key)
            if not value:
                if row is not None:
                    session.delete(row)
            elif row is None:
                session.add(Setting(key=key, value=value))
            else:
                row.value = value


def _from_env() -> tuple[str, str, str]:
    currency = (settings.pw_preferred_currency or "").strip().upper()
    region = (settings.pw_region or "").strip().upper()
    # Either one implies the other: a CAD shopper wants .ca storefronts, and a
    # CA shopper wants CAD, so a half-filled .env still behaves sensibly.
    region = region or region_for_currency(currency)
    currency = currency or currency_for_region(region)
    return currency, region, "env"


def _ensure() -> tuple[str, str, str]:
    global _state
    if _state is None:
        saved = _saved()
        if saved:
            currency, region = saved
            _state = (currency, region or region_for_currency(currency), "runtime-override")
        else:
            _state = _from_env()
    return _state


def preferred_currency() -> str:
    """ISO-4217 code the agent should answer in, or "" for no preference."""
    return _ensure()[0]


def preferred_region() -> str:
    """ISO-3166 country whose storefronts to prefer, or "" for no preference."""
    return _ensure()[1]


def current() -> dict:
    currency, region, source = _ensure()
    env_currency, env_region, _ = _from_env()
    rates = fx.status()
    return {
        "preferred_currency": currency or None,
        "preferred_region": region or None,
        "source": source,                # "env" | "runtime-override"
        "default_currency": env_currency or None,
        "default_region": env_region or None,
        "supported_currencies": list(SUPPORTED),
        # Which day's rate stands behind any approx_in_preferred figure —
        # None means the static fallback is in force (see get_exchange_rates).
        "rates_as_of": rates["as_of"],
        "rates_source": rates["source"],
        "note": (
            f"Quote prices in {currency} where the store bills in {currency}. "
            "Stores that bill in another currency keep their own figure — say "
            "which currency it is; the approx_in_preferred field beside it is "
            "an indicative conversion for comparison only."
        ) if currency else "no currency preference set — results are reported as each store bills",
    }


def set_preference(currency: str | None, region: str | None = None) -> dict:
    """Apply a preference now and persist it. None/''/'default' → env default."""
    global _state
    requested = (currency or "").strip().upper()
    wanted_region = (region or "").strip().upper()

    if requested in ("", "DEFAULT", "RESET", "NONE"):
        _save(None, None)
        _state = _from_env()
        log.info("currency preference reset to env default (%s)", _state[0] or "none")
        return current()

    if requested not in SUPPORTED:
        raise LocaleError(
            f"{requested!r} is not a supported currency — the engine has no "
            f"indicative rate for it. Choose one of: {', '.join(SUPPORTED)}")
    if wanted_region and wanted_region not in REGION_CURRENCY:
        raise LocaleError(
            f"{wanted_region!r} is not a region the engine knows a storefront "
            f"for. Choose one of: {', '.join(sorted(REGION_CURRENCY))}")

    wanted_region = wanted_region or region_for_currency(requested)
    _save(requested, wanted_region)
    _state = (requested, wanted_region, "runtime-override")
    log.info("currency preference set to %s (region %s)", requested, wanted_region)
    return current()


def reload() -> None:
    """Drop the cache so the next read re-consults the DB (startup, tests)."""
    global _state
    _state = None
