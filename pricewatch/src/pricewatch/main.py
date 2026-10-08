"""ASGI entrypoint: REST API + MCP server + scheduled price sweeps in one process."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import fx, preferences, scheduler, service
from .api import router
from .db import init_db
from .fetch import fetcher
from .mcp_server import build_mcp_app, mcp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("pricewatch")

# Build the MCP ASGI app first — this is what creates the session manager.
mcp_app = build_mcp_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # The currency preference was first read while building the MCP blurb above,
    # possibly before the settings table existed. Re-read it now the schema is
    # there, so a saved override is live from the first request.
    preferences.reload()
    fx.load()
    fixed = await service.backfill_tracker_currency()
    if fixed:
        log.info("gave %d pre-existing watch(es) the currency of their baseline listing", fixed)
    async with mcp.session_manager.run():
        scheduler.start()
        # A fresh container (or one that slept past a refresh) should not wait
        # for the next cron tick to stop ranking on the static table — but it
        # must not hold up startup for a network call either.
        warmup = asyncio.create_task(fx.refresh()) if fx.status()["stale"] else None
        log.info("pricewatch ready — REST on /api, MCP on /mcp (currency: %s)",
                 preferences.preferred_currency() or "as each store bills")
        try:
            yield
        finally:
            if warmup is not None and not warmup.done():
                warmup.cancel()
                await asyncio.gather(warmup, return_exceptions=True)
            scheduler.stop()
            await service.stop_full_sweep()
            await fetcher.aclose()


app = FastAPI(
    title="Hermes Shopping — price engine",
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(router, prefix="/api")
static_dir = Path(__file__).with_name("static")
app.mount("/assets", StaticFiles(directory=static_dir), name="assets")


@app.get("/", include_in_schema=False)
@app.get("/watchlist", include_in_schema=False)
async def watchlist():
    return FileResponse(static_dir / "watchlist.html", headers={"Cache-Control": "no-cache"})


# Serves the Streamable-HTTP MCP endpoint at /mcp
app.mount("/", mcp_app)
