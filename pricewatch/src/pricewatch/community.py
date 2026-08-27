"""Community deal intelligence: what people are actually saying on Reddit.

Why RSS: Reddit's JSON API rejects datacenter/scripted clients outright (403
even with a browser TLS fingerprint), but the public RSS/Atom feeds are served
freely with only rate limiting (429 under bursts). So everything here rides the
`.rss` endpoints — search feeds for discovery, thread feeds for comments — via
the shared throttled fetcher so we stay polite (one request per ~2.5s per host).

This exists because deal chatter routinely precedes or explains price moves
("$50 with the checkout coupon", "clearance at Micro Center"), and the agent
kept hand-rolling this exact scrape one curl at a time.
"""
from __future__ import annotations

import datetime as dt
import html as html_mod
import logging
import re
from urllib.parse import quote

from lxml import etree

from .fetch import fetcher

log = logging.getLogger(__name__)

_ATOM = "{http://www.w3.org/2005/Atom}"

#: Where 3D-printing / tech deal chatter actually happens.
DEFAULT_SUBREDDITS = ("3Dprinting", "BambuLab", "3dbargains", "buildapcsales")


def _default_subreddits() -> tuple[str, ...]:
    """PW_COMMUNITY_SUBREDDITS overrides the built-in set."""
    from .settings import settings
    configured = tuple(
        s.strip() for s in settings.pw_community_subreddits.split(",") if s.strip())
    return configured or DEFAULT_SUBREDDITS

#: Price-looking tokens in free text: "$49.99", "C$142", "CAD 138.73", "€18".
_PRICE_RE = re.compile(
    r"(?:USD|CAD|EUR|GBP|C\$|US\$|A\$|\$|€|£)\s?\d{1,5}(?:[.,]\d{2})?", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

#: Words that mark a post as deal-relevant rather than general discussion.
_DEAL_WORDS = ("deal", "sale", "coupon", "promo", "discount", "clearance",
               "price", "%", "off", "$", "€", "£", "cheap", "drop", "code")


def _clean_html(raw: str | None) -> str:
    """Reddit RSS ships comment bodies as escaped HTML; flatten to plain text."""
    if not raw:
        return ""
    text = html_mod.unescape(raw)
    text = _TAG_RE.sub(" ", text)
    # Feed boilerplate that carries no signal.
    text = text.replace("[link]", " ").replace("[comments]", " ")
    text = re.sub(r"submitted by\s+/u/\S+\s+to\s+r/\S+", " ", text)
    return _WS_RE.sub(" ", text).strip()


def _parse_when(stamp: str | None) -> dt.datetime | None:
    if not stamp:
        return None
    try:
        return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def _prices_in(text: str) -> list[str]:
    seen: list[str] = []
    for match in _PRICE_RE.findall(text):
        token = match.replace(" ", "")
        if token not in seen:
            seen.append(token)
    return seen[:8]


def parse_feed(xml_text: str) -> list[dict]:
    """Parse a Reddit Atom feed (search results or a thread) into plain dicts."""
    try:
        root = etree.fromstring(xml_text.encode("utf-8", "ignore"))
    except etree.XMLSyntaxError:
        return []

    items: list[dict] = []
    for entry in root.iter(_ATOM + "entry"):
        title = _clean_html((entry.findtext(_ATOM + "title") or ""))
        content = _clean_html(entry.findtext(_ATOM + "content"))
        link_node = entry.find(_ATOM + "link")
        link = link_node.get("href") if link_node is not None else ""
        author = (entry.findtext(f"{_ATOM}author/{_ATOM}name") or "").strip()
        when = _parse_when(entry.findtext(_ATOM + "updated")
                           or entry.findtext(_ATOM + "published"))
        category = entry.find(_ATOM + "category")
        subreddit = category.get("label", "") if category is not None else ""

        blob = f"{title} {content}"
        items.append({
            "title": title,
            "url": link,
            "author": author,
            "subreddit": subreddit.removeprefix("r/"),
            "posted_at": when.isoformat() if when else None,
            "snippet": content[:500],
            "prices_mentioned": _prices_in(blob),
            "deal_signals": sorted({w for w in _DEAL_WORDS
                                    if w in blob.lower()})[:6],
            "_when": when,
        })
    return items


#: Reddit rate-limits RSS well below the generic per-host throttle, and blocks
#: bursts outright. One measured backoff retry recovers most 429/403s.
_REDDIT_RETRY_DELAY = 8.0
_REDDIT_FEED_GAP = 4.0


async def _fetch_feed(url: str) -> tuple[list[dict], str | None]:
    """Return (items, error). 429s get one backoff retry; failures degrade to
    an error note so one angry subreddit never sinks the whole search."""
    import asyncio

    last_error = None
    for attempt in range(2):
        try:
            page = await fetcher.get(url, retries=0)
        except Exception as exc:                      # noqa: BLE001 — network
            return [], f"fetch failed: {exc}"
        if page.status in (429, 403):
            last_error = (f"rate-limited by Reddit ({page.status})"
                          " — retry in a minute")
            if attempt == 0:
                await asyncio.sleep(_REDDIT_RETRY_DELAY)
                continue
            return [], last_error
        if page.status != 200:
            return [], f"HTTP {page.status}"
        items = parse_feed(page.text)
        if not items and "<entry" not in page.text:
            return [], "no feed in response (possibly a block page)"
        return items, None
    return [], last_error or "unreachable"


async def search(query: str, subreddits: list[str] | None = None,
                 days: int = 14, limit: int = 20) -> dict:
    """Search recent Reddit posts mentioning a product, newest first.

    One RSS request per subreddit (throttled), so keep the subreddit list
    short. Results include any dollar amounts mentioned, so the agent can
    triage "people say it was $50" claims without opening every thread.
    """
    subs = list(subreddits or _default_subreddits())[:5]
    window = "week" if days <= 7 else "month" if days <= 31 else "year"
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)

    import asyncio

    posts: list[dict] = []
    errors: dict[str, str] = {}
    consecutive_limited = 0
    for index, sub in enumerate(subs):
        if consecutive_limited >= 2:
            # Reddit is refusing this host right now — further feeds will only
            # burn retry delays. Report partial results instead of stalling.
            errors[sub] = "skipped — Reddit is rate-limiting this host"
            continue
        if index:                       # extra spacing beyond the host throttle
            await asyncio.sleep(_REDDIT_FEED_GAP)
        url = (f"https://www.reddit.com/r/{quote(sub)}/search.rss"
               f"?q={quote(query)}&restrict_sr=1&sort=new&t={window}")
        items, error = await _fetch_feed(url)
        if error:
            errors[sub] = error
            consecutive_limited += 1 if "rate-limited" in error else 0
            continue
        consecutive_limited = 0
        for item in items:
            when = item.pop("_when", None)
            if when is not None and when < cutoff:
                continue
            item.setdefault("subreddit", sub)
            item["subreddit"] = item["subreddit"] or sub
            posts.append(item)

    for post in posts:
        post.pop("_when", None)
    posts.sort(key=lambda p: p.get("posted_at") or "", reverse=True)
    return {
        "query": query,
        "subreddits_searched": subs,
        "window_days": days,
        "count": len(posts[:limit]),
        "posts": posts[:limit],
        "errors": errors,
        "note": ("Community chatter is unverified — treat prices as leads and "
                 "confirm with get_price/compare_prices before quoting them."),
    }


async def thread(url: str, max_comments: int = 15) -> dict:
    """Read one Reddit thread (post + top comments) via its RSS feed.

    Accepts any reddit.com comments URL; strips query strings and fetches the
    `.rss` view, which works where the JSON API is blocked.
    """
    clean = url.split("?")[0].rstrip("/")
    if "reddit.com" not in clean:
        return {"ok": False, "error": "not a reddit.com URL", "url": url}
    items, error = await _fetch_feed(clean + ".rss")
    if error:
        return {"ok": False, "error": error, "url": url}
    for item in items:
        item.pop("_when", None)
    post = items[0] if items else None
    comments = items[1:max_comments + 1]
    return {
        "ok": True,
        "url": clean,
        "post": post,
        "comments": comments,
        "comment_count_returned": len(comments),
    }
