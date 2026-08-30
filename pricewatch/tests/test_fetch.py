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


def test_plain_404_is_not_a_challenge():
    # Amazon's "Page Not Found" body carries the line "To discuss automated
    # access to Amazon data please contact api-services-support@amazon.com",
    # which is a CHALLENGE_MARKERS entry. Read as a wall, a retired ASIN costs
    # a ~35s browser escalation and reports a bot challenge that never happened.
    body = ("<html><title>Page Not Found</title><body>Sorry! We couldn't find "
            "that page. To discuss automated access to Amazon data please "
            "contact api-services-support@amazon.com</body></html>")
    assert not Page("https://www.amazon.ca/dp/B08MVFJ1RJ", 404, body, "http").looks_blocked


def test_body_markers_still_apply_on_a_200():
    body = "<html>Enter the characters you see below</html>"
    assert Page("https://www.amazon.ca/dp/B0BKVY4WKT", 200, body, "http").looks_blocked
