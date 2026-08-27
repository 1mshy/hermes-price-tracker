from decimal import Decimal

from pricewatch.extract import extract


def test_jsonld_product():
    html = """
    <html><head><script type="application/ld+json">
    {"@type": "Product", "name": "Test Printer", "sku": "TP-1",
     "offers": {"@type": "Offer", "price": "299.00", "priceCurrency": "USD",
                "availability": "https://schema.org/InStock"}}
    </script></head><body></body></html>
    """
    found = extract(html)
    assert found.ok
    assert found.price == Decimal("299.00")
    assert found.currency == "USD"
    assert found.title == "Test Printer"
    assert found.in_stock is True
    assert found.method == "json-ld"


def test_jsonld_prefers_in_stock_over_cheaper_out_of_stock():
    html = """
    <html><head><script type="application/ld+json">
    {"@type": "Product", "name": "Widget", "offers": [
      {"@type": "Offer", "price": "10.00", "priceCurrency": "USD",
       "availability": "OutOfStock"},
      {"@type": "Offer", "price": "12.00", "priceCurrency": "USD",
       "availability": "InStock"}]}
    </script></head><body></body></html>
    """
    found = extract(html)
    assert found.price == Decimal("12.00")
    assert found.in_stock is True


def test_jsonld_product_group_variants():
    html = """
    <html><head><script type="application/ld+json">
    {"@type": "ProductGroup", "name": "Filament",
     "hasVariant": [{"@type": "Product",
                     "offers": {"@type": "Offer", "price": 18.99,
                                "priceCurrency": "USD",
                                "availability": "InStock"}}]}
    </script></head><body></body></html>
    """
    found = extract(html)
    assert found.price == Decimal("18.99")


def test_jsonld_trailing_comma_tolerated():
    html = """
    <html><head><script type="application/ld+json">
    {"@type": "Product", "name": "X",
     "offers": {"price": "42.00", "priceCurrency": "USD",},}
    </script></head><body></body></html>
    """
    found = extract(html)
    assert found.price == Decimal("42.00")


def test_meta_fallback():
    html = """
    <html><head>
    <meta property="og:title" content="Nozzle"/>
    <meta property="product:price:amount" content="24.50"/>
    <meta property="product:price:currency" content="EUR"/>
    </head><body></body></html>
    """
    found = extract(html)
    assert found.price == Decimal("24.50")
    assert found.currency == "EUR"
    assert found.method == "meta"


def test_amazon_price_amount_pair():
    html = ('<html><head><title>Amazon thing</title></head><body>'
            '<script>{"priceAmount":49.99,"currencySymbol":"$"}</script>'
            '</body></html>')
    found = extract(html)
    assert found.price == Decimal("49.99")
    assert found.currency == "USD"
    assert found.method == "price-amount"


def test_embedded_json_price():
    html = ('<html><head><title>Store</title></head><body>'
            '<script>{"currentPrice":"129.99"}</script></body></html>')
    found = extract(html)
    assert found.price == Decimal("129.99")
    assert found.method == "embedded-json"


def test_no_price_anywhere():
    assert not extract("<html><body><p>hello</p></body></html>").ok
