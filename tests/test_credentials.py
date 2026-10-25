import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from test_web import LiveHeater, sign_in, token

from app.auth import LoginRejected, LoginRequired
from app.config import Config
from app.keygen import generate
from app.main import create_app
from app.scheduler import Scheduler
from app.storage import Store
from app.vault import CredentialVault, VaultError


@pytest.fixture
def vault(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    key = tmp_path / "secrets" / "emerald.key"
    generate(key)
    return CredentialVault(Store(data / "boost.sqlite3"), key)


def production_config(vault):
    return Config(
        data_dir=vault.store.path.parent,
        driver="emerald",
        credential_key_file=vault.key_file,
    )


def test_vault_encrypts_both_fields_and_forget_clears_record(vault):
    assert vault.available
    assert not vault.saved
    vault.save("test@example.com", "vendor-password")
    encrypted = vault.store.encrypted_login()
    assert vault.load() == ("test@example.com", "vendor-password")
    disk = b"".join(p.read_bytes() for p in vault.store.path.parent.iterdir() if p.is_file())
    assert b"test@example.com" not in disk
    assert b"vendor-password" not in disk
    assert encrypted in disk
    vault.forget()
    assert not vault.saved
    assert vault.load() is None
    disk = b"".join(p.read_bytes() for p in vault.store.path.parent.iterdir() if p.is_file())
    assert encrypted not in disk


def test_invalid_ciphertext_or_key_never_falls_back_to_plaintext(vault):
    vault.save("test@example.com", "vendor-password")
    original = vault.store.encrypted_login()
    vault.store.save_encrypted_login(original[:-8] + b"modified")
    with pytest.raises(VaultError):
        vault.load()
    vault.store.save_encrypted_login(original)
    vault.key_file.chmod(0o600)
    vault.key_file.write_bytes(Fernet.generate_key())
    with pytest.raises(VaultError):
        vault.load()
    vault.forget()
    assert not vault.saved


def test_missing_insecure_or_colocated_key_disables_saving(vault):
    vault.key_file.chmod(0o644)
    assert not vault.available
    with pytest.raises(VaultError):
        vault.save("test@example.com", "vendor-password")
    assert not vault.saved
    local_key = vault.store.path.parent / "bad.key"
    generate(local_key)
    assert not CredentialVault(vault.store, local_key).available
    vault.key_file.unlink()
    assert not vault.available
    vault.forget()


def test_key_generation_never_overwrites_existing_key(vault):
    before = vault.key_file.read_bytes()
    with pytest.raises(FileExistsError):
        generate(vault.key_file)
    assert vault.key_file.read_bytes() == before
    assert vault.key_file.stat().st_mode & 0o077 == 0


def test_opt_in_then_disable_deletes_credentials_but_keeps_token(vault):
    heater = LiveHeater()
    app = create_app(production_config(vault), heater=heater, run_loop=False)
    with TestClient(app, base_url="https://testserver") as client:
        assert sign_in(client, remember=True).status_code == 200
        state = client.get("/api/status").json()
        assert state["saved_login"]
        assert "vendor-password" not in str(state)
        assert "test@example.com" not in str(state)
        active_token = heater.token
        response = client.post("/credentials/forget", data={"csrf": token(client)})
        assert "Saving disabled and saved login deleted" in response.text
        assert not app.state.vault.saved
        assert heater.token == active_token
        assert not client.get("/api/status").json()["saved_login"]
        heater.token = ""
        asyncio.run(app.state.scheduler.tick())
        assert heater.login_calls == 1
    assert not CredentialVault(Store(vault.store.path), vault.key_file).saved


def test_session_only_login_also_deletes_previously_saved_credentials(vault):
    app = create_app(production_config(vault), heater=LiveHeater(), run_loop=False)
    with TestClient(app, base_url="https://testserver") as client:
        sign_in(client, remember=True)
        assert app.state.vault.saved
        assert sign_in(client, remember=False).status_code == 200
        assert not app.state.vault.saved


def test_forget_works_when_key_is_gone_and_emerald_auth_expired(vault):
    heater = LiveHeater()
    app = create_app(production_config(vault), heater=heater, run_loop=False)
    with TestClient(app, base_url="https://testserver") as client:
        sign_in(client, remember=True)
        vault.key_file.unlink()
        heater.token = ""
        response = client.post("/credentials/forget", data={"csrf": token(client)})
        assert response.status_code == 200
        assert not app.state.vault.saved
        assert not heater.logged_in


def test_remember_requires_a_key_and_does_not_save_on_login_failure(vault):
    heater = LiveHeater()
    app = create_app(production_config(vault), heater=heater, run_loop=False)
    with TestClient(app, base_url="https://testserver") as client:
        assert sign_in(client, password="wrong", remember=True).status_code == 400
        assert not vault.saved
        vault.key_file.unlink()
        before = heater.login_calls
        assert sign_in(client, remember=True).status_code == 400
        assert heater.login_calls == before
        assert not vault.saved


def test_restart_reauthenticates_only_with_saved_opt_in(vault):
    config = production_config(vault)
    app = create_app(config, heater=LiveHeater(), run_loop=False)
    with TestClient(app, base_url="https://testserver") as client:
        sign_in(client, remember=True)
    restarted_heater = LiveHeater()
    restarted = create_app(config, heater=restarted_heater, run_loop=False)
    with TestClient(restarted, base_url="https://testserver") as client:
        assert not restarted_heater.logged_in
        asyncio.run(restarted.state.scheduler.tick())
        assert restarted_heater.logged_in
        assert restarted_heater.login_calls == 1
        assert client.get("/api/status").status_code == 401
    vault.forget()
    third_heater = LiveHeater()
    with TestClient(create_app(config, heater=third_heater, run_loop=False)) as client:
        assert client.get("/api/status").status_code == 401
        assert third_heater.login_calls == 0


def test_expired_auth_relogs_and_retries_the_command_once(vault):
    class ExpiringHeater(LiveHeater):
        def __init__(self):
            super().__init__()
            self.boost_calls = 0

        def boost(self, deadline):
            self.boost_calls += 1
            if self.boost_calls == 1:
                self.token = ""
                raise LoginRequired()
            return super().boost(deadline)

    heater = ExpiringHeater()
    heater.login("test@example.com", "vendor-password")
    vault.save("test@example.com", "vendor-password")
    scheduler = Scheduler(vault.store, heater, "Australia/Melbourne", vault)
    asyncio.run(scheduler.manual_start())
    assert heater.login_calls == 2
    assert heater.boost_calls == 2
    assert vault.store.active()["status"] == "active"


def test_expired_auth_without_saving_never_relogs(vault):
    heater = LiveHeater()
    scheduler = Scheduler(vault.store, heater, "Australia/Melbourne", vault)
    asyncio.run(scheduler.tick())
    assert heater.login_calls == 0


def test_logout_pauses_saved_auto_login_across_restart(vault):
    config = production_config(vault)
    app = create_app(config, heater=LiveHeater(), run_loop=False)
    with TestClient(app, base_url="https://testserver") as client:
        sign_in(client, remember=True)
        client.post("/logout", data={"csrf": token(client)})
        asyncio.run(app.state.scheduler.tick())
        assert not app.state.scheduler.heater.logged_in
        assert app.state.vault.saved
    heater = LiveHeater()
    restarted = create_app(config, heater=heater, run_loop=False)
    with TestClient(restarted, base_url="https://testserver"):
        asyncio.run(restarted.state.scheduler.tick())
        assert heater.login_calls == 0


def test_failed_auto_login_backs_off_and_never_logs_plaintext(vault):
    class OfflineHeater(LiveHeater):
        def login(self, email, password):
            self.login_calls += 1
            raise RuntimeError(email + password)

    heater = OfflineHeater()
    vault.save("test@example.com", "vendor-password")
    scheduler = Scheduler(vault.store, heater, "Australia/Melbourne", vault)
    now = datetime.now(UTC)
    asyncio.run(scheduler.tick(now))
    asyncio.run(scheduler.tick(now + timedelta(seconds=30)))
    assert heater.login_calls == 1
    assert "test@example.com" not in str(vault.store.history())
    assert "vendor-password" not in str(vault.store.history())
    asyncio.run(scheduler.tick(now + timedelta(minutes=5)))
    assert heater.login_calls == 2


def test_rejected_saved_password_stops_auto_login_attempts(vault):
    class RejectedHeater(LiveHeater):
        def login(self, email, password):
            self.login_calls += 1
            raise LoginRejected()

    heater = RejectedHeater()
    vault.save("test@example.com", "vendor-password")
    scheduler = Scheduler(vault.store, heater, "Australia/Melbourne", vault)
    now = datetime.now(UTC)
    asyncio.run(scheduler.tick(now))
    asyncio.run(scheduler.tick(now + timedelta(days=1)))
    assert heater.login_calls == 1
    assert scheduler.auth_blocked
