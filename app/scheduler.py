import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.auth import LoginRejected, LoginRequired
from app.heater import Heater
from app.storage import Store
from app.vault import CredentialVault, VaultError

logger = logging.getLogger(__name__)


def occurrence_at(day, start: str, zone: ZoneInfo):
    wall = datetime.combine(day, time.fromisoformat(start))
    candidate = wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
    if candidate.astimezone(zone).replace(tzinfo=None) != wall:
        return None
    return candidate


def occurrence_key(start: datetime, zone: ZoneInfo):
    return f"{zone.key}:{start.astimezone(zone).date()}"


def next_start(schedule: dict, now: datetime, zone: ZoneInfo, store: Store):
    if not schedule["enabled"]:
        return None
    for offset in range(8):
        day = now.astimezone(zone).date() + timedelta(days=offset)
        if day.weekday() not in schedule["days"]:
            continue
        candidate = occurrence_at(day, schedule["start_time"], zone)
        if (
            candidate
            and candidate > now
            and not store.has_occurrence(occurrence_key(candidate, zone))
        ):
            return candidate
    return None


class Scheduler:
    def __init__(
        self, store: Store, heater: Heater, timezone: str, vault: CredentialVault | None = None
    ):
        self.store = store
        self.heater = heater
        self.zone = ZoneInfo(timezone)
        self.lock = asyncio.Lock()
        self.next_retry = datetime.min.replace(tzinfo=UTC)
        self.last_tick = None
        self.last_error = None
        self.vault = vault
        self.auth_error = None
        self.auth_blocked = False
        self.next_login = datetime.min.replace(tzinfo=UTC)

    def reset_auth_retry(self):
        self.auth_error = None
        self.auth_blocked = False
        self.next_login = datetime.min.replace(tzinfo=UTC)

    async def _ensure_login(self, now):
        if self.heater.logged_in:
            return True
        if not self.vault or not self.vault.saved or self.store.auto_login_paused():
            return False
        if self.auth_blocked or now < self.next_login:
            return False
        email = password = ""
        try:
            email, password = self.vault.load()
            await self._call(self.heater.login, email, password)
            self.reset_auth_retry()
            self.store.event(now, "Signed in automatically using the encrypted saved login.")
            return True
        except (VaultError, LoginRejected):
            self.auth_blocked = True
            message = "Saved login needs attention. Sign in again or disable saving."
        except Exception:
            self.next_login = now + timedelta(minutes=5)
            message = "Automatic sign-in unavailable. Retrying in five minutes."
        finally:
            email = password = ""
        if message != self.auth_error:
            self.store.event(now, message)
        self.auth_error = message
        return False

    async def _command(self, method, *args):
        try:
            return await self._call(method, *args)
        except LoginRequired:
            if not await self._ensure_login(datetime.now(UTC)):
                raise
            return await self._call(method, *args)

    async def _call(self, method, *args):
        task = asyncio.create_task(asyncio.to_thread(method, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # A cancelled to_thread await does not stop an in-flight device command.
            # Keep ownership until it finishes before disconnecting or unlocking.
            with suppress(Exception):
                await task
            raise

    async def tick(self, now: datetime | None = None):
        now = now or datetime.now(UTC)
        async with self.lock:
            self.last_tick = now
            if not await self._ensure_login(now):
                return
            active = self.store.active()
            if active:
                if active["status"] == "stopping" or now >= datetime.fromisoformat(
                    active["ends_at"]
                ):
                    if now >= self.next_retry:
                        await self._stop(active, now)
                return
            schedule = self.store.schedule()
            day = now.astimezone(self.zone).date()
            if not schedule["enabled"] or day.weekday() not in schedule["days"]:
                return
            start = occurrence_at(day, schedule["start_time"], self.zone)
            if not start or start > now:
                return
            key = occurrence_key(start, self.zone)
            if self.store.has_occurrence(key):
                return
            end = start + timedelta(minutes=schedule["duration"])
            if now - start > timedelta(seconds=90):
                run_id = self.store.reserve("schedule", start, end, key)
                if run_id:
                    self.store.update_run(run_id, "missed", "Missed schedule; no late boost")
                    self.store.event(
                        now, "Missed scheduled start. Skipped to avoid an unexpected late boost."
                    )
                return
            await self._start("schedule", start, end, key, now)

    async def _start(self, source, start, end, occurrence, now):
        run_id = self.store.reserve(source, start, end, occurrence)
        if not run_id:
            return
        self.store.event(
            now, "Starting scheduled boost." if source == "schedule" else "Starting manual boost."
        )
        try:
            confirmed = await self._command(self.heater.boost, end)
            status = "active" if confirmed else "uncertain"
            message = (
                "Boost reported by device."
                if confirmed
                else "Boost command sent; device confirmation unavailable."
            )
        except Exception:
            status = "uncertain"
            message = (
                "Boost not confirmed. Check Emerald app; Standard will be requested at expiry."
            )
        self.store.update_run(run_id, status, message)
        self.store.event(now, message)

    async def _stop(self, active, now):
        self.store.update_run(active["id"], "stopping", "Returning to Standard")
        try:
            confirmed = await self._command(self.heater.normal)
            if not confirmed:
                raise RuntimeError("Device has not reported Standard mode")
        except Exception:
            self.next_retry = now + timedelta(seconds=60)
            message = "Standard mode not confirmed. Retrying in 60 seconds; check Emerald app."
            if active["message"] != message:
                self.store.event(now, message)
            self.store.update_run(active["id"], "stopping", message)
            return
        self.store.update_run(active["id"], "completed", "Returned to Standard")
        self.store.event(now, "Boost window ended. Device reported Standard mode.")

    async def manual_start(self, now: datetime | None = None):
        now = now or datetime.now(UTC)
        async with self.lock:
            if self.store.active():
                raise ValueError("A boost window is already active or awaiting recovery.")
            if not await self._ensure_login(now):
                raise ValueError("Sign in to Emerald before starting a boost.")
            end = now + timedelta(minutes=self.store.schedule()["duration"])
            await self._start("manual", now, end, None, now)

    async def stop(self, now: datetime | None = None):
        async with self.lock:
            if active := self.store.active():
                await self._stop(active, now or datetime.now(UTC))

    async def loop(self):
        while True:
            try:
                await self.tick()
                self.last_error = None
            except Exception:
                self.last_error = "Scheduler error. Check server logs and storage permissions."
                logger.error("Scheduler tick failed; retrying", exc_info=True)
            await asyncio.sleep(5)
