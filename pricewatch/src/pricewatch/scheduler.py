"""Periodic price sweeps — reschedulable at runtime, persisted across restarts.

The sweep cadence starts from PW_CHECK_CRON but can be changed live (by the
agent via the set_sweep_schedule MCP tool, or PATCH /api/schedule). A changed
schedule is stored in the settings table so a container restart keeps it.
"""
from __future__ import annotations

import datetime as dt
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import service
from .settings import settings

log = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="UTC")

_JOB_ID = "price_sweep"
_SETTING_KEY = "sweep_cron"

#: Politeness floor — sweeping 38 retailers more often than this is a good way
#: to get every adapter blocked (see README "Notes and caveats").
MIN_SWEEP_INTERVAL_SECONDS = 300

_active_cron: str = settings.pw_check_cron
_cron_source: str = "env"


class ScheduleError(ValueError):
    """The requested cron expression is invalid or impolitely frequent."""


async def sweep() -> None:
    try:
        summary = await service.refresh_offers()
        log.info("sweep: checked=%s updated=%s failed=%s alerts=%s",
                 summary["checked"], summary["updated"], summary["failed"],
                 len(summary["alerts"]))
    except Exception:
        log.exception("scheduled sweep failed")


def validated_trigger(cron: str) -> CronTrigger:
    """Parse a 5-field cron expression, rejecting overly aggressive cadences."""
    try:
        trigger = CronTrigger.from_crontab(cron.strip(), timezone="UTC")
    except ValueError as exc:
        raise ScheduleError(
            f"invalid cron expression {cron!r}: {exc} "
            "(expected 5 fields, e.g. '*/30 * * * *' for every 30 minutes)"
        ) from exc

    # Sample successive fire times to catch e.g. "* * * * *".
    previous: dt.datetime | None = None
    probe = dt.datetime.now(dt.timezone.utc)
    for _ in range(4):
        upcoming = trigger.get_next_fire_time(previous, probe)
        if upcoming is None:
            break
        if previous is not None:
            gap = (upcoming - previous).total_seconds()
            if gap < MIN_SWEEP_INTERVAL_SECONDS:
                raise ScheduleError(
                    f"{cron!r} would sweep every {int(gap)}s — the floor is "
                    f"{MIN_SWEEP_INTERVAL_SECONDS // 60} minutes so the "
                    "retailers are not hammered")
        previous, probe = upcoming, upcoming
    return trigger


def _load_saved_cron() -> str | None:
    try:
        from .db import session_scope
        from .models import Setting
        with session_scope() as session:
            row = session.get(Setting, _SETTING_KEY)
            return row.value if row else None
    except Exception:                                  # noqa: BLE001
        log.debug("no saved sweep schedule", exc_info=True)
        return None


def _save_cron(value: str | None) -> None:
    """Persist an override; None removes it (revert to the env default)."""
    from .db import session_scope
    from .models import Setting
    with session_scope() as session:
        row = session.get(Setting, _SETTING_KEY)
        if value is None:
            if row is not None:
                session.delete(row)
        elif row is None:
            session.add(Setting(key=_SETTING_KEY, value=value))
        else:
            row.value = value


def current() -> dict:
    """The live schedule, where it came from, and when the next sweep fires."""
    job = scheduler.get_job(_JOB_ID) if scheduler.running else None
    next_run = getattr(job, "next_run_time", None)
    return {
        "cron": _active_cron,
        "source": _cron_source,          # "env" | "runtime-override"
        "default_cron": settings.pw_check_cron,
        "next_sweep_at": next_run.isoformat() if next_run else None,
    }


def reschedule(cron: str | None) -> dict:
    """Apply a new sweep cadence now and persist it. None/'' → env default."""
    global _active_cron, _cron_source
    requested = (cron or "").strip()
    if requested.lower() in ("", "default", "reset"):
        trigger = validated_trigger(settings.pw_check_cron)
        _save_cron(None)
        _active_cron, _cron_source = settings.pw_check_cron, "env"
    else:
        trigger = validated_trigger(requested)
        _save_cron(requested)
        _active_cron, _cron_source = requested, "runtime-override"

    scheduler.add_job(sweep, trigger, id=_JOB_ID, replace_existing=True,
                      max_instances=1, coalesce=True, misfire_grace_time=600)
    log.info("sweep rescheduled (cron: %s, source: %s)", _active_cron, _cron_source)
    return current()


def start() -> None:
    global _active_cron, _cron_source
    saved = _load_saved_cron()
    cron, source = (saved, "runtime-override") if saved else (settings.pw_check_cron, "env")
    try:
        trigger = validated_trigger(cron)
    except ScheduleError as exc:
        log.warning("saved/env schedule rejected (%s); using default %r",
                    exc, settings.pw_check_cron)
        cron, source = settings.pw_check_cron, "env"
        trigger = CronTrigger.from_crontab(cron, timezone="UTC")

    _active_cron, _cron_source = cron, source
    scheduler.add_job(sweep, trigger, id=_JOB_ID, replace_existing=True,
                      max_instances=1, coalesce=True, misfire_grace_time=600)
    scheduler.start()
    log.info("scheduler started (cron: %s, source: %s)", cron, source)


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
