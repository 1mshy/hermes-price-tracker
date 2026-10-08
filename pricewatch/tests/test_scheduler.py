import pytest

from pricewatch import scheduler
from pricewatch.db import init_db


def test_valid_cron_accepted():
    trigger = scheduler.validated_trigger("*/30 * * * *")
    assert trigger is not None


def test_every_minute_rejected_as_impolite():
    with pytest.raises(scheduler.ScheduleError):
        scheduler.validated_trigger("* * * * *")


def test_garbage_rejected_with_helpful_message():
    with pytest.raises(scheduler.ScheduleError) as exc:
        scheduler.validated_trigger("often please")
    assert "5 fields" in str(exc.value)


def test_hourly_and_daily_accepted():
    scheduler.validated_trigger("0 * * * *")
    scheduler.validated_trigger("0 9 * * *")
    scheduler.validated_trigger("*/5 * * * *")     # exactly at the floor


def test_reschedule_persists_and_reverts():
    init_db()
    out = scheduler.reschedule("0 */2 * * *")
    assert out["cron"] == "0 */2 * * *"
    assert out["source"] == "runtime-override"
    assert scheduler._load_saved_cron() == "0 */2 * * *"

    out = scheduler.reschedule("default")
    assert out["source"] == "env"
    assert scheduler._load_saved_cron() is None


def test_reschedule_rejects_bad_cron_without_changing_state():
    init_db()
    before = scheduler.current()["cron"]
    with pytest.raises(scheduler.ScheduleError):
        scheduler.reschedule("* * * * *")
    assert scheduler.current()["cron"] == before


# ── the host sleeps: missed slots run once, after the network is back ─────
import asyncio
import socket


def test_scheduled_jobs_run_a_missed_slot_once_instead_of_dropping_it():
    init_db()
    scheduler.reschedule("default")
    job = next(j for j in scheduler.scheduler.get_jobs() if j.id == scheduler._JOB_ID)
    assert job.misfire_grace_time is None      # no "missed" after a long sleep
    assert job.coalesce is True                # ...and 6 missed slots = 1 run
    assert job.max_instances == 1


def test_network_ready_waits_out_a_dns_blackout(monkeypatch):
    answers = [socket.gaierror(-5, "No address associated with hostname"),
               socket.gaierror(-5, "No address associated with hostname"), []]

    def getaddrinfo(host, port):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(scheduler.socket, "getaddrinfo", getaddrinfo)
    assert asyncio.run(scheduler.network_ready(delays=(0, 0, 0))) is True
    assert answers == []


def test_network_ready_gives_up_when_dns_stays_down(monkeypatch):
    def getaddrinfo(host, port):
        raise socket.gaierror(-5, "No address associated with hostname")
    monkeypatch.setattr(scheduler.socket, "getaddrinfo", getaddrinfo)
    assert asyncio.run(scheduler.network_ready(delays=(0, 0))) is False


def test_probe_host_follows_the_ntfy_server(monkeypatch):
    from pricewatch.settings import settings
    monkeypatch.setattr(settings, "ntfy_server", "https://ntfy.example.org:8443/")
    assert scheduler.probe_host() == "ntfy.example.org"
    monkeypatch.setattr(settings, "ntfy_server", "")
    assert scheduler.probe_host() == "ntfy.sh"


def test_sweep_is_skipped_without_network_and_says_so(monkeypatch):
    from pricewatch import service
    swept = []

    async def refresh_offers():
        swept.append(1)
        return {"checked": 1, "updated": 1, "failed": 0, "alerts": []}

    async def offline():
        return False
    monkeypatch.setattr(service, "refresh_offers", refresh_offers)
    monkeypatch.setattr(scheduler, "network_ready", offline)
    asyncio.run(scheduler.sweep())
    assert swept == []
    assert scheduler.current()["last_skipped_at"] is not None

    async def online():
        return True
    monkeypatch.setattr(scheduler, "network_ready", online)
    asyncio.run(scheduler.sweep())
    assert swept == [1]


def test_health_reports_the_last_full_sweep_however_it_was_started(monkeypatch):
    from pricewatch import service
    from pricewatch.db import session_scope
    from pricewatch.models import Offer
    init_db()
    monkeypatch.setattr(service, "_last_full_sweep", None)
    with session_scope() as session:
        for offer in session.query(Offer).all():
            offer.active = False
    assert scheduler.current()["last_sweep_at"] is None
    asyncio.run(service.refresh_offers())              # what POST /api/refresh does
    status = scheduler.current()
    assert status["last_sweep_at"] is not None
    assert status["last_sweep"] == {"checked": 0, "updated": 0, "failed": 0, "alerts": 0}


def test_startup_runs_a_catchup_without_waiting_for_cron(monkeypatch):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    init_db()
    calls = []
    async def exercise():
        fresh = AsyncIOScheduler(timezone='UTC')
        monkeypatch.setattr(scheduler, 'scheduler', fresh)
        monkeypatch.setattr(scheduler.settings, 'pw_fx_enabled', False)
        arrived = asyncio.Event()
        async def sweep():
            calls.append(1)
            arrived.set()
        monkeypatch.setattr(scheduler, 'sweep', sweep)
        scheduler.start()
        try:
            await asyncio.wait_for(arrived.wait(), 1)
            assert fresh.get_job(scheduler._JOB_ID) is not None
        finally:
            scheduler.stop()
            await asyncio.sleep(0)
    asyncio.run(exercise())
    assert calls == [1]


def test_network_outage_keeps_a_retry_scheduled_until_success(monkeypatch):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    async def exercise():
        fresh = AsyncIOScheduler(timezone='UTC')
        monkeypatch.setattr(scheduler, 'scheduler', fresh)
        fresh.start()
        async def offline():
            return False
        async def online():
            return True
        async def refresh():
            return {'checked': 1, 'updated': 1, 'failed': 0, 'alerts': []}
        try:
            monkeypatch.setattr(scheduler, 'network_ready', offline)
            await scheduler.sweep()
            assert scheduler.current()['recovery_sweep_at'] is not None
            await scheduler.sweep()
            assert len(fresh.get_jobs()) == 1
            monkeypatch.setattr(scheduler, 'network_ready', online)
            monkeypatch.setattr(scheduler.service, 'refresh_offers', refresh)
            await scheduler.sweep()
            assert scheduler.current()['recovery_sweep_at'] is None
        finally:
            fresh.shutdown(wait=False)
            await asyncio.sleep(0)
    asyncio.run(exercise())
