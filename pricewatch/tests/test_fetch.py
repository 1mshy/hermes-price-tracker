from pricewatch.fetch import Page


def test_blocked_by_status():
    assert Page("https://x.com/", 403, "x" * 5000, "http").looks_blocked
    assert Page("https://x.com/", 429, "x" * 5000, "http").looks_blocked
    assert Page("https://x.com/", 503, "x" * 5000, "http").looks_blocked


def test_blocked_by_body_marker():
    body = "<html><title>Just a moment...</title></html>"
    assert Page("https://x.com/", 200, body, "http").looks_blocked


def test_blocked_by_challenge_redirect_url():
    # eBay's splashui challenge returns 200 with a clean body — only the
    # final URL gives it away.
    url = "https://www.ebay.com/splashui/challenge?ap=1&ru=https%3A%2F%2Fwww.ebay.com%2F"
    assert Page(url, 200, "<html>welcome</html>", "http").looks_blocked


def test_normal_page_not_blocked():
    page = Page("https://shop.example.com/products/x", 200,
                "<html><body>Product page</body></html>", "http")
    assert not page.looks_blocked
