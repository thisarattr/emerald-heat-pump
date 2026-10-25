import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.heater import DemoHeater
from app.scheduler import Scheduler, next_start, occurrence_at
from app.storage import Store

ZONE = ZoneInfo("Australia/Melbourne")


class RecordingHeater(DemoHeater):
    def __init__(self):
        super().__init__()
        self.boosts = 0
        self.stops = 0
        self.fail_start = False
        self.fail_stop = False

    def boost(self, deadline):
        self.boosts += 1
        if self.fail_start:
            raise RuntimeError("private vendor response")
        return super().boost(deadline)

    def normal(self):
        self.stops += 1
        if self.fail_stop:
            raise RuntimeError("private vendor response")
        return super().normal()


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    store.save_schedule(True, "11:00", 60, list(range(7)))
    heater = RecordingHeater()
    return store, heater, Scheduler(store, heater, ZONE.key)


def local(hour=11, minute=0, day=26):
    return datetime(2026, 10, day, hour, minute, tzinfo=ZONE).astimezone(UTC)


def test_scheduled_start_once_and_stop_at_deadline(setup):
    store, heater, scheduler = setup

    async def scenario():
        await scheduler.tick(local(10, 59))
        assert heater.boosts == 0
        await scheduler.tick(local())
        await scheduler.tick(local(11, 1))
        assert heater.boosts == 1
        assert store.active()["status"] == "active"
        await scheduler.tick(local(12))
        assert heater.mode == "Standard"
        assert store.active() is None
        await scheduler.tick(local(12, 1))
        assert heater.boosts == 1
        assert heater.stops == 1

    asyncio.run(scenario())


def test_restart_after_expiry_restores_without_reboost(setup):
    store, heater, scheduler = setup
    asyncio.run(scheduler.tick(local()))
    store = Store(store.path)
    restarted = Scheduler(store, heater, ZONE.key)
    asyncio.run(restarted.tick(local(13)))
    assert heater.boosts == 1
    assert heater.stops == 1
    assert store.active() is None


def test_pause_and_schedule_edit_do_not_change_active_deadline(setup):
    store, heater, scheduler = setup
    asyncio.run(scheduler.tick(local()))
    deadline = store.active()["ends_at"]
    store.save_schedule(False, "13:00", 180, [0])
    assert store.active()["ends_at"] == deadline
    asyncio.run(scheduler.tick(local(12)))
    assert heater.stops == 1
    asyncio.run(scheduler.tick(local(13)))
    assert heater.boosts == 1


def test_return_failure_retries_without_new_boost(setup):
    store, heater, scheduler = setup
    asyncio.run(scheduler.tick(local()))
    heater.fail_stop = True
    asyncio.run(scheduler.tick(local(12)))
    assert store.active()["status"] == "stopping"
    asyncio.run(scheduler.tick(local(12) + timedelta(seconds=30)))
    assert heater.stops == 1
    heater.fail_stop = False
    asyncio.run(scheduler.tick(local(12, 1)))
    assert heater.stops == 2
    assert heater.boosts == 1
    assert store.active() is None


def test_unconfirmed_start_not_retried_and_gets_cleanup(setup):
    store, heater, scheduler = setup
    heater.fail_start = True
    asyncio.run(scheduler.tick(local()))
    assert store.active()["status"] == "uncertain"
    asyncio.run(scheduler.tick(local(11, 1)))
    assert heater.boosts == 1
    assert "private vendor" not in str(store.history())
    asyncio.run(scheduler.tick(local(12)))
    assert heater.stops == 1
    assert store.active() is None


def test_late_start_skipped_and_deduplicated(setup):
    store, heater, scheduler = setup
    asyncio.run(scheduler.tick(local(11, 2)))
    asyncio.run(scheduler.tick(local(11, 3)))
    assert heater.boosts == 0
    assert len(store.history()) == 1
    assert store.active() is None


def test_disabled_or_wrong_weekday_never_starts(setup):
    store, heater, scheduler = setup
    store.save_schedule(False, "11:00", 60, [0])
    asyncio.run(scheduler.tick(local()))
    store.save_schedule(True, "11:00", 60, [1])
    asyncio.run(scheduler.tick(local()))
    assert heater.boosts == 0


def test_cross_midnight_deadline_survives_pause(setup):
    store, heater, scheduler = setup
    store.save_schedule(True, "23:30", 90, [0])
    asyncio.run(scheduler.tick(local(23, 30)))
    store.save_schedule(False, "23:30", 90, [0])
    asyncio.run(scheduler.tick(local(0, 59, day=27)))
    assert heater.stops == 0
    asyncio.run(scheduler.tick(local(1, day=27)))
    assert heater.stops == 1


def test_dst_missing_time_skipped_and_repeated_time_once(setup):
    store, heater, scheduler = setup
    assert occurrence_at(datetime(2026, 10, 4).date(), "02:30", ZONE) is None
    store.save_schedule(True, "02:30", 5, [6])
    first = datetime(2027, 4, 4, 2, 30, tzinfo=ZONE, fold=0).astimezone(UTC)
    second = datetime(2027, 4, 4, 2, 30, tzinfo=ZONE, fold=1).astimezone(UTC)
    asyncio.run(scheduler.tick(first))
    asyncio.run(scheduler.tick(first + timedelta(minutes=5)))
    asyncio.run(scheduler.tick(second))
    assert heater.boosts == 1
    assert heater.stops == 1


def test_next_occurrence_uses_selected_days_and_skips_used_day(setup):
    store, heater, scheduler = setup
    store.save_schedule(True, "11:00", 60, [0])
    assert next_start(store.schedule(), local(10), ZONE, store) == local()
    asyncio.run(scheduler.tick(local()))
    assert next_start(store.schedule(), local(11, 1), ZONE, store) == local() + timedelta(days=7)


def test_concurrent_starts_create_one_window(setup):
    store, heater, scheduler = setup

    async def scenario():
        results = await asyncio.gather(
            scheduler.manual_start(local()), scheduler.manual_start(local()), return_exceptions=True
        )
        assert sum(isinstance(result, ValueError) for result in results) == 1

    asyncio.run(scenario())
    assert heater.boosts == 1


def test_cancel_waits_for_inflight_command(setup):
    import threading

    _, _, scheduler = setup
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def command():
        entered.set()
        release.wait(2)
        finished.set()

    async def scenario():
        task = asyncio.create_task(scheduler._call(command))
        await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()

    asyncio.run(scenario())


def test_scheduler_waits_for_login_and_recovers_expired_window(setup, monkeypatch):
    store, heater, scheduler = setup
    logged_in = [False]
    monkeypatch.setattr(RecordingHeater, "logged_in", property(lambda self: logged_in[0]))
    asyncio.run(scheduler.tick(local()))
    assert heater.boosts == 0
    assert store.active() is None
    logged_in[0] = True
    asyncio.run(scheduler.tick(local() + timedelta(seconds=30)))
    assert heater.boosts == 1
    logged_in[0] = False
    asyncio.run(scheduler.tick(local(12)))
    assert heater.stops == 0
    assert store.active() is not None
    logged_in[0] = True
    asyncio.run(scheduler.tick(local(12, 1)))
    assert heater.stops == 1
    assert store.active() is None
