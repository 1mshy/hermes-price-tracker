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
