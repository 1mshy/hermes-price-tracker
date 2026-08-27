"""Fallback adapter: any storefront that publishes schema.org data.

Handles the long tail (Prusa, MatterHackers, Sunlu, most WooCommerce/Magento/
BigCommerce shops) and escalates to Chromium when plain HTTP is challenged.
"""
from __future__ import annotations

from ..extract import extract
from ..fetch import Blocked, fetcher
from ..settings import settings
from .base import StoreAdapter, StoreResult, host_of


class StructuredAdapter(StoreAdapter):
    name = "structured"

    def __init__(self, name: str | None = None, domains: tuple[str, ...] = (),
                 wait_for: str | None = None, force_browser: bool = False):
        if name:
            self.name = name
        self.domains = domains
        self.wait_for = wait_for
        self.force_browser = force_browser

    def matches(self, url: str) -> bool:
        return super().matches(url) if self.domains else False

    async def fetch_offer(self, url: str) -> StoreResult:
        store = self.name if self.domains else host_of(url)
        try:
            # get_or_render tries the fast fingerprinted HTTP path first, escalates
            # to a browser only when a host needs it, and records the outcome so a
            # proxy-walled store fails fast next time instead of re-spinning Chromium.
            page = await fetcher.get_or_render(
                url, wait_for=self.wait_for, browser_first=self.force_browser)
        except Blocked as exc:
            return StoreResult(store=store, url=url, error=str(exc), method="blocked")
        except Exception as exc:
            return StoreResult(store=store, url=url, error=f"fetch failed: {exc}")

        found = extract(page.text, default_currency=settings.pw_currency)
        if not found.ok:
            # One escalation: the price may only exist after JS runs.
            if page.method == "http" and settings.pw_browser_enabled:
                try:
                    page = await fetcher.render(url, wait_for=self.wait_for)
                    found = extract(page.text, default_currency=settings.pw_currency)
                except Exception:
                    pass
        if not found.ok:
            reason = "bot challenge" if page.looks_blocked else "no price in page"
            return StoreResult(store=store, url=page.url, error=reason,
                               method=f"{page.method}:failed")
        return StoreResult(
            store=store,
            url=page.url,
            title=found.title,
            price=found.price,
            currency=found.currency or settings.pw_currency,
            in_stock=found.in_stock,
            sku=found.sku,
            method=f"{page.method}:{found.method}",
        )
