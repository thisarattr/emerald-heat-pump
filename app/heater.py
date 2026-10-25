import logging
import threading
import time
from datetime import UTC, datetime
from typing import Protocol

from app.auth import LoginRejected, LoginRequired
from app.config import Config


class HeaterError(Exception):
    pass


class Heater(Protocol):
    @property
    def logged_in(self) -> bool: ...
    def login(self, email: str, password: str) -> None: ...
    def boost(self, deadline: datetime) -> bool: ...
    def normal(self) -> bool: ...
    def status(self) -> dict: ...
    def close(self) -> None: ...


class DemoHeater:
    def __init__(self):
        self.mode = "Standard"

    @property
    def logged_in(self):
        return True

    def boost(self, deadline: datetime) -> bool:
        self.mode = "Boost"
        return True

    def normal(self) -> bool:
        self.mode = "Standard"
        return True

    def status(self):
        return {
            "mode": self.mode,
            "connected": True,
            "detail": "Simulated heat pump · no device commands",
        }

    def close(self):
        pass


class EmeraldHeater:
    def __init__(self, config: Config):
        self.config = config
        self.client = None
        self.device_id = None
        self.updated = threading.Event()
        self.reported_mode = "Unknown"
        self.reported_power = None
        self.reported_at = None

    @property
    def logged_in(self):
        return bool(self.client and self.client.token and not self.client.auth_expired.is_set())

    def _update(self):
        client = self.client
        if client and self.device_id:
            with client._state_lock:
                for prop in client.properties:
                    for device in prop.get("heat_pump", []):
                        if device.get("id") == self.device_id:
                            last = device.get("last_state") or {}
                            self.reported_mode = {"0": "Boost", "1": "Standard", "2": "Quiet"}.get(
                                str(last.get("mode")), "Unknown"
                            )
                            self.reported_power = last.get("switch")
                            self.reported_at = datetime.now(UTC)
        self.updated.set()

    def login(self, email: str, password: str):
        from emerald_hws.emeraldhws import EmeraldHWS

        logging.getLogger("emerald_hws").disabled = True
        logging.getLogger("emerald_hws.emeraldhws").disabled = True

        class TokenOnlyClient(EmeraldHWS):
            def getLoginToken(self):
                if self.token and not self._setup_complete:
                    return True
                self.auth_expired.set()
                self.token = ""
                raise LoginRequired("Emerald session expired. Sign in again.")

        client = TokenOnlyClient(email, password, update_callback=self._update)
        client.auth_expired = threading.Event()
        try:
            try:
                # The base method exchanges credentials exactly once. Later calls
                # use the override above and can never re-submit a password.
                EmeraldHWS.getLoginToken(client)
            finally:
                client.email = ""
                client.password = ""
                email = password = ""
            client.connect()
            devices = client.listHWS()
            if self.config.device_id:
                if self.config.device_id not in devices:
                    raise HeaterError("Configured heat pump not found. Check EMERALD_DEVICE_ID.")
                selected = self.config.device_id
            elif len(devices) == 1:
                selected = devices[0]
            else:
                raise HeaterError(
                    "Set EMERALD_DEVICE_ID when the account has zero or multiple heat pumps."
                )
            if self.device_id and self.device_id != selected:
                raise HeaterError("Sign in to the same heat pump already used by this controller.")
            self.close()
            self.device_id = selected
            self.client = client
            self._update()
        except HeaterError:
            client.disconnect()
            client.token = ""
            raise
        except Exception as error:
            client.disconnect()
            client.token = ""
            if getattr(error, "status_code", None) in (401, 403) or getattr(
                error, "api_code", None
            ) in (401, 403):
                raise LoginRejected("Emerald rejected the login. Sign in again.") from None
            raise HeaterError(
                "Sign-in failed. Check your login, device Wi-Fi and Emerald service."
            ) from None

    def _require_login(self):
        if not self.logged_in:
            raise LoginRequired("Sign in to Emerald before running the schedule.")
        try:
            self.client.getAllHWS()
        except Exception as error:
            self._handle_failure(error)
            if not self.logged_in:
                raise LoginRequired("Emerald session expired.") from None
            raise

    def _handle_failure(self, error):
        if getattr(error, "status_code", None) in (401, 403) or getattr(
            error, "api_code", None
        ) in (401, 403):
            self.client.auth_expired.set()
            self.client.token = ""

    def _reported_mode(self):
        if self.reported_power in (0, "off"):
            return "Off"
        return self.reported_mode

    def _confirm(self, expected: str):
        self.updated.clear()
        self.client.requestStatusUpdate(self.device_id)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.updated.wait(timeout=min(0.5, max(0, deadline - time.monotonic()))):
                if self.reported_mode == expected and (
                    expected != "Boost" or self.reported_power in (1, "on")
                ):
                    return True
                self.updated.clear()
        return False

    def boost(self, deadline: datetime) -> bool:
        try:
            self._require_login()
            if datetime.now(UTC) >= deadline:
                raise HeaterError("Boost window expired before connection completed.")
            self.client.turnOn(self.device_id)
            if datetime.now(UTC) >= deadline:
                raise HeaterError("Boost window expired before boost command could be sent.")
            self.client.setBoostMode(self.device_id)
            return self._confirm("Boost")
        except (HeaterError, LoginRequired):
            raise
        except Exception as error:
            self._handle_failure(error)
            raise HeaterError(
                "Emerald connection failed. Check credentials, Wi-Fi and cloud access."
            ) from None

    def normal(self) -> bool:
        try:
            self._require_login()
            self.client.setNormalMode(self.device_id)
            return self._confirm("Standard")
        except (HeaterError, LoginRequired):
            raise
        except Exception as error:
            self._handle_failure(error)
            raise HeaterError(
                "Cannot confirm Standard mode. Will retry; check Emerald app."
            ) from None

    def status(self):
        if not self.logged_in:
            return {"mode": "Unknown", "connected": False, "detail": "Sign in to resume automation"}
        connected = self.client._connection_event.is_set()
        return {
            "mode": self._reported_mode() if connected else "Unknown",
            "connected": connected,
            "detail": "Last reported device state"
            if connected
            else "Emerald connection unavailable",
            "reported_at": self.reported_at.isoformat() if self.reported_at else None,
        }

    def close(self):
        if self.client:
            client = self.client
            self.client = None
            try:
                client.disconnect()
            finally:
                client.token = ""
                client.email = client.password = ""
