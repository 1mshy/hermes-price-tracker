"""Price parsing. Retail pages express the same number a dozen different ways."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_CURRENCY_SYMBOLS = {
    "$": "USD", "US$": "USD", "USD": "USD", "CA$": "CAD", "C$": "CAD", "CAD": "CAD",
    "£": "GBP", "GBP": "GBP", "€": "EUR", "EUR": "EUR", "¥": "JPY", "JPY": "JPY",
    "A$": "AUD", "AUD": "AUD", "CHF": "CHF", "SEK": "SEK", "PLN": "PLN", "CZK": "CZK",
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


def fmt(amount: Decimal | float | None, currency: str = "USD") -> str:
    if amount is None:
        return "n/a"
    symbol = {"USD": "$", "CAD": "CA$", "EUR": "€", "GBP": "£"}.get(currency, f"{currency} ")
    return f"{symbol}{Decimal(amount):,.2f}"
