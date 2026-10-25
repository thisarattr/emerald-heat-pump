import sys
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.auth import LoginRequired
from app.config import Config
from app.heater import EmeraldHeater, HeaterError


class FakeClient:
    devices = ["heater-1"]

    def __init__(self, email, password, update_callback):
        self.email = email
        self.password = password
        self.token = ""
        self._setup_complete = False
        self.sign_ins = 0
        self.reject_token = False
        self.callback = update_callback
        self._state_lock = threading.Lock()
        self._connection_event = threading.Event()
        self.properties = [
            {"heat_pump": [{"id": "heater-1", "last_state": {"mode": 1, "switch": 1}}]}
        ]
        self.commands = []
        self.disconnected = False

    def connect(self):
        self.getLoginToken()
        self._setup_complete = True
        self._connection_event.set()

    def getLoginToken(self):
        self.sign_ins += 1
        self.token = "vendor-token-for-testing"

    def getAllHWS(self):
        if self.reject_token:
            error = RuntimeError("private auth response")
            error.status_code = 401
            raise error

    def listHWS(self):
        return self.devices

    def turnOn(self, device):
        self.commands.append("on")
        self.properties[0]["heat_pump"][0]["last_state"]["switch"] = 1

    def setBoostMode(self, device):
        self.commands.append("boost")
        self.properties[0]["heat_pump"][0]["last_state"]["mode"] = 0

    def setNormalMode(self, device):
        self.commands.append("normal")
        self.properties[0]["heat_pump"][0]["last_state"]["mode"] = 1

    def requestStatusUpdate(self, device):
        self.callback()

    def disconnect(self):
        self.disconnected = True


@pytest.fixture
def heater(monkeypatch):
    monkeypatch.setitem(
        sys.modules, "emerald_hws.emeraldhws", SimpleNamespace(EmeraldHWS=FakeClient)
    )
    return EmeraldHeater(Config(driver="emerald"))


def test_adapter_boost_and_standard_use_vendor_commands(heater):
    heater.login("test@example.com", "vendor-password")
    assert heater.boost(datetime.now(UTC) + timedelta(minutes=30))
    client = heater.client
    assert client.commands == ["on", "boost"]
    assert heater.status()["mode"] == "Boost"
    assert heater.normal()
    assert client.commands == ["on", "boost", "normal"]
    assert heater.status()["mode"] == "Standard"
    heater.close()
    assert client.disconnected


def test_expired_window_never_sends_boost(heater):
    heater.login("test@example.com", "vendor-password")
    with pytest.raises(HeaterError, match="expired"):
        heater.boost(datetime.now(UTC) - timedelta(seconds=1))
    assert heater.client.commands == []
    heater.close()


def test_multiple_devices_require_explicit_selection(heater, monkeypatch):
    monkeypatch.setattr(FakeClient, "devices", ["heater-1", "heater-2"])
    with pytest.raises(HeaterError, match="EMERALD_DEVICE_ID"):
        heater.login("test@example.com", "vendor-password")
    assert heater.client is None


def test_credentials_discarded_and_no_automatic_password_login(heater):
    heater.login("test@example.com", "vendor-password")
    client = heater.client
    assert client.email == client.password == ""
    assert client.sign_ins == 1
    assert client.token == "vendor-token-for-testing"
    assert "test@example.com" not in repr(heater.config)
    assert "vendor-password" not in repr(heater.config)
    with pytest.raises(LoginRequired):
        client.getLoginToken()
    assert client.sign_ins == 1
    assert not heater.logged_in
    heater.close()
    assert client.token == ""


def test_rejected_bearer_token_requires_manual_login(heater):
    heater.login("test@example.com", "vendor-password")
    client = heater.client
    client.reject_token = True
    with pytest.raises(LoginRequired):
        heater.boost(datetime.now(UTC) + timedelta(minutes=30))
    assert not heater.logged_in
    assert client.token == ""
    assert client.sign_ins == 1
    assert client.commands == []
    heater.close()


def test_unauthenticated_commands_never_sign_in(heater):
    with pytest.raises(LoginRequired):
        heater.boost(datetime.now(UTC) + timedelta(minutes=30))
    with pytest.raises(LoginRequired):
        heater.normal()
    assert heater.client is None


def test_login_cannot_change_the_bound_heat_pump(heater, monkeypatch):
    heater.login("test@example.com", "vendor-password")
    old = heater.client
    monkeypatch.setattr(FakeClient, "devices", ["heater-2"])
    with pytest.raises(HeaterError, match="same heat pump"):
        heater.login("another@example.com", "other-password")
    assert heater.client is old
    assert old.email == old.password == ""
    heater.close()


def test_failed_token_exchange_discards_credentials(heater, monkeypatch):
    clients = []

    def fail(client):
        clients.append(client)
        raise RuntimeError("Private vendor error")

    monkeypatch.setattr(FakeClient, "getLoginToken", fail)
    with pytest.raises(HeaterError):
        heater.login("test@example.com", "vendor-password")
    assert clients[0].email == clients[0].password == clients[0].token == ""
    assert heater.client is None
