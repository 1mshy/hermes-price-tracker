"""Price parsing. Retail pages express the same number a dozen different ways."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# Ordered: every qualified dollar sign ("CDN$", "CA$", "US$", "A$") is checked
# before the bare "$", which is a substring of all of them — amazon.com shows a
# Canadian visitor "CDN$ 138.83", and that is not USD.
_CURRENCY_SYMBOLS = {
    "US$": "USD", "CDN$": "CAD", "CA$": "CAD", "C$": "CAD", "A$": "AUD",
    "USD": "USD", "CAD": "CAD", "AUD": "AUD",
    "£": "GBP", "GBP": "GBP", "€": "EUR", "EUR": "EUR", "¥": "JPY", "JPY": "JPY",
    "CHF": "CHF", "SEK": "SEK", "PLN": "PLN", "CZK": "CZK",
    "$": "USD",
}

# 1.234,56 (EU) vs 1,234.56 (US) — decide by which separator comes last.
_NUM_RE = re.compile(r"(\d[\d\s., ']*\d|\d)")


def parse_price(raw) -> Decimal | None:
    """Best-effort numeric price from a string, int, float or Decimal."""
    if raw is None:
        return None
    if isinstance(raw, (int, float, Decimal)):
        try:
            value = Decimal(str(raw))
        except InvalidOperation:
            return None
        return value if value > 0 else None

    text = str(raw).strip()
    if not text:
        return None

    text = text.replace("\xa0", " ").replace("\u202f", " ")
    match = _NUM_RE.search(text)
    if not match:
        return None
    number = re.sub(r"[\s' ]", "", match.group(1))

    last_comma, last_dot = number.rfind(","), number.rfind(".")
    if last_comma >= 0 and last_dot >= 0:
        if last_comma > last_dot:             # 1.234,56
            number = number.replace(".", "").replace(",", ".")
        else:                                 # 1,234.56
            number = number.replace(",", "")
    elif last_comma >= 0:
        # Commas only: "12,99" is a decimal comma, but "1,299" (and
        # "1,299,000") are US thousands groups — a 3-digit tail or several
        # commas means separator, not cents.
        tail = number.split(",")[-1]
        decimal_comma = len(tail) == 2 and number.count(",") == 1
        number = number.replace(",", "." if decimal_comma else "")
    elif number.count(".") > 1:               # 1.234.567 — EU thousands only
        number = number.replace(".", "")
    try:
        value = Decimal(number)
    except InvalidOperation:
        return None
    return value if value > 0 else None


def detect_currency(text: str | None, default: str = "USD") -> str:
    if not text:
        return default
    upper = str(text).upper()
    for token in ("USD", "CAD", "EUR", "GBP", "JPY", "AUD", "CHF", "SEK", "PLN", "CZK"):
        if token in upper:
            return token
    for symbol, code in _CURRENCY_SYMBOLS.items():
        if symbol in str(text):
            return code
    return default


def cents_to_decimal(cents) -> Decimal | None:
    """Shopify reports money as integer cents."""
    if cents is None:
        return None
    try:
        value = Decimal(int(cents)) / 100
    except (TypeError, ValueError, InvalidOperation):
        return None
    return value if value > 0 else None


# Indicative rates for ORDERING mixed-currency results only — never for
# display. Close enough that a CAD 96 listing no longer outranks USD 99;
# quoted prices always keep their original currency.
_INDICATIVE_USD_RATE = {
    "USD": Decimal("1"), "CAD": Decimal("0.73"), "EUR": Decimal("1.08"),
    "GBP": Decimal("1.27"), "AUD": Decimal("0.65"), "JPY": Decimal("0.0066"),
    "CHF": Decimal("1.12"), "SEK": Decimal("0.095"), "PLN": Decimal("0.25"),
    "CZK": Decimal("0.043"),
}


def usd_sort_key(price: Decimal | None, currency: str | None) -> Decimal:
    """Approximate USD value for sorting; unknown currencies sort as-is."""
    if price is None:
        return Decimal("Infinity")
    rate = _INDICATIVE_USD_RATE.get((currency or "USD").upper(), Decimal("1"))
    return price * rate


def convert(amount: Decimal | float | None, frm: str | None, to: str | None) -> Decimal | None:
    """Indicative cross-currency value — for orientation, never for quoting.

    Same static rates as the sort key, so a Canadian shopper can see roughly
    what a USD listing costs them without the engine ever passing the
    converted number off as a real price. None when either side is unknown.
    """
    if amount is None:
        return None
    frm, to = (frm or "USD").upper(), (to or "USD").upper()
    if frm == to:
        return Decimal(amount)
    rate_from, rate_to = _INDICATIVE_USD_RATE.get(frm), _INDICATIVE_USD_RATE.get(to)
    if rate_from is None or rate_to is None:
        return None
    return (Decimal(amount) * rate_from / rate_to).quantize(Decimal("0.01"))


# ─────────────────────────── region ↔ currency ───────────────────────────
# Which storefront a currency belongs to, and back again. Used to pick the
# regional storefront a shopper actually buys from (ca.store.bambulab.com over
# us.store.bambulab.com) and to guess what a shop bills in when its API reports
# a bare number — Shopify's product JSON is the big offender there.
REGION_CURRENCY: dict[str, str] = {
    "US": "USD", "CA": "CAD", "GB": "GBP", "AU": "AUD", "JP": "JPY",
    "CH": "CHF", "SE": "SEK", "PL": "PLN", "CZ": "CZK",
    "EU": "EUR", "DE": "EUR", "FR": "EUR", "IT": "EUR", "ES": "EUR",
    "NL": "EUR", "IE": "EUR", "BE": "EUR", "AT": "EUR",
}
#: One canonical region per currency, for the reverse lookup.
_CURRENCY_REGION = {"USD": "US", "CAD": "CA", "GBP": "GB", "EUR": "EU", "AUD": "AU",
                    "JPY": "JP", "CHF": "CH", "SEK": "SE", "PLN": "PL", "CZK": "CZ"}

#: Regional storefronts announce themselves in the sub-domain (ca.store.…) …
_SUBDOMAIN_REGION = {"us": "US", "ca": "CA", "eu": "EU", "uk": "GB", "gb": "GB",
                     "de": "DE", "fr": "FR", "au": "AU", "jp": "JP"}
#: … or in the TLD. Longest-first so ".co.uk" is not read as ".uk".
_TLD_REGION = ((".co.uk", "GB"), (".co.jp", "JP"), (".com.au", "AU"),
               (".ca", "CA"), (".de", "DE"), (".fr", "FR"), (".it", "IT"),
               (".es", "ES"), (".nl", "NL"), (".eu", "EU"), (".ch", "CH"),
               (".se", "SE"), (".pl", "PL"), (".cz", "CZ"), (".jp", "JP"),
               (".au", "AU"), (".uk", "GB"), (".us", "US"), (".com", "US"))


def currency_for_region(region: str | None, default: str = "") -> str:
    return REGION_CURRENCY.get((region or "").strip().upper(), default)


def region_for_currency(currency: str | None, default: str = "") -> str:
    return _CURRENCY_REGION.get((currency or "").strip().upper(), default)


def region_for_host(host: str | None, default: str = "") -> str:
    """Which market a storefront hostname serves: ca.store.bambulab.com → CA."""
    host = (host or "").strip().lower().removeprefix("www.")
    if not host:
        return default
    label = host.split(".")[0]
    if label in _SUBDOMAIN_REGION and "." in host:
        return _SUBDOMAIN_REGION[label]
    for suffix, region in _TLD_REGION:
        if host.endswith(suffix):
            return region
    return default


def currency_for_host(host: str | None, default: str = "USD") -> str:
    """Best guess at what a storefront bills in, from its hostname alone.

    Only a guess, and only worth using where the store publishes no currency
    at all: anything that reports one should be believed instead.
    """
    return currency_for_region(region_for_host(host), default)


def fmt(amount: Decimal | float | None, currency: str = "USD") -> str:
    if amount is None:
        return "n/a"
    symbol = {"USD": "$", "CAD": "CA$", "EUR": "€", "GBP": "£"}.get(currency, f"{currency} ")
    return f"{symbol}{Decimal(amount):,.2f}"
