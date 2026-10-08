"""Periodic price sweeps — reschedulable at runtime, persisted across restarts.

The sweep cadence starts from PW_CHECK_CRON but can be changed live (by the
agent via the set_sweep_schedule MCP tool, or PATCH /api/schedule). A changed
schedule is stored in the settings table so a container restart keeps it.

The same scheduler also refreshes the indicative exchange rates (fx.py) on
PW_FX_REFRESH_CRON — fixed at startup, not reschedulable, because nothing the
user does should need it to change.

The host is a laptop that sleeps, gets unplugged and moves between networks,
so two things here are about coming back rather than running on time: a slot
missed while the container was frozen is run once as soon as the process
resumes (coalesce, no misfire grace), and that catch-up first waits for name
resolution to return, because Docker's DNS on the host side is the last thing
to wake up.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import socket
from collections.abc import Sequence
from urllib.parse import urlsplit

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.base import JobLookupError

from . import fx, service
from .settings import Settings, settings

log = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="UTC")

_JOB_ID = "price_sweep"
_SETTING_KEY = "sweep_cron"
_FX_JOB_ID = "fx_refresh"
_RECOVERY_JOB_ID = "recovery_sweep"
NETWORK_PROBE_TIMEOUT = 5
RECOVERY_DELAY_SECONDS = 60

#: Politeness floor — sweeping 38 retailers more often than this is a good way
#: to get every adapter blocked (see README "Notes and caveats").
MIN_SWEEP_INTERVAL_SECONDS = 300
#: The ECB publishes one rate a day and Frankfurter is a free service, so a
#: refresh more often than hourly is pure waste.
MIN_FX_INTERVAL_SECONDS = 3600

#: Delays (seconds) between DNS probes before a sweep. After a laptop resume
#: the first tick fires immediately, but the host-side resolver Docker hands
#: the containers is often a few seconds behind — the 2026-09-09 watchdog push
#: died on exactly that ("No address associated with hostname"). Summing to
#: about a minute; if the network is still gone after that, this slot is
#: skipped and the next tick (or POST /api/refresh from the wake hook) tries.
NETWORK_PROBE_DELAYS: tuple[float, ...] = (0, 3, 5, 10, 20, 30)

_active_cron: str = settings.pw_check_cron
_cron_source: str = "env"
_last_skipped_at: dt.datetime | None = None


#: Job options shared by every scheduled task. `misfire_grace_time=None` is the
#: point: with the default (or the 10 min this used to be) a slot that passed
#: while the laptop slept is logged as "missed" and dropped, so after a wake
#: the next check waited for the next cron boundary — up to half an hour of
#: stale prices. None runs the overdue slot as soon as the loop is back;
#: coalesce turns any number of missed slots into that one run.
_CATCH_UP = {"max_instances": 1, "coalesce": True, "misfire_grace_time": None}


class ScheduleError(ValueError):
    """The requested cron expression is invalid or impolitely frequent."""


def probe_host() -> str:
    """The name whose resolution stands in for "the network is back".

    The ntfy server is what the first alert after a resume has to reach, so
    it is the honest probe; a self-hosted one on the LAN works the same way.
    """
    return urlsplit(settings.ntfy_server or "https://ntfy.sh").hostname or "ntfy.sh"


async def network_ready(delays: Sequence[float] = NETWORK_PROBE_DELAYS) -> bool:
    """Wait, with backoff, until DNS answers for probe_host(). False = gave up."""
    host = probe_host()
    for attempt, delay in enumerate(delays, start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            await asyncio.wait_for(asyncio.to_thread(socket.getaddrinfo, host, 443),
                                   timeout=NETWORK_PROBE_TIMEOUT)
        except (OSError, asyncio.TimeoutError) as exc:
            log.warning("network probe %d/%d: cannot resolve %s (%s)",
                        attempt, len(delays), host, exc)
            continue
        if attempt > 1:
            log.info("network is back (resolved %s on probe %d)", host, attempt)
        return True
    return False


def retry_sweep() -> None:
    """Keep a missed sweep pending without changing the user's regular cadence."""
    if scheduler.running:
        scheduler.add_job(sweep, "date", id=_RECOVERY_JOB_ID, replace_existing=True,
                          run_date=dt.datetime.now(dt.timezone.utc)
                          + dt.timedelta(seconds=RECOVERY_DELAY_SECONDS), **_CATCH_UP)


async def sweep() -> None:
    global _last_skipped_at
    if not await network_ready():
        _last_skipped_at = dt.datetime.now(dt.timezone.utc)
        log.warning("sweep postponed: no network (%s does not resolve); retry in %ss",
                    probe_host(), RECOVERY_DELAY_SECONDS)
        retry_sweep()
        return
    try:
        summary = await service.refresh_offers()
        log.info("sweep: checked=%s updated=%s failed=%s alerts=%s",
                 summary["checked"], summary["updated"], summary["failed"],
                 len(summary["alerts"]))
        try:
            scheduler.remove_job(_RECOVERY_JOB_ID)
        except JobLookupError:
            pass
    except Exception:
        log.exception("scheduled sweep failed")
        retry_sweep()


async def refresh_rates() -> None:
    # fx.refresh() reports failure rather than raising, but a scheduler job
    # must not be trusted to keep that promise: an exception here would kill
    # the job for good.
    try:
        await fx.refresh()
    except Exception:
        log.exception("scheduled exchange-rate refresh failed")


def _parse_cron(cron: str) -> CronTrigger:
    try:
        return CronTrigger.from_crontab(cron.strip(), timezone="UTC")
    except ValueError as exc:
        raise ScheduleError(
            f"invalid cron expression {cron!r}: {exc} "
            "(expected 5 fields, e.g. '*/30 * * * *' for every 30 minutes)"
        ) from exc


def _shortest_gap(trigger: CronTrigger) -> float | None:
    """Seconds between the next few fire times — catches e.g. "* * * * *"."""
    shortest: float | None = None
    previous: dt.datetime | None = None
    probe = dt.datetime.now(dt.timezone.utc)
    for _ in range(4):
        upcoming = trigger.get_next_fire_time(previous, probe)
        if upcoming is None:
            break
        if previous is not None:
            gap = (upcoming - previous).total_seconds()
            shortest = gap if shortest is None else min(shortest, gap)
        previous, probe = upcoming, upcoming
    return shortest


def validated_trigger(cron: str) -> CronTrigger:
    """Parse a 5-field cron expression, rejecting overly aggressive cadences."""
    trigger = _parse_cron(cron)
    gap = _shortest_gap(trigger)
    if gap is not None and gap < MIN_SWEEP_INTERVAL_SECONDS:
        raise ScheduleError(
            f"{cron!r} would sweep every {int(gap)}s — the floor is "
            f"{MIN_SWEEP_INTERVAL_SECONDS // 60} minutes so the "
            "retailers are not hammered")
    return trigger


def fx_trigger() -> CronTrigger:
    """The rate-refresh cadence from PW_FX_REFRESH_CRON.

    Nobody is on the other end of a bad value at startup, so instead of a
    ScheduleError it is logged and the built-in default takes over.
    """
    default = Settings.model_fields["pw_fx_refresh_cron"].default
    cron = settings.pw_fx_refresh_cron
    try:
        trigger = _parse_cron(cron)
        gap = _shortest_gap(trigger)
        if gap is not None and gap < MIN_FX_INTERVAL_SECONDS:
            raise ScheduleError(
                f"{cron!r} would refresh rates every {int(gap)}s — the floor "
                "is hourly; the ECB publishes once a day")
    except ScheduleError as exc:
        log.warning("PW_FX_REFRESH_CRON rejected (%s); using default %r", exc, default)
        return CronTrigger.from_crontab(default, timezone="UTC")
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
    last = service.last_full_sweep()
    recovery = scheduler.get_job(_RECOVERY_JOB_ID) if scheduler.running else None
    recovery_at = getattr(recovery, "next_run_time", None)
    return {
        "cron": _active_cron,
        "source": _cron_source,          # "env" | "runtime-override"
        "default_cron": settings.pw_check_cron,
        "next_sweep_at": next_run.isoformat() if next_run else None,
        # Any full sweep counts, the scheduled ones and POST /api/refresh alike.
        "last_sweep_at": last["at"] if last else None,
        "last_sweep": {k: v for k, v in last.items() if k != "at"} if last else None,
        "last_skipped_at": _last_skipped_at.isoformat() if _last_skipped_at else None,
        "recovery_sweep_at": recovery_at.isoformat() if recovery_at else None,
        "sweep_running": service._full_sweep is not None and not service._full_sweep.done(),
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

    scheduler.add_job(sweep, trigger, id=_JOB_ID, replace_existing=True, **_CATCH_UP)
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
    scheduler.add_job(sweep, trigger, id=_JOB_ID, replace_existing=True, **_CATCH_UP)
    # In-memory cron jobs forget missed slots when Docker itself restarts.
    # Always catch up once on boot, without delaying API/MCP readiness.
    scheduler.add_job(sweep, "date", id=_RECOVERY_JOB_ID, replace_existing=True,
                      **_CATCH_UP)
    if settings.pw_fx_enabled:
        scheduler.add_job(refresh_rates, fx_trigger(), id=_FX_JOB_ID,
                          replace_existing=True, **_CATCH_UP)
    scheduler.start()
    log.info("scheduler started (cron: %s, source: %s; fx refresh: %s)", cron, source,
             settings.pw_fx_refresh_cron if settings.pw_fx_enabled else "disabled")


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
