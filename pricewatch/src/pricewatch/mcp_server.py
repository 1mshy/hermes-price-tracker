"""MCP tools exposed to the Hermes agent.

Tool descriptions are the agent's only documentation, so they state plainly what
each tool needs and what it returns.
"""
from __future__ import annotations

import logging

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from . import community, preferences, service
from . import scheduler as sweep_scheduler
from .notify import Alert, channel_status, dispatch
from .stores.registry import catalog_summary, fetch_offer
from .settings import settings

log = logging.getLogger(__name__)

_BASE_INSTRUCTIONS = (
    "Price research and tracking across major tech and 3D-printing retailers. "
    "Use compare_prices for a one-off 'what does this cost' question. Use "
    "track_product_url or track_product_description to set up an ongoing watch "
    "that notifies the user on Discord/Signal/WhatsApp when a price target is hit. "
    "Prices are read from official store APIs and structured product data, so "
    "always report the store and URL alongside any figure you quote."
)


def _instructions() -> str:
    """The server blurb, extended with the user's currency when they set one.

    This is the one piece of guidance the agent sees on every connection, so
    the currency rule belongs here rather than only in a skill that may not
    have loaded.
    """
    currency = preferences.preferred_currency()
    if not currency:
        return _BASE_INSTRUCTIONS
    region = preferences.preferred_region()
    return _BASE_INSTRUCTIONS + (
        f" This user shops in {currency}"
        + (f" ({region})" if region else "")
        + f". Answer in {currency}: lead with the stores that bill in {currency} and "
        f"link their regional storefront. Never convert a price yourself and never "
        f"present a foreign figure as {currency} — quote each store's real price in "
        f"its own currency and name that currency. Where a result carries an "
        f"`approx_in_preferred` field it is an indicative rate for comparison only, "
        f"so show it as an approximation (≈) beside the real price, never instead of "
        f"it. Call set_preferred_currency if the user asks to be quoted differently."
    )


mcp = MCPServer("hermes-shopping", instructions=_instructions())


@mcp.tool(
    description=(
        "Look up the current price of one specific product page. Give the full product URL. "
        "Works for Amazon, Best Buy, Walmart, Newegg, Micro Center, B&H, and 3D-printing "
        "stores (Bambu Lab, Prusa, Elegoo, Creality, Anycubic, Polymaker, E3D, MatterHackers "
        "and many more). Returns price, currency, stock status and how the price was read."
    )
)
async def get_price(url: str) -> dict:
    result = await fetch_offer(url)
    return result.as_dict()


@mcp.tool(
    description=(
        "Search many stores at once for a product and return the current prices, cheapest "
        "first. Pass a specific, detailed description or exact product title (e.g. "
        "'Bambu Lab P1S Combo with AMS' or 'Polymaker PolyTerra PLA 1kg matte black'). "
        "Optionally restrict to specific stores by their keys (see list_stores). "
        "This does NOT set up tracking — use the track_* tools for that."
    )
)
async def compare_prices(query: str, stores: list[str] | None = None,
                         limit_per_store: int = 3) -> dict:
    return await service.compare(query, stores=stores, limit_per_store=limit_per_store)


@mcp.tool(
    description=(
        "Start tracking the price of a specific product URL and alert the user when it drops. "
        "Set target_price for an absolute threshold in dollars (alert when price <= target), "
        "and/or drop_pct for a relative one (alert when it falls that many percent below "
        "today's price). At least one of the two should be given. channels is an optional "
        "comma-separated subset of 'discord,signal,whatsapp' — leave empty to use every "
        "channel the user has configured."
    )
)
async def track_product_url(url: str, target_price: float | None = None,
                            drop_pct: float | None = None, label: str = "",
                            channels: str = "", cooldown_hours: int = 12) -> dict:
    return await service.track_url(url, target_price=target_price, drop_pct=drop_pct,
                                   label=label, channels=channels,
                                   cooldown_hours=cooldown_hours)


@mcp.tool(
    description=(
        "Start tracking a product described in words rather than by URL, across every store "
        "that stocks it. Give the most exhaustive description you can — brand, model and "
        "variant — because matching across stores depends on it. Creates one watch covering "
        "all matched store listings and alerts on the cheapest one. Same target_price / "
        "drop_pct semantics as track_product_url."
    )
)
async def track_product_description(description: str, target_price: float | None = None,
                                    drop_pct: float | None = None,
                                    stores: list[str] | None = None,
                                    channels: str = "", cooldown_hours: int = 12) -> dict:
    return await service.track_query(description, stores=stores, target_price=target_price,
                                     drop_pct=drop_pct, channels=channels,
                                     cooldown_hours=cooldown_hours)


@mcp.tool(
    description=(
        "List every active price watch with its target, its current cheapest offer, and every "
        "store listing being monitored. Use this before modifying or deleting a tracker so you "
        "can quote the right tracker_id."
    )
)
async def list_trackers() -> dict:
    return {"trackers": await service.list_tracked()}


@mcp.tool(
    description=(
        "Change an existing watch: adjust target_price, drop_pct, notification channels, "
        "cooldown_hours, or pause it with active=false. Set reset_baseline=true to make the "
        "current price the new reference point for percentage drops."
    )
)
async def update_tracker(tracker_id: int, target_price: float | None = None,
                         drop_pct: float | None = None, channels: str | None = None,
                         cooldown_hours: int | None = None, active: bool | None = None,
                         label: str | None = None, reset_baseline: bool = False) -> dict:
    return await service.set_tracker(
        tracker_id, target_price=target_price, drop_pct=drop_pct, channels=channels,
        cooldown_hours=cooldown_hours, active=active, label=label,
        reset_baseline=reset_baseline)


@mcp.tool(description="Stop and delete a price watch permanently, by tracker_id.")
async def delete_tracker(tracker_id: int) -> dict:
    return await service.delete_tracker(tracker_id)


@mcp.tool(
    description=(
        "Attach one more store listing to a product already being tracked, by product_id and "
        "the new store's product URL. Useful when the user finds the item somewhere the "
        "automatic search missed."
    )
)
async def add_store_listing(product_id: int, url: str) -> dict:
    return await service.add_offer(product_id, url)


@mcp.tool(
    description=(
        "Search the catalog for additional stores selling a product that is already tracked, "
        "and add any confident matches to the watch. Widens price coverage for an existing "
        "tracker_id's product_id."
    )
)
async def broaden_coverage(product_id: int, max_new: int = 6) -> dict:
    return await service.find_more_stores(product_id, max_new=max_new)


@mcp.tool(
    description=(
        "Show recorded price history for a tracked product, newest first, plus a summary "
        "with the current best price, lowest/highest/average seen, and a plain verdict "
        "('at or near the lowest recorded price' / 'above the recorded average'). Use it "
        "to answer 'is this actually a good deal' with the verdict, not a number dump."
    )
)
async def get_price_history(product_id: int, limit: int = 100) -> dict:
    return await service.price_history(product_id, limit=limit)


@mcp.tool(
    description=(
        "Force an immediate re-check of every tracked listing right now instead of waiting for "
        "the next scheduled sweep, then evaluate all alert rules. Returns how many listings "
        "were refreshed and any alerts that fired."
    )
)
async def refresh_prices_now() -> dict:
    return await service.refresh_offers()


@mcp.tool(
    description=(
        "List every store the system can read prices from, with its key (for the `stores` "
        "argument elsewhere), the retail category it covers, and the extraction method used."
    )
)
async def list_stores() -> dict:
    return {"stores": catalog_summary()}


@mcp.tool(
    description=(
        "Report which notification channels are configured (Discord, ntfy push, Signal, "
        "WhatsApp) and optionally send a test message to confirm delivery end to end. Use "
        "this when the user asks whether alerts will actually reach them. If none are "
        "configured, suggest ntfy: pick an unguessable topic, subscribe in the ntfy app, "
        "set NTFY_TOPIC in .env — no account needed."
    )
)
async def check_notifications(send_test: bool = False) -> dict:
    status = channel_status()
    if not send_test:
        return {"channels": status, "configured": [k for k, v in status.items() if v]}
    outcome = await dispatch(Alert(
        title="✅ Hermes Shopping test alert",
        body="If you can read this, price alerts will reach you on this channel.",
    ))
    return {"channels": status, "test_result": outcome or "no channel configured"}


@mcp.tool(
    description=(
        "Report which currency the user wants to be quoted in and which country's storefronts "
        "the engine reaches for. Call it when you are unsure what currency to answer in, or "
        "before quoting a comparison that spans several currencies."
    )
)
async def get_preferred_currency() -> dict:
    return preferences.current()


@mcp.tool(
    description=(
        "Set the currency the user wants every price answered in (ISO code, e.g. 'CAD'), and "
        "optionally the country whose storefronts to prefer (ISO code, e.g. 'CA' — defaults to "
        "the currency's home market). The engine then searches regional storefronts that bill "
        "in that currency, and annotates any foreign-currency result with an indicative "
        "conversion. It never rewrites a store's actual price. Pass 'default' to clear the "
        "preference. Takes effect immediately and survives restarts."
    )
)
async def set_preferred_currency(currency: str, region: str = "") -> dict:
    try:
        return {"ok": True, **preferences.set_preference(currency, region)}
    except preferences.LocaleError as exc:
        return {"ok": False, "error": str(exc), **preferences.current()}


@mcp.tool(
    description=(
        "Report how the price engine is configured: scan schedule, browser fallback, and which "
        "optional retailer API keys are present. Use it to explain why a particular store might "
        "not be returning prices."
    )
)
async def engine_status() -> dict:
    from .fetch import _CffiSession, fetcher
    schedule = sweep_scheduler.current()
    return {
        "sweep_schedule": schedule,
        "check_schedule_cron": schedule["cron"],
        "locale": preferences.current(),
        "http_fingerprint": (settings.pw_impersonate if _CffiSession is not None else "unavailable"),
        "browser_fallback_enabled": settings.pw_browser_enabled,
        "residential_proxy_configured": bool(settings.pw_http_proxy),
        "per_host_requests_per_second": settings.pw_per_host_rps,
        "optional_api_keys": {
            "bestbuy": bool(settings.bestbuy_api_key),
            "ebay": bool(settings.ebay_app_id and settings.ebay_cert_id),
            "keepa_amazon": bool(settings.keepa_api_key),
        },
        # What the engine has learned about how to reach each host, so repeat
        # lookups skip the probe: host -> "http" | "browser" | "blocked".
        "host_fetch_playbook": fetcher.playbook.as_dict(),
        "notification_channels": channel_status(),
    }


@mcp.tool(
    description=(
        "Search recent Reddit posts for community chatter about a product — deal threads, "
        "coupon codes, and price claims ('people are getting these for $50'). Searches the "
        "3D-printing and deal subreddits by default (3Dprinting, BambuLab, 3dbargains, "
        "buildapcsales); pass subreddits to override. Returns recent posts newest first, "
        "each with any prices mentioned in the text. Community claims are unverified "
        "leads — confirm with get_price or compare_prices before quoting a price from here."
    )
)
async def community_pulse(query: str, subreddits: list[str] | None = None,
                          days: int = 14, limit: int = 20) -> dict:
    return await community.search(query, subreddits=subreddits, days=days, limit=limit)


@mcp.tool(
    description=(
        "Read one Reddit thread — the post plus its top comments — by its reddit.com URL. "
        "Works via RSS, which Reddit serves even where its JSON API is blocked, so do not "
        "hand-fetch reddit.com with curl. Use it to check what a deal thread actually says "
        "(coupon terms, region, expiry) before relaying a community price claim."
    )
)
async def read_reddit_thread(url: str, max_comments: int = 15) -> dict:
    return await community.thread(url, max_comments=max_comments)


@mcp.tool(
    description=(
        "Change how often every tracked price is re-checked (the sweep schedule). Takes a "
        "standard 5-field cron expression evaluated in UTC — '*/15 * * * *' is every 15 "
        "minutes, '0 9 * * *' is daily at 09:00 UTC. The engine refuses schedules more "
        "frequent than every 5 minutes to stay polite to retailers. Pass 'default' to "
        "revert to the configured default. Takes effect immediately, survives restarts, "
        "and returns the active schedule with the next sweep time."
    )
)
async def set_sweep_schedule(cron: str) -> dict:
    try:
        return {"ok": True, **sweep_scheduler.reschedule(cron)}
    except sweep_scheduler.ScheduleError as exc:
        return {"ok": False, "error": str(exc), **sweep_scheduler.current()}


@mcp.tool(
    description=(
        "Show the most recent fired price alerts, newest first: which tracker fired, at "
        "what price, why, and whether any notification channel actually delivered it. Use "
        "this to answer 'has anything triggered?' — especially when no notification "
        "channels are configured, which makes fired alerts otherwise invisible."
    )
)
async def list_alert_events(limit: int = 30) -> dict:
    return await service.list_alerts(limit=limit)


def build_mcp_app():
    """Create the Streamable-HTTP ASGI app (also initialises the session manager).

    The SDK defaults to accepting only 127.0.0.1, which rejects the agent
    container with 421 Misdirected Request. Keep rebinding protection on, but
    allow the hostnames this service is actually reachable at.
    """
    hosts = [h.strip() for h in settings.pw_mcp_allowed_hosts.split(",") if h.strip()]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"http://{h}" for h in hosts] + [f"https://{h}" for h in hosts],
    )
    return mcp.streamable_http_app(transport_security=security)
