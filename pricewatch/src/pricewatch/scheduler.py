"""Periodic price sweeps."""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import service
from .settings import settings

log = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="UTC")


async def sweep() -> None:
    try:
        summary = await service.refresh_offers()
        log.info("sweep: checked=%s updated=%s failed=%s alerts=%s",
                 summary["checked"], summary["updated"], summary["failed"],
                 len(summary["alerts"]))
    except Exception:
        log.exception("scheduled sweep failed")


def start() -> None:
    trigger = CronTrigger.from_crontab(settings.pw_check_cron, timezone="UTC")
    scheduler.add_job(sweep, trigger, id="price_sweep", replace_existing=True,
                      max_instances=1, coalesce=True, misfire_grace_time=600)
    scheduler.start()
    log.info("scheduler started (cron: %s)", settings.pw_check_cron)


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
