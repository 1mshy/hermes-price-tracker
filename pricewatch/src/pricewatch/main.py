"""ASGI entrypoint: REST API + MCP server + scheduled price sweeps in one process."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import preferences, scheduler, service
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
    fixed = await service.backfill_tracker_currency()
    if fixed:
        log.info("gave %d pre-existing watch(es) the currency of their baseline listing", fixed)
    async with mcp.session_manager.run():
        scheduler.start()
        log.info("pricewatch ready — REST on /api, MCP on /mcp (currency: %s)",
                 preferences.preferred_currency() or "as each store bills")
        try:
            yield
        finally:
            scheduler.stop()
            await fetcher.aclose()


app = FastAPI(
    title="Hermes Shopping — price engine",
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(router, prefix="/api")
# Serves the Streamable-HTTP MCP endpoint at /mcp
app.mount("/", mcp_app)
