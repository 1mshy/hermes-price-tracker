"""The contract every scout in the swarm implements.

A scout owns exactly one site: build that site's URL for the query, read
whatever structured data it publishes, and hand back `CarListing`s. It does not
filter, rank, deduplicate or convert — the swarm does all of that once, for
everyone, so that a bug in one adapter cannot quietly change the shape of the
final report.

The other half of the contract is failure. Car sites break, wall, and rate
limit constantly, and a swarm that drops a broken source without saying so
produces a confident report about a market it only half saw. `run` therefore
converts every exception into a status, and the status always reaches the user.
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..listing import BLOCKED, EMPTY, ERROR, OK, SourceOutcome
from ..spec import CarQuery

log = logging.getLogger(__name__)


class CarSource:
    """One site the swarm can send a scout to."""

    key: str = "generic"
    label: str = "Generic"
    #: how you would buy from it — decides whether its prices are comparable
    #: with a dealer's asking price at all
    channel: str = "retail"
    #: countries it covers; the swarm skips sources that cannot serve the query
    countries: tuple[str, ...] = ("CA",)
    #: set when the site needs credentials we may not have
    requires: str | None = None

    async def search(self, query: CarQuery) -> list:   # pragma: no cover - interface
        raise NotImplementedError

    def supports(self, query: CarQuery) -> bool:
        country = (query.place.country or "CA").upper()
        return country in self.countries


async def run(source: CarSource, query: CarQuery, *, timeout: float = 45.0) -> SourceOutcome:
    """Dispatch one scout, and never let it take the swarm down with it."""
    started = time.monotonic()
    try:
        listings = await asyncio.wait_for(source.search(query), timeout=timeout)
    except asyncio.TimeoutError:
        return SourceOutcome(source=source.key, status=ERROR,
                             note=f"timed out after {timeout:.0f}s",
                             elapsed_s=time.monotonic() - started)
    except Exception as exc:                           # noqa: BLE001 — a scout must not kill the swarm
        note = f"{type(exc).__name__}: {exc}"[:300]
        status = BLOCKED if _looks_walled(exc) else ERROR
        log.warning("car source %s failed: %s", source.key, note)
        return SourceOutcome(source=source.key, status=status, note=note,
                             elapsed_s=time.monotonic() - started)

    elapsed = time.monotonic() - started
    if not listings:
        return SourceOutcome(source=source.key, status=EMPTY, elapsed_s=elapsed,
                             note="no listings matched at the source")
    return SourceOutcome(source=source.key, status=OK, listings=listings,
                         elapsed_s=elapsed)


def _looks_walled(exc: Exception) -> bool:
    from ...fetch import Blocked
    if isinstance(exc, Blocked):
        return True
    text = str(exc).lower()
    return any(word in text for word in ("blocked", "captcha", "403", "forbidden", "challenge"))
