"""MCP tools exposed to the Hermes agent.

Tool descriptions are the agent's only documentation, so they state plainly what
each tool needs and what it returns.
"""
from __future__ import annotations

import logging

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from . import service
from .notify import Alert, channel_status, dispatch
from .stores.registry import catalog_summary, fetch_offer
from .settings import settings

log = logging.getLogger(__name__)

mcp = MCPServer(
    "hermes-shopping",
    instructions=(
        "Price research and tracking across major tech and 3D-printing retailers. "
        "Use compare_prices for a one-off 'what does this cost' question. Use "
        "track_product_url or track_product_description to set up an ongoing watch "
        "that notifies the user on Discord/Signal/WhatsApp when a price target is hit. "
        "Prices are read from official store APIs and structured product data, so "
        "always report the store and URL alongside any figure you quote."
    ),
)


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
        "Show recorded price history for a tracked product, newest first. Use it to answer "
        "'is this actually a good deal' — compare the current price against the recent range."
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
        "Report which notification channels are configured (Discord, Signal, WhatsApp) and "
        "optionally send a test message to confirm delivery end to end. Use this when the user "
        "asks whether alerts will actually reach them."
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
        "Report how the price engine is configured: scan schedule, browser fallback, and which "
        "optional retailer API keys are present. Use it to explain why a particular store might "
        "not be returning prices."
    )
)
async def engine_status() -> dict:
    return {
        "check_schedule_cron": settings.pw_check_cron,
        "browser_fallback_enabled": settings.pw_browser_enabled,
        "per_host_requests_per_second": settings.pw_per_host_rps,
        "optional_api_keys": {
            "bestbuy": bool(settings.bestbuy_api_key),
            "ebay": bool(settings.ebay_app_id and settings.ebay_cert_id),
            "keepa_amazon": bool(settings.keepa_api_key),
        },
        "notification_channels": channel_status(),
    }


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
