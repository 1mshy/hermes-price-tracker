"""Channel dispatch: send an alert to every configured channel, independently."""
from __future__ import annotations

import asyncio
import logging

from .base import Alert, Channel
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
        try:
            await channel.send(alert)
            return channel.name, "sent"
        except Exception as exc:
            log.warning("notification via %s failed: %s", channel.name, exc)
            return channel.name, f"error: {exc}"

    return dict(await asyncio.gather(*(one(c) for c in wanted)))


__all__ = ["Alert", "dispatch", "configured_channels", "channel_status", "CHANNELS"]
