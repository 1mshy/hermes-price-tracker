"""Channel dispatch: send an alert to every configured channel, independently.

Transport failures and temporary provider failures are retried with bounded
backoff. Permanent errors (bad credentials, payloads) fail immediately.
"""
from __future__ import annotations

import asyncio
import logging
from time import monotonic

import httpx

from .base import Alert, Channel, DeliveryError
from .discord import DiscordChannel
from .ntfy import NtfyChannel
from .signal import SignalChannel
from .whatsapp import CallMeBotWhatsAppChannel, TwilioWhatsAppChannel

log = logging.getLogger(__name__)

CHANNELS: list[Channel] = [
    DiscordChannel(),
    NtfyChannel(),
    SignalChannel(),
    TwilioWhatsAppChannel(),
    CallMeBotWhatsAppChannel(),
]


#: Seconds to wait before each retry of a transport-level failure. Channels
#: run in parallel. These delays are additional to each request's timeout.
RETRY_DELAYS: tuple[float, ...] = (2, 5, 15)
MAX_RETRY_DELAY = 30
_deferred_until: dict[str, float] = {}


def transient(exc: BaseException) -> bool:
    """Network outage, rate limit, or a temporary provider failure."""
    return (isinstance(exc, (httpx.TransportError, OSError, asyncio.TimeoutError))
            or isinstance(exc, DeliveryError) and exc.status in (408, 425, 429, 500, 502, 503, 504))


def configured_channels() -> list[Channel]:
    return [c for c in CHANNELS if c.configured]


def channel_status() -> dict[str, bool]:
    return {c.name: c.configured for c in CHANNELS}


async def dispatch(alert: Alert, only: list[str] | None = None) -> dict[str, str]:
    """Returns {channel: "sent" | "error: …"}. One bad channel never blocks the rest."""
    wanted = [c for c in configured_channels() if not only or c.name in only]
    if not wanted:
        return {}

    async def one(channel: Channel) -> tuple[str, str]:
        remaining = _deferred_until.get(channel.name, 0) - monotonic()
        if remaining > 0:
            return channel.name, f"error: provider retry deferred for {remaining:.0f}s"
        attempts = len(RETRY_DELAYS) + 1
        for attempt in range(1, attempts + 1):
            try:
                await channel.send(alert)
            except Exception as exc:
                if attempt < attempts and transient(exc):
                    delay = RETRY_DELAYS[attempt - 1]
                    if isinstance(exc, DeliveryError):
                        delay = max(delay, exc.retry_after)
                    # Never retry earlier than Retry-After. Long provider
                    # outages are left to the next sweep with the watch armed.
                    if delay > MAX_RETRY_DELAY:
                        _deferred_until[channel.name] = monotonic() + delay
                        return channel.name, f"error: {exc}; retry deferred"
                    log.warning("notification via %s failed (%s); retry %d/%d in %ss",
                                channel.name, exc, attempt, attempts - 1, delay)
                    await asyncio.sleep(delay)
                    continue
                log.warning("notification via %s failed: %s", channel.name, exc)
                return channel.name, f"error: {exc}"
            if attempt > 1:
                log.info("notification via %s delivered on attempt %d", channel.name, attempt)
            _deferred_until.pop(channel.name, None)
            return channel.name, "sent"
        return channel.name, "error: unreachable"      # pragma: no cover - loop always returns

    return dict(await asyncio.gather(*(one(c) for c in wanted)))


__all__ = ["Alert", "dispatch", "configured_channels", "channel_status", "CHANNELS",
           "RETRY_DELAYS", "transient"]
