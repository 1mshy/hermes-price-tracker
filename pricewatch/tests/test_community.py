"""Community (Reddit RSS) parsing, against feeds captured from the live site."""
import asyncio
import pathlib

from pricewatch import community
from pricewatch.fetch import Page

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SEARCH_XML = (FIXTURES / "reddit_search.xml").read_text()
THREAD_XML = (FIXTURES / "reddit_thread.xml").read_text()


def test_parse_search_feed():
    items = community.parse_feed(SEARCH_XML)
    assert len(items) >= 20
    first = items[0]
    assert first["title"]
    assert first["url"].startswith("https://www.reddit.com/")
    assert first["posted_at"]


def test_parse_thread_feed_finds_price_claims():
    items = community.parse_feed(THREAD_XML)
    assert items
    all_prices = [p for item in items for p in item["prices_mentioned"]]
    assert "$50" in all_prices


def test_parse_feed_garbage_is_safe():
    assert community.parse_feed("<html>not a feed</html>") == []
    assert community.parse_feed("") == []


def test_clean_html_strips_boilerplate():
    raw = ("&lt;p&gt;Great &lt;b&gt;deal&lt;/b&gt;&lt;/p&gt; submitted by "
           "/u/someone to r/3Dprinting [link] [comments]")
    assert community._clean_html(raw) == "Great deal"


def _patch_fetch(monkeypatch, xml: str, status: int = 200):
    async def fake_get(url, **kwargs):
        return Page(url, status, xml, "http")
    monkeypatch.setattr(community.fetcher, "get", fake_get)


def test_search_returns_recent_posts(monkeypatch):
    _patch_fetch(monkeypatch, SEARCH_XML)
    out = asyncio.run(community.search("sunlu ams heater",
                                       subreddits=["BambuLab"], days=3650))
    assert out["count"] > 0
    assert not out["errors"]
    assert out["posts"][0]["posted_at"] >= out["posts"][-1]["posted_at"]


def test_search_reports_rate_limit(monkeypatch):
    monkeypatch.setattr(community, "_REDDIT_RETRY_DELAY", 0.01)
    _patch_fetch(monkeypatch, "", status=429)
    out = asyncio.run(community.search("anything", subreddits=["BambuLab"]))
    assert out["count"] == 0
    assert "429" in out["errors"]["BambuLab"]


def test_search_aborts_early_when_reddit_is_angry(monkeypatch):
    monkeypatch.setattr(community, "_REDDIT_RETRY_DELAY", 0.01)
    monkeypatch.setattr(community, "_REDDIT_FEED_GAP", 0.01)
    _patch_fetch(monkeypatch, "", status=429)
    out = asyncio.run(community.search(
        "anything", subreddits=["a", "b", "c", "d"]))
    assert out["count"] == 0
    # first two hit the wall for real; the rest are skipped without retries
    assert "skipped" in out["errors"]["c"]
    assert "skipped" in out["errors"]["d"]


def test_thread_reader(monkeypatch):
    _patch_fetch(monkeypatch, THREAD_XML)
    out = asyncio.run(community.thread(
        "https://www.reddit.com/r/3Dprinting/comments/1vz3k0h/x/?share=1"))
    assert out["ok"]
    assert out["post"] is not None
    assert "$50" in str(out)


def test_thread_rejects_non_reddit():
    out = asyncio.run(community.thread("https://example.com/thing"))
    assert not out["ok"]
