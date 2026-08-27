from decimal import Decimal

from pricewatch.money import cents_to_decimal, detect_currency, fmt, parse_price


def test_us_format():
    assert parse_price("$1,234.56") == Decimal("1234.56")


def test_eu_format():
    assert parse_price("1.234,56 €") == Decimal("1234.56")


def test_space_thousands():
    assert parse_price("1 299,95") == Decimal("1299.95")


def test_bare_comma_thousands_vs_decimal():
    assert parse_price("1,299") == Decimal("1299")      # 3-digit tail → thousands
    assert parse_price("12,99") == Decimal("12.99")     # 2-digit tail → decimal


def test_currency_prefixes():
    assert parse_price("C$142") == Decimal("142")
    assert parse_price("US$ 49.99") == Decimal("49.99")
    assert parse_price("CAD 138.73") == Decimal("138.73")


def test_numeric_inputs():
    assert parse_price(119) == Decimal("119")
    assert parse_price(49.99) == Decimal("49.99")


def test_garbage_and_zero():
    assert parse_price(None) is None
    assert parse_price("") is None
    assert parse_price("free shipping") is None
    assert parse_price(0) is None


def test_cents_to_decimal():
    assert cents_to_decimal(11999) == Decimal("119.99")
    assert cents_to_decimal("3200") == Decimal("32")
    assert cents_to_decimal(None) is None
    assert cents_to_decimal(0) is None


def test_detect_currency():
    assert detect_currency("CAD 138.73") == "CAD"
    assert detect_currency("€19.99", "USD") == "EUR"
    assert detect_currency("no hints here", "USD") == "USD"


def test_fmt():
    assert fmt(Decimal("1234.5"), "USD") == "$1,234.50"
    assert fmt(None) == "n/a"
