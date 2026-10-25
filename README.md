# Emerald Boost

A small web app that starts an Emerald hot-water heat pump in boost mode on a weekly schedule, then requests Standard mode after the selected duration. Version one has no solar, inverter, battery or weather integration.

Python 3.12, FastAPI, server-rendered HTML, a small amount of JavaScript, and SQLite. One container and one worker. No external database or task queue.

For TrueNAS, use the published image **`ghcr.io/thisarattr/emerald-heat-pump:stable`** and [ready-to-paste configuration](compose.truenas.yaml). No source checkout, build commands, manual encryption-key creation or host-directory permissions are required. See [TrueNAS setup and updates](#truenas-25x).

## Try it locally

With [uv](https://docs.astral.sh/uv/):

```sh
uv sync --locked
uv run uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Open http://localhost:8080. It starts in **demo mode**, with the schedule paused. Demo controls simulate a heat pump and never log in to Emerald. Choose days, time and duration, then enable and save the schedule. “Boost now” uses the last saved duration.

## Docker Compose

```sh
cp .env.example .env
docker compose up -d --build
```

Open http://localhost:8080. Compose creates a persistent named volume with the correct permissions automatically. The image runs as UID/GID `10001:10001`. If you change `APP_DATA_PATH` to a host directory, create that directory first and grant this identity write access. On TrueNAS, use the dataset mount and ACL setup below.

The default port is bound to localhost. To access it from other home devices, bind to the server's LAN IP or put it behind your HTTPS reverse proxy. Demo has no authentication and never connects to Emerald; live mode always requires sign-in.

## Connect your Emerald

Set `HEATER_DRIVER=emerald` and recreate the container. **Do not put an email, username, password or token in `.env` or the TrueNAS YAML.** Open the login page over HTTPS and enter the same email/password used in the Emerald mobile app.

- Saving is off on a new installation. With Remember login unchecked, credentials exist only while processing sign-in and no credential record is kept. With it checked, email/password are encrypted together in SQLite for automatic re-login. The library's plaintext credential attributes are cleared after every token exchange.
- Emerald bearer tokens and temporary MQTT credentials stay in server process memory. They are never written to SQLite, configuration, browser storage, logs or cookies by this app.
- The browser receives a separate random HttpOnly, SameSite=Strict session cookie, not the Emerald token. The server keeps only its hash in memory. The cookie has no persistent expiry; the server expires dashboard sessions after eight hours. Closing the browser does not sign the controller out.
- Expiring a dashboard session does not itself stop automation. Remember login allows background sign-in after Emerald authentication expiry or a restart. Without it, manual sign-in is required. Automatic background sign-in never grants a browser session; the dashboard still needs authentication.
- No browser localStorage/sessionStorage is used. The form requests `autocomplete=off`, but browser password managers and session restoration are controlled by your browser; decline saving the password there if desired.
- Logout ends an active boost first, then disconnects, drops the server tokens and all dashboard sessions, and persistently pauses automatic sign-in until the next manual login. It does not delete an opted-in encrypted login. If Standard cannot be confirmed, logout is held so recovery can continue; the UI explains this.
- Schedules, deadlines, activity messages and the selected heat pump UUID persist. The UUID prevents accidentally applying an existing schedule to a different heat pump when another account signs in. Email/password are persisted only as an encrypted record when saving is enabled; Emerald session tokens are never persisted.
- Removing references in Python is not secure RAM zeroization. Host administrators, memory dumps or OS swap may still expose process memory. The app never deliberately writes plaintext credentials or session tokens to disk; protect the host as well.

`COOKIE_SECURE=true` is the default. Live sign-in rejects plain HTTP and the session cookie is Secure. Configure HTTPS termination at your reverse proxy and set `FORWARDED_ALLOW_IPS` to that proxy's actual source IP so Uvicorn trusts its HTTPS forwarding header. Do not use `*` on an exposed port. `COOKIE_SECURE=false` is an explicit option for isolated local HTTP development; it removes transport protection and should not be used for normal live deployment.

Login requests have CSRF protection and a limit of eight attempts per minute per controller. Dashboard mutations require a session and CSRF token. Login errors do not echo credentials. Vendor library logging is suppressed, and the app does not log login bodies. Do not enable request-body logging in a proxy or debugging tool.

An account with exactly one heat pump is selected automatically. For multiple pumps, set `EMERALD_DEVICE_ID` to the intended device UUID; the app will not guess. Use one replica and one Uvicorn worker. A file lock prevents two processes sharing a data directory.

If upgrading from v0.1, remove the old credential variables from your `.env`/TrueNAS configuration. Existing external configuration backups are outside the app's control and may still contain the old values.

The adapter uses the community [emerald-hws](https://github.com/ross-w/emerald_hws_py) library: `turnOn`, `setBoostMode`, `setNormalMode`, and a requested device-state update. Your EE-HWS-A1-320 still needs a live compatibility test. **A broker acknowledgement alone is not treated as device confirmation.** The dashboard reports the latest available device state; it does not measure whether the compressor is physically running.

Start with the schedule paused. Use **Boost now**, check the official Emerald app for Boost, then use **End boost** and check for Standard. Enable your weekly schedule once both commands work. If other Emerald schedules or Smart Actions control the same device, reconcile those settings so the controllers do not conflict.

## Optional encrypted credential saving

The login screen's **Remember login on this server** checkbox controls saving. It is off initially. Once saved, the checkbox reflects that choice on later logins. To disable it, use **Disable saving & delete saved login** on the dashboard, or uncheck Remember login during a successful sign-in.

Disabling saving deletes the encrypted record from SQLite, stops future automatic re-login, and keeps the current in-memory token/session active. It works even when the key is missing or the Emerald session has expired, as long as the dashboard session is valid. The app does not retain a decrypted credential cache. The separate key file remains available for a future opt-in.

Deletion uses SQLite secure-delete and requests WAL truncation. **It cannot remove copies from old TrueNAS snapshots, external backups, storage-device remnants or an attacker's copy.** Apply your backup retention policy to those copies. This is not a promise of forensic erasure.

Encryption uses `cryptography` Fernet authenticated encryption. The random key must be a separate regular file outside the app's data directory, readable by the app, with no group/world permission bits. Missing, invalid, unreadable, permissive or colocated keys disable saving; normal session-only login still works. No key is hardcoded or auto-created beside the database.

For a host with uv:

```sh
uv run python -m app.keygen /path/outside-app-data/emerald.key
```

The helper creates an owner-read-only key without printing it and refuses to overwrite an existing file. For a Docker deployment, grant UID 10001 read access to that file; do not make it world-readable.

The optional `compose.credentials.yaml` overlay mounts the key read-only as a Compose secret:

```sh
export CREDENTIAL_KEY_PATH=/absolute/path/to/emerald.key
docker compose -f compose.yaml -f compose.credentials.yaml up -d --build
```

The source file is **not encrypted by Compose**. Restrict its host permissions and use TrueNAS encrypted storage where appropriate. Protect key backups separately from database backups. A full host or running-container compromise can expose both key and decrypted data. Losing the key requires entering the login again and saving under a new key; existing ciphertext cannot be recovered without its key.

Automatic sign-in runs under the same lock as control and deletion, so disabling saving cannot race with a background process that re-saves it. Confirmed authentication rejection before a device command triggers one automatic re-login and one command retry. The boost deadline is rechecked before sending Boost, so a slow login cannot start it after the window ends. Unknown command outcomes are not blindly retried.

Temporary login/network failures back off for five minutes. A rejected saved password or undecryptable record stops automatic attempts until a manual login or saving is disabled. All resulting activity messages omit credentials. Manual login resets the backoff.

## TrueNAS 25.x

Use **Apps → Discover Apps → ⋮ → Install via YAML**. The exact menu depends on your TrueNAS release. If Apps has not been configured, select its storage pool first. [TrueNAS documentation](https://www.truenas.com/docs/scale/25.10/scaleuireference/apps/installcustomappscreens/).

1. Name the app `emerald-boost` and paste [compose.truenas.yaml](compose.truenas.yaml) into Custom Config.
2. Set `FORWARDED_ALLOW_IPS` to your HTTPS reverse proxy's actual source IP. The proxy should forward to `http://TRUENAS_IP:8080` and send `X-Forwarded-Proto: https`. Keep `COOKIE_SECURE: "true"`. Change port `8080` in the YAML if needed. HTTPS setup is separate from installing this app; opening the HTTP address directly will not permit login.
3. Save. TrueNAS downloads the published Linux image; both Intel/AMD and ARM64 are supported. No registry login is needed for the public image. The `prepare-key` service runs once per deployment and exits successfully; this is expected. It creates a private key in its own persistent volume only when missing, and validates an existing key without replacing it.
4. Open your HTTPS address and sign in with the account used by the official Emerald app. **Remember login** is initially unchecked. Check it if you want automatic sign-in after updates, restarts or token expiry. Credentials remain encrypted in the database; the key is a separate file mounted read-only in the controller.
5. Leave the schedule paused until Boost and return-to-Standard are confirmed against the official app. Stop any controller running on your Mac before enabling the TrueNAS schedule.

The template creates two persistent Docker volumes: `boost-data` for the database and `credential-key` for the private encryption-key file. The app runs as UID/GID 10001. Docker initializes ownership automatically. No TrueNAS source dataset, manual key command, Git installation or image build is needed.

These volumes survive ordinary image updates and container recreation. Back up both, protecting the key backup separately; do not delete their volumes when editing or updating the app. Removing an app and its storage can delete its settings and saved credentials. Losing the key prevents decrypting an existing saved login. Back up the database while the controller is stopped, or use SQLite's backup API. Advanced users can replace the data volume with an existing host dataset; grant UID/GID 10001 write access first. Preserve the same volume or host path when migrating an existing installation.

### Updating and rolling back

New releases publish `ghcr.io/thisarattr/emerald-heat-pump:stable` and a numbered tag such as `0.3.1`. `stable` follows the most recently published release. The separate `edge` tag follows successful builds from `main` and is intended for development.

1. Back up the persistent data and key before upgrading. Finish any active boost first when possible.
2. In **Apps → Installed → emerald-boost → ⋮ → Update**, apply an available image update. If TrueNAS does not offer it, use **Manage Container Images → Pull Image** for `ghcr.io/thisarattr/emerald-heat-pump:stable`, then redeploy the custom app through Edit/Update. Both template services use `pull_policy: always` to request the current image on deployment. [TrueNAS update documentation](https://apps.truenas.com/managing-apps/managing-installed-apps/).
3. Settings and encrypted saved credentials stay in the existing volumes. Memory-only tokens are lost during a restart. With Remember login enabled, the controller can sign in automatically; otherwise, sign in again. An explicit Sign out still pauses automatic sign-in until a manual login.

You do not repeat installation, create new storage or regenerate the key for routine updates. YAML changes are only needed when a release introduces new configuration.

To pin a version or roll back, replace **both** image references in the YAML with a compatible numbered tag, such as `ghcr.io/thisarattr/emerald-heat-pump:0.3.1`, then save. Check release notes for database compatibility before downgrading. Keep the same app name and volumes.

### Publishing releases

[The Container workflow](.github/workflows/container.yaml) runs lint and tests before publishing images to GitHub Container Registry using its built-in `GITHUB_TOKEN`. Actions are pinned to commit SHAs. Pull requests run checks without registry write permissions. Pushes to `main` publish `edge`; version tags publish `stable`, the full version and a commit tag for Linux AMD64 and ARM64. A published image also receives a demo-mode health check.

For a release, update the version in `pyproject.toml` and the local development image tag in `compose.yaml`, run `uv lock` and the checks, commit and push, then tag that commit:

```sh
git tag v0.3.1
git push origin v0.3.1
```

Use the new version for subsequent releases. The workflow rejects a release tag that does not match `pyproject.toml`. On the first publication, the repository owner must change the GitHub package visibility to **Public** in [package settings](https://github.com/users/thisarattr/packages/container/emerald-heat-pump/settings); GitHub defaults newly created packages to private. This is a one-time registry setting, not a setting on each TrueNAS installation. Keep the package public for anonymous installation and updates.

For local development, `compose.yaml` still builds from source and uses demo mode by default. The TrueNAS template can also be used on another Docker Compose host with the same HTTPS setup.

## Scheduling and recovery

- Times use `Australia/Melbourne`, including daylight saving. Duration is elapsed time, including across midnight or a clock change.
- One schedule, selected weekdays, and a duration of 5–720 minutes. The initial schedule is paused at 11:00 for 60 minutes.
- A selected day starts at most one scheduled boost, even after schedule edits or a restart. Manual starts are separate and cannot overlap an existing window.
- The scheduler checks every five seconds and permits up to 90 seconds of start delay. Later starts are skipped and recorded. Restarting hours later never starts an unexpected boost.
- A nonexistent spring daylight-saving time is skipped. A repeated autumn time runs only on its first occurrence.
- The deadline is persisted **before** issuing any device command. An uncertain boost command is not retried; the app still requests Standard at the saved deadline.
- After a restart and a fresh login, an expired boost window is returned to Standard without starting boost again. Pausing or editing the schedule does not alter an existing deadline. “End boost” requests Standard immediately.
- Failure to confirm Standard keeps the window pending and retries every 60 seconds. It blocks new boosts until recovery. The UI and activity log show this condition.
- The pump may finish its own one-shot boost before the app deadline. The app does not repeatedly re-enable it during that window.
- Exact expiry cannot be guaranteed when the server, network or Emerald cloud is unavailable. Inspect the official app if a command is unconfirmed. Keep the appliance powered and preserve its built-in hygiene/protection logic; this app never switches mains power or changes temperature settings.

## Development checks

```sh
uv run pytest
uv run ruff check .
```

Tests cover the real adapter's command mapping with a fake transport, weekly scheduling, duplicate prevention, daylight saving, missed starts, restart recovery, connection failures, persistent settings, authentication, CSRF and single-controller ownership. They do not prove live device compatibility.

When saving is disabled or its record is deleted, passwords are unavailable for background re-authentication and automation waits for manual login after token expiry or a restart.

Dependencies are locked in `uv.lock`; `requirements.txt` is its production export used by Docker. To update dependencies, run `uv lock --upgrade`, `uv export --no-dev --no-hashes --no-emit-project -o requirements.txt`, then rerun the checks.
