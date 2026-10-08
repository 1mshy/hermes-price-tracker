"""Polite HTTP and Chromium fetching with recoverable, bounded browser sessions.

HTTP is tried first where appropriate. Chromium handles client-rendered prices
and some access challenges; neither transport guarantees retailer access.
Both honor PW_HTTP_PROXY. Successful strategy hints expire after a day, and
challenge cooldowns expire after five minutes instead of permanently disabling
browser recovery. Dedicated anonymous session state survives container restarts.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import httpx
from lxml import html as lxml_html

from .settings import settings

try:                                    # optional, but strongly recommended
    from curl_cffi.requests import AsyncSession as _CffiSession
except Exception:                       # pragma: no cover - dependency missing
    _CffiSession = None

log = logging.getLogger(__name__)

# Sending a plausible full browser header set (not just a UA string) is the single
# biggest factor in whether a retail CDN serves the real page or a challenge.
BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

CHALLENGE_MARKERS = (
    "px-captcha", "/_incapsula_", "distil_r_captcha", "cf-browser-verification",
    "just a moment...", "enable javascript and cookies to continue",
    "to discuss automated access", "request unsuccessful. incapsula",
    "are you a human", "unusual traffic from your computer", "access denied",
    "robot or human", "verify you are a human", "enter the characters you see",
    "type the characters you see", "/sec_cpt/", "captcha-delivery.com",
)

# Playbook rungs, coarsest first. "http" = a fingerprinted GET returned a real
# page; "browser" = it needed Chromium; "blocked" = even Chromium failed, so the
# host almost certainly needs a proxy or API key and should fail fast.
HTTP, BROWSER, BLOCKED_STRAT = "http", "browser", "blocked"


class Blocked(Exception):
    """Site served a bot challenge rather than the product page."""


def _safe_url(url: str) -> str:
    parts = urlsplit(url)
    secret = {"key", "apikey", "api_key", "access_token", "token", "password"}
    query = [(k, "REDACTED" if k.lower() in secret else v) for k, v in parse_qsl(parts.query)]
    return urlunsplit(parts._replace(query=urlencode(query)))


class _Playbook:
    """Remembers the cheapest fetch rung that worked per host, persisted to disk.

    All IO is best-effort: a read-only or missing data dir must never break
    fetching, so every failure degrades to an in-memory-only cache.
    """

    def __init__(self) -> None:
        self._path = self._resolve_path()
        self._data: dict[str, dict] = {}
        self._dirty = False
        self._load()

    @staticmethod
    def _resolve_path() -> str | None:
        configured = (settings.pw_playbook_path or "").strip()
        if configured.lower() == "off":
            return None
        if configured:
            return configured
        # default: sit next to the SQLite DB when we can find its directory
        url = settings.pw_db_url
        if url.startswith("sqlite") and "/" in url:
            db_path = url.split("///", 1)[-1]
            directory = os.path.dirname(db_path) or "."
            return os.path.join(directory, "fetch_playbook.json")
        return None

    def _load(self) -> None:
        if not self._path or not os.path.exists(self._path):
            return
        try:
            with open(self._path) as handle:
                raw = json.load(handle)
            if isinstance(raw, dict):
                # Legacy entries have no age. Re-probe instead of preserving
                # permanent "blocked" decisions from older versions.
                self._data = {str(k): v for k, v in raw.items()
                              if isinstance(v, dict) and isinstance(v.get("at"), (int, float))}
        except Exception as exc:            # noqa: BLE001
            log.debug("could not load fetch playbook: %s", exc)

    def strategy(self, host: str) -> str | None:
        entry = self._data.get(host.removeprefix("www."), {})
        strategy = entry.get("strategy")
        ttl = (settings.pw_blocked_retry_seconds if strategy == BLOCKED_STRAT
               else settings.pw_strategy_ttl_seconds)
        age = time.time() - entry.get("at", 0)
        if age < 0 or age >= ttl:
            return None
        return strategy if strategy in (HTTP, BROWSER, BLOCKED_STRAT) else None

    def record(self, host: str, strategy: str) -> None:
        host = host.removeprefix("www.")
        self._data[host] = {"strategy": strategy, "at": time.time()}
        self._dirty = True
        self._save()

    def _save(self) -> None:
        if not self._path or not self._dirty:
            return
        try:
            tmp = f"{self._path}.tmp"
            with open(tmp, "w") as handle:
                json.dump(self._data, handle, indent=0, sort_keys=True)
            os.replace(tmp, self._path)
            self._dirty = False
        except Exception as exc:            # noqa: BLE001
            log.debug("could not persist fetch playbook: %s", exc)

    def as_dict(self) -> dict[str, str]:
        return {host: strategy for host in self._data
                if (strategy := self.strategy(host)) is not None}


@dataclass
class Page:
    url: str
    status: int
    text: str
    method: str          # "http" | "browser"

    @property
    def looks_blocked(self) -> bool:
        if self.status in (401, 403, 429) or self.status >= 500:
            return True
        # A plain 404 is a dead product, not a wall. Amazon's own "Page Not
        # Found" body carries the "to discuss automated access" footer, so
        # trusting body markers here would send every retired ASIN to the
        # browser and then report a bot challenge that never happened — and,
        # via get_or_render, pin the whole host to "blocked" in the playbook.
        if self.status == 404:
            return False
        # Some walls (eBay's splashui) redirect to a clean-looking challenge
        # page that returns 200 and none of the body markers.
        final = self.url.lower()
        if "/splashui/challenge" in final or "captcha" in final or urlsplit(final).path.rstrip("/") == "/blocked":
            return True
        head = self.text[:500_000].lower()
        # Loading placeholders and JavaScript strings on real storefronts can
        # contain challenge copy. "Just a moment" is only evidence in the title
        # or a small interstitial, not a 300KB product catalog.
        try:
            doc = lxml_html.fromstring(head)
            title = doc.xpath("string(//title)").strip()
            for node in doc.xpath("//script | //style | //template"):
                node.drop_tree()
            visible = " ".join(doc.text_content().split())
        except Exception:
            title, visible = "", head
        if ("just a moment" in title or "access to this page has been denied" in title
                or title == "verify your identity"):
            return True
        if len(head) < 12_000 and ("powered and protected by" in visible
                                  or "captcha-delivery.com" in head):
            return True
        markers = [m for m in CHALLENGE_MARKERS if m != "just a moment..."]
        return any(m in visible for m in markers) or (
            len(head) < 12_000 and any(m in head for m in CHALLENGE_MARKERS))


class _HostThrottle:
    """One token every 1/rps seconds, per hostname."""

    def __init__(self, rps: float):
        self._min_gap = 1.0 / rps if rps > 0 else 0.0
        self._next: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait(self, host: str) -> None:
        if self._min_gap <= 0:
            return
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            earliest = self._next.get(host, 0.0)
            if earliest > now:
                await asyncio.sleep(earliest - now)
            # jitter keeps a fleet of trackers from marching in lockstep
            self._next[host] = time.monotonic() + self._min_gap * random.uniform(0.85, 1.3)


class Fetcher:
    """Shared HTTP client plus a lazily-started Chromium for walled sites."""

    def __init__(self) -> None:
        self._throttle = _HostThrottle(settings.pw_per_host_rps)
        self._client: httpx.AsyncClient | None = None
        self._cffi: "_CffiSession | None" = None
        self._playwright = None
        self._browser = None
        self._browser_lock = asyncio.Lock()
        self._render_slots = asyncio.Semaphore(max(1, settings.pw_browser_concurrency))
        self._contexts: dict[str, object] = {}
        self._context_lock = asyncio.Lock()
        self.playbook = _Playbook()

    async def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                follow_redirects=True,
                timeout=settings.pw_request_timeout,
                headers={"User-Agent": settings.pw_user_agent, **BROWSER_HEADERS},
                proxy=settings.pw_http_proxy or None,
                http2=True,
            )
        return self._client

    def _cffi_session(self) -> "_CffiSession | None":
        if _CffiSession is None or not settings.pw_impersonate:
            return None
        if self._cffi is None:
            kwargs: dict = {"impersonate": settings.pw_impersonate,
                            "timeout": settings.pw_request_timeout}
            if settings.pw_http_proxy:
                kwargs["proxies"] = {"http": settings.pw_http_proxy,
                                     "https": settings.pw_http_proxy}
            self._cffi = _CffiSession(**kwargs)
        return self._cffi

    async def _get_cffi(self, url: str, headers: dict | None) -> Page | None:
        """One GET with a real-browser TLS/HTTP2 fingerprint; None if unusable."""
        session = self._cffi_session()
        if session is None:
            return None
        response = await session.get(url, headers=headers or None, allow_redirects=True)
        text = response.text or ""
        return Page(str(response.url), response.status_code, text, "http")

    async def get(self, url: str, *, headers: dict | None = None, retries: int = 2) -> Page:
        """Fingerprinted GET first; fall back to plain httpx if that path is gone.

        Returns whatever the transport produced (including challenge pages) —
        callers decide what to do via ``Page.looks_blocked``.
        """
        host = urlsplit(url).netloc
        last: Exception | None = None
        for attempt in range(retries + 1):
            await self._throttle.wait(host)
            try:
                page = await self._get_cffi(url, headers)
                if page is not None:
                    return page
            except Exception as exc:                       # noqa: BLE001 — network is messy
                last = exc
                log.debug("cffi fetch failed for %s: %s", _safe_url(url), type(exc).__name__)
            try:
                client = await self.client()
                response = await client.get(url, headers=headers or None)
                return Page(str(response.url), response.status_code, response.text, "http")
            except Exception as exc:                       # noqa: BLE001 — network is messy
                last = exc
                if attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"GET {_safe_url(url)} failed ({type(last).__name__})")

    async def get_json(self, url: str, *, headers: dict | None = None):
        page = await self.get(url, headers={"Accept": "application/json", **(headers or {})})
        if page.status != 200:
            raise RuntimeError(f"HTTP {page.status} for {_safe_url(url)}")
        import json
        return json.loads(page.text)

    # ── headless browser ────────────────────────────────────────────────
    async def _ensure_browser(self):
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        async with self._browser_lock:
            if self._browser is None or not self._browser.is_connected():
                self._contexts.clear()
                from playwright.async_api import async_playwright
                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    proxy=self._browser_proxy(),
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                )
        return self._browser

    @staticmethod
    def _browser_proxy() -> dict | None:
        if not settings.pw_http_proxy:
            return None
        parsed = urlsplit(settings.pw_http_proxy)
        if not parsed.hostname or not parsed.scheme:
            raise ValueError("PW_HTTP_PROXY must include a scheme and hostname")
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        proxy = {"server": f"{parsed.scheme}://{host}" + (f":{parsed.port}" if parsed.port else "")}
        if parsed.username is not None:
            proxy["username"] = unquote(parsed.username)
        if parsed.password is not None:
            proxy["password"] = unquote(parsed.password)
        return proxy

    @staticmethod
    def _state_path(host: str) -> Path | None:
        configured = settings.pw_browser_state_dir.strip()
        if configured.lower() == "off":
            return None
        if not configured:
            if not settings.pw_db_url.startswith("sqlite"):
                return None
            configured = str(Path(settings.pw_db_url.split("///", 1)[-1]).parent / "browser_sessions")
        # Sessions are isolated by hostname, market preference and proxy route.
        identity = "|".join((host, settings.pw_region, settings.pw_preferred_currency,
                             settings.pw_http_proxy))
        return Path(configured) / (hashlib.sha256(identity.encode()).hexdigest()[:24] + ".json")

    async def _context(self, browser, host: str):
        async with self._context_lock:
            if host in self._contexts:
                context = self._contexts.pop(host)
                # Reserve a page under the lock so eviction cannot close it.
                try:
                    page = await context.new_page()
                except Exception:
                    try:
                        await context.close()
                    except Exception:
                        pass
                else:
                    self._contexts[host] = context
                    return context, page, False
            for key, context in list(self._contexts.items()):
                if len(self._contexts) < max(1, settings.pw_browser_context_limit):
                    break
                if not context.pages:
                    await context.close()
                    del self._contexts[key]
            path = self._state_path(host)
            state = None
            if path and path.exists():
                try:
                    state = json.loads(path.read_text())
                    if not isinstance(state, dict):
                        state = None
                except (OSError, ValueError):
                    pass
            from .preferences import preferred_region
            region = preferred_region() or "US"
            options = dict(locale={"CA": "en-CA", "GB": "en-GB"}.get(region, "en-US"),
                           viewport={"width": 1440, "height": 900})
            # HeadlessChrome in the default UA is rejected by some retailers.
            # Use the actual installed version rather than an obsolete fixed UA.
            options["user_agent"] = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                     f"(KHTML, like Gecko) Chrome/{browser.version} Safari/537.36")
            try:
                context = await browser.new_context(**options, storage_state=state)
            except Exception:
                if state is None:
                    raise
                context = await browser.new_context(**options)
            ephemeral = len(self._contexts) >= max(1, settings.pw_browser_context_limit)
            if not ephemeral:
                self._contexts[host] = context
            return context, await context.new_page(), ephemeral

    async def _save_context(self, context, host: str) -> None:
        path = self._state_path(host)
        if not path:
            return
        try:
            state = await context.storage_state()
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temp = path.with_suffix(".tmp")
            # Cookies can contain session identifiers; do not create a world-readable file.
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(state, handle)
            os.replace(temp, path)
        except Exception:
            log.debug("could not persist storefront browser session for %s", host)

    async def render(self, url: str, *, wait_for: str | None = None, timeout_ms: int = 35_000) -> Page:
        """Load a page in Chromium. Used only when plain HTTP is blocked or JS-only."""
        if not settings.pw_browser_enabled:
            raise Blocked("browser fallback disabled (PW_BROWSER_ENABLED=false)")
        host = urlsplit(url).netloc
        async with self._render_slots:
            for attempt in range(2):
                browser = await self._ensure_browser()
                context, page, ephemeral = await self._context(browser, host)
                try:
                    await self._throttle.wait(host)
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    if wait_for and response and response.status < 400:
                        try:
                            await page.wait_for_selector(wait_for, timeout=8_000)
                        except Exception:
                            pass
                    await page.wait_for_timeout(1_200)
                    result = Page(page.url, response.status if response else 0,
                                  await page.content(), "browser")
                    await self._save_context(context, host)
                    return result
                except Exception:
                    if attempt or browser.is_connected():
                        raise
                    # A browser lost across suspend/crash gets one fresh launch.
                finally:
                    try:
                        await page.close()
                        if ephemeral:
                            await context.close()
                    except Exception:
                        pass

    @staticmethod
    def _usable(page: Page) -> bool:
        return 200 <= page.status < 400 and not page.looks_blocked and len(page.text) > 2_000

    async def get_or_render(self, url: str, *, wait_for: str | None = None,
                            browser_first: bool = False) -> Page:
        """Fetch by the cheapest rung known to work for this host, escalating only
        when needed, and remember the outcome so the next lookup starts there.

        ``browser_first`` is for JS-rendered stores whose plain HTML carries a
        stale or partial price: skip straight to the browser when enabled.
        """
        host = urlsplit(url).netloc.removeprefix("www.")
        known = self.playbook.strategy(host)

        # Hosts that even Chromium could not crack: try the fast path once (walls
        # do get lifted) but do not pay for a browser we expect to fail.
        if known == BLOCKED_STRAT:
            try:
                page = await self.get(url)
                if page.status in (404, 410):
                    return page
                if self._usable(page):
                    self.playbook.record(host, HTTP)
                    return page
            except Exception as exc:                       # noqa: BLE001
                log.debug("http fetch failed for %s: %s", url, exc)
            raise Blocked(f"{host} recently returned a challenge; browser retry after the "
                          f"{settings.pw_blocked_retry_seconds:g}s cooldown. "
                          "An approved store API or PW_HTTP_PROXY may help.")

        # Try the fast HTTP path unless we already know this host needs a browser
        # (or the caller asked to prefer one and we have not proven HTTP works).
        skip_http = settings.pw_browser_enabled and (known == BROWSER or browser_first)
        if not skip_http:
            try:
                page = await self.get(url)
                if page.status in (404, 410):
                    return page
                if self._usable(page):
                    self.playbook.record(host, HTTP)
                    return page
            except Exception as exc:                       # noqa: BLE001
                log.debug("http fetch failed for %s: %s", url, exc)

        page = await self.render(url, wait_for=wait_for)
        if self._usable(page):
            self.playbook.record(host, BROWSER)
        elif page.status not in (404, 410, 429) and page.status < 500 and page.looks_blocked:
            self.playbook.record(host, BLOCKED_STRAT)
        return page

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._cffi is not None:
            try:
                await self._cffi.close()
            except Exception:                              # noqa: BLE001
                pass
            self._cffi = None
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
            self._contexts.clear()
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None


fetcher = Fetcher()
