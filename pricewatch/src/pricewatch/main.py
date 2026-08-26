"""ASGI entrypoint: REST API + MCP server + scheduled price sweeps in one process."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import scheduler
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
    async with mcp.session_manager.run():
        scheduler.start()
        log.info("pricewatch ready — REST on /api, MCP on /mcp")
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
