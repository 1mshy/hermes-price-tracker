"""HTTP + headless-browser fetching, with per-host politeness."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .settings import settings

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
    "robot or human", "verify you are a human",
)


class Blocked(Exception):
    """Site served a bot challenge rather than the product page."""


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
        self._playwright = None
        self._browser = None
        self._browser_lock = asyncio.Lock()

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

    async def get(self, url: str, *, headers: dict | None = None, retries: int = 2) -> Page:
        host = urlsplit(url).netloc
        client = await self.client()
        last: Exception | None = None
        for attempt in range(retries + 1):
            await self._throttle.wait(host)
            try:
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

    async def get_or_render(self, url: str, *, wait_for: str | None = None) -> Page:
        """Plain HTTP first; escalate to Chromium only if that looks blocked."""
        try:
            page = await self.get(url)
            if not page.looks_blocked and len(page.text) > 2_000:
                return page
        except Exception as exc:
            log.debug("http fetch failed for %s: %s", url, exc)
        return await self.render(url, wait_for=wait_for)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None


fetcher = Fetcher()
