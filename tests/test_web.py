import re

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.heater import DemoHeater
from app.main import create_app


class LiveHeater(DemoHeater):
    def __init__(self):
        super().__init__()
        self.token = ""
        self.device_id = "test-heater-id"
        self.fail_stop = False
        self.login_calls = 0

    @property
    def logged_in(self):
        return bool(self.token)

    def login(self, email, password):
        self.login_calls += 1
        if email != "test@example.com" or password != "vendor-password":
            raise ValueError("Private credentials must not be echoed")
        self.token = "vendor-token-never-persist"

    def normal(self):
        if self.fail_stop:
            return False
        return super().normal()

    def close(self):
        self.token = ""


def sign_in(client, password="vendor-password", remember=False):
    page = client.get("/login")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    return client.post(
        "/login",
        data={
            "csrf": csrf,
            "email": "test@example.com",
            "password": password,
            **({"remember": "on"} if remember else {}),
        },
    )


def token(client):
    return re.search(r'name="csrf" value="([^"]+)"', client.get("/").text).group(1)


def test_schedule_persists_and_controls_work_in_demo(tmp_path):
    config = Config(data_dir=tmp_path)
    with TestClient(create_app(config, run_loop=False)) as client:
        csrf = token(client)
        response = client.post(
            "/schedule",
            data={
                "csrf": csrf,
                "start_time": "10:15",
                "duration": "90",
                "days": ["0", "2", "4"],
                "enabled": "on",
            },
        )
        assert response.status_code == 200
        state = client.get("/api/status").json()
        assert state["schedule"]["days"] == [0, 2, 4]
        assert state["schedule"]["enabled"] is True
        assert client.post("/boost", data={"csrf": csrf}).status_code == 200
        assert client.get("/api/status").json()["heater"]["mode"] == "Boost"
        assert client.post("/stop", data={"csrf": csrf}).status_code == 200
        assert client.get("/api/status").json()["heater"]["mode"] == "Standard"
    with TestClient(create_app(config, run_loop=False)) as client:
        state = client.get("/api/status").json()
        assert state["schedule"]["start_time"] == "10:15"
        assert state["schedule"]["duration"] == 90
        assert any(
            "Device reported Standard mode" in event["message"] for event in state["history"]
        )


def test_demo_schedule_cannot_carry_into_live_mode(tmp_path):
    with TestClient(create_app(Config(data_dir=tmp_path), run_loop=False)) as client:
        client.post(
            "/schedule",
            data={
                "csrf": token(client),
                "start_time": "10:00",
                "duration": "30",
                "days": "0",
                "enabled": "on",
            },
        )
        assert client.get("/api/status").json()["schedule"]["enabled"]
    live = Config(data_dir=tmp_path, driver="emerald")
    with TestClient(
        create_app(live, heater=LiveHeater(), run_loop=False), base_url="https://testserver"
    ) as client:
        assert sign_in(client).status_code == 200
        assert not client.get("/api/status").json()["schedule"]["enabled"]
        assert client.get("/api/status").json()["active"] is None


@pytest.mark.parametrize(
    "data",
    [
        {"start_time": "25:00", "duration": "60", "days": "0"},
        {"start_time": "10:00", "duration": "0", "days": "0"},
        {"start_time": "10:00", "duration": "721", "days": "0"},
        {"start_time": "10:00", "duration": "hello", "days": "0"},
        {"start_time": "10:00", "duration": "60", "days": "9"},
        {"start_time": "10:00", "duration": "60"},
    ],
)
def test_invalid_schedule_does_not_modify_saved_values(tmp_path, data):
    with TestClient(create_app(Config(data_dir=tmp_path), run_loop=False)) as client:
        before = client.get("/api/status").json()["schedule"]
        response = client.post("/schedule", data={"csrf": token(client), **data})
        assert response.status_code == 200
        assert client.get("/api/status").json()["schedule"] == before


def test_auth_and_csrf_protect_all_mutations(tmp_path):
    with TestClient(
        create_app(
            Config(data_dir=tmp_path, driver="emerald"), heater=LiveHeater(), run_loop=False
        ),
        base_url="https://testserver",
    ) as client:
        assert client.get("/", follow_redirects=False).status_code == 303
        assert client.get("/api/status").status_code == 401
        assert client.get("/healthz").status_code == 200
        for route in ("/schedule", "/boost", "/stop", "/logout", "/credentials/forget"):
            assert client.post(route).status_code == 401
        assert sign_in(client).status_code == 200
        assert client.get("/").status_code == 200
        for route in ("/schedule", "/boost", "/stop", "/logout", "/credentials/forget"):
            assert client.post(route, data={"csrf": "wrong"}).status_code == 403
        response = client.get("/")
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["cache-control"] == "no-store"


def test_live_starts_without_any_configured_credentials(tmp_path):
    config = Config(data_dir=tmp_path, driver="emerald")
    with TestClient(create_app(config, run_loop=False), base_url="https://testserver") as client:
        assert "Your login." in client.get("/login").text
        assert client.get("/api/status").status_code == 401


def test_only_one_controller_owns_the_data_directory(tmp_path):
    config = Config(data_dir=tmp_path)
    with TestClient(create_app(config, run_loop=False)):
        with pytest.raises(RuntimeError, match="Another controller"):
            with TestClient(create_app(config, run_loop=False)):
                pass


def test_login_tokens_and_credentials_never_reach_disk_or_browser(tmp_path):
    heater = LiveHeater()
    app = create_app(Config(data_dir=tmp_path, driver="emerald"), heater=heater, run_loop=False)
    with TestClient(app, base_url="https://testserver") as client:
        response = sign_in(client)
        assert response.status_code == 200
        assert heater.logged_in
        assert "Secure" in response.history[0].headers["set-cookie"]
        assert "HttpOnly" in response.history[0].headers["set-cookie"]
        assert "SameSite=strict" in response.history[0].headers["set-cookie"]
        for marker in ("vendor-password", "test@example.com", heater.token):
            assert marker not in response.text
            assert marker not in client.get("/api/status").text
            assert marker not in str(dict(client.cookies))
            assert marker.encode() not in b"".join(
                p.read_bytes() for p in tmp_path.iterdir() if p.is_file()
            )
        assert app.state.store.bound_device() == "test-heater-id"
    assert not heater.logged_in


def test_login_csrf_failure_and_bad_password_do_not_authenticate(tmp_path):
    heater = LiveHeater()
    with TestClient(
        create_app(Config(data_dir=tmp_path, driver="emerald"), heater=heater, run_loop=False),
        base_url="https://testserver",
    ) as client:
        assert (
            client.post(
                "/login",
                data={"email": "test@example.com", "password": "vendor-password", "csrf": "wrong"},
            ).status_code
            == 403
        )
        response = sign_in(client, password="wrong-password")
        assert response.status_code == 400
        assert "wrong-password" not in response.text
        assert not heater.logged_in
        assert client.get("/api/status").status_code == 401


def test_live_http_login_is_blocked(tmp_path):
    with TestClient(
        create_app(Config(data_dir=tmp_path, driver="emerald"), heater=LiveHeater(), run_loop=False)
    ) as client:
        assert 'name="email"' not in client.get("/login").text
        assert client.post("/login").status_code == 400


def test_logout_clears_tokens_and_rejects_replayed_cookie(tmp_path):
    heater = LiveHeater()
    app = create_app(Config(data_dir=tmp_path, driver="emerald"), heater=heater, run_loop=False)
    with TestClient(app, base_url="https://testserver") as client:
        sign_in(client)
        old_cookie = client.cookies.get("boost_session")
        client.post("/boost", data={"csrf": token(client)})
        assert heater.mode == "Boost"
        response = client.post("/logout", data={"csrf": token(client)})
        assert response.status_code == 200
        assert heater.mode == "Standard"
        assert not heater.logged_in
        assert not app.state.sessions.sessions
        client.cookies.set("boost_session", old_cookie)
        assert client.get("/api/status").status_code == 401


def test_logout_keeps_recovery_session_if_standard_unconfirmed(tmp_path):
    heater = LiveHeater()
    app = create_app(Config(data_dir=tmp_path, driver="emerald"), heater=heater, run_loop=False)
    with TestClient(app, base_url="https://testserver") as client:
        sign_in(client)
        client.post("/boost", data={"csrf": token(client)})
        heater.fail_stop = True
        response = client.post("/logout", data={"csrf": token(client)})
        assert "Sign-out paused" in response.text
        assert heater.logged_in
        assert client.get("/api/status").json()["active"]["status"] == "stopping"


def test_restart_requires_new_login_and_invalidates_old_session(tmp_path):
    config = Config(data_dir=tmp_path, driver="emerald")
    with TestClient(
        create_app(config, heater=LiveHeater(), run_loop=False), base_url="https://testserver"
    ) as client:
        sign_in(client)
        old_cookie = client.cookies.get("boost_session")
    with TestClient(
        create_app(config, heater=LiveHeater(), run_loop=False), base_url="https://testserver"
    ) as client:
        client.cookies.set("boost_session", old_cookie)
        assert client.get("/api/status").status_code == 401
        assert sign_in(client).status_code == 200


def test_login_attempts_rate_limited(tmp_path):
    with TestClient(
        create_app(
            Config(data_dir=tmp_path, driver="emerald"), heater=LiveHeater(), run_loop=False
        ),
        base_url="https://testserver",
    ) as client:
        for _ in range(8):
            assert sign_in(client, password="wrong-password").status_code == 400
        assert sign_in(client, password="wrong-password").status_code == 429
