"""HTTP + headless-browser fetching, with per-host politeness.

Fetch strategy, cheapest first:

  1. HTTP with a real-browser TLS/HTTP2 fingerprint (curl_cffi impersonation).
     This is the single biggest win: retail bot walls (Cloudflare "Just a
     moment", Akamai, Amazon) fingerprint the TLS ClientHello and HTTP/2
     settings, not the header set — so plain httpx gets challenged even with
     perfect headers, while an impersonated Chrome fingerprint sails through in
     ~1s. Falls back to plain httpx if curl_cffi is unavailable.
  2. Headless Chromium — only for genuinely JS-rendered pages, or the handful of
     sites that fingerprint deeper than TLS.
  3. Nothing else helps: the remaining walls (Micro Center, Adorama, Mouser) key
     on IP reputation and need a residential proxy (PW_HTTP_PROXY) or an API key.

A per-host "playbook" remembers which rung actually returned a real page for
each domain, so repeat lookups jump straight there instead of re-probing —
which is what makes the agent stop rediscovering how to reach a site every time.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

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


class _Playbook:
    """Remembers the cheapest fetch rung that worked per host, persisted to disk.

    All IO is best-effort: a read-only or missing data dir must never break
    fetching, so every failure degrades to an in-memory-only cache.
    """

    def __init__(self) -> None:
        self._path = self._resolve_path()
        self._data: dict[str, str] = {}
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
                self._data = {str(k): str(v) for k, v in raw.items()}
        except Exception as exc:            # noqa: BLE001
            log.debug("could not load fetch playbook: %s", exc)

    def strategy(self, host: str) -> str | None:
        return self._data.get(host.removeprefix("www."))

    def record(self, host: str, strategy: str) -> None:
        host = host.removeprefix("www.")
        if self._data.get(host) == strategy:
            return
        self._data[host] = strategy
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
        return dict(self._data)


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
        head = self.text[:200_000].lower()
        return any(marker in head for marker in CHALLENGE_MARKERS)


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
                log.debug("cffi fetch failed for %s: %s", url, exc)
            try:
                client = await self.client()
                response = await client.get(url, headers=headers or None)
                return Page(str(response.url), response.status_code, response.text, "http")
            except Exception as exc:                       # noqa: BLE001 — network is messy
                last = exc
                if attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"GET {url} failed: {last}")

    async def get_json(self, url: str, *, headers: dict | None = None):
        page = await self.get(url, headers={"Accept": "application/json", **(headers or {})})
        if page.status != 200:
            raise RuntimeError(f"HTTP {page.status} for {url}")
        import json
        return json.loads(page.text)

    # ── headless browser ────────────────────────────────────────────────
    async def _ensure_browser(self):
        if self._browser is not None:
            return self._browser
        async with self._browser_lock:
            if self._browser is None:
                from playwright.async_api import async_playwright
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                )
        return self._browser

    async def render(self, url: str, *, wait_for: str | None = None, timeout_ms: int = 35_000) -> Page:
        """Load a page in Chromium. Used only when plain HTTP is blocked or JS-only."""
        if not settings.pw_browser_enabled:
            raise Blocked("browser fallback disabled (PW_BROWSER_ENABLED=false)")
        browser = await self._ensure_browser()
        context = await browser.new_context(
            user_agent=settings.pw_user_agent,
            locale="en-US",
            timezone_id="America/New_York",
            viewport={"width": 1440, "height": 900},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        # Strip the most obvious automation tell before any page script runs.
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "window.chrome={runtime:{}};"
        )
        try:
            page = await context.new_page()
            await self._throttle.wait(urlsplit(url).netloc)
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            if wait_for:
                try:
                    await page.wait_for_selector(wait_for, timeout=8_000)
                except Exception:
                    pass
            await page.wait_for_timeout(1_200)   # let client-side price widgets settle
            html = await page.content()
            status = response.status if response else 0
            return Page(page.url, status, html, "browser")
        finally:
            await context.close()

    @staticmethod
    def _usable(page: Page) -> bool:
        return not page.looks_blocked and len(page.text) > 2_000

    async def get_or_render(self, url: str, *, wait_for: str | None = None,
                            browser_first: bool = False) -> Page:
        """Fetch by the cheapest rung known to work for this host, escalating only
        when needed, and remember the outcome so the next lookup starts there.

        ``browser_first`` is for JS-rendered stores whose plain HTML carries a
        stale or partial price: skip straight to the browser unless the playbook
        has already proven the fast HTTP path works for this host.
        """
        host = urlsplit(url).netloc.removeprefix("www.")
        known = self.playbook.strategy(host)

        # Hosts that even Chromium could not crack: try the fast path once (walls
        # do get lifted) but do not pay for a browser we expect to fail.
        if known == BLOCKED_STRAT:
            try:
                page = await self.get(url)
                if self._usable(page):
                    self.playbook.record(host, HTTP)
                    return page
            except Exception as exc:                       # noqa: BLE001
                log.debug("http fetch failed for %s: %s", url, exc)
            raise Blocked(f"{host} is known to block automated fetches — "
                          "set PW_HTTP_PROXY (residential) or a store API key")

        # Try the fast HTTP path unless we already know this host needs a browser
        # (or the caller asked to prefer one and we have not proven HTTP works).
        skip_http = known == BROWSER or (browser_first and known != HTTP)
        if not skip_http:
            try:
                page = await self.get(url)
                if self._usable(page):
                    self.playbook.record(host, HTTP)
                    return page
            except Exception as exc:                       # noqa: BLE001
                log.debug("http fetch failed for %s: %s", url, exc)

        try:
            page = await self.render(url, wait_for=wait_for)
        except Blocked:
            self.playbook.record(host, BLOCKED_STRAT)
            raise
        self.playbook.record(host, BROWSER if self._usable(page) else BLOCKED_STRAT)
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
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None


fetcher = Fetcher()
