# Emerald Boost

A small web app that starts an Emerald hot-water heat pump in boost mode on a weekly schedule, then requests Standard mode after the selected duration. Version one has no solar, inverter, battery or weather integration.

Python 3.12, FastAPI, server-rendered HTML, a small amount of JavaScript, and SQLite. One container and one worker. No external database or task queue.

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

Use **Apps → Discover Apps → Install via YAML** on versions supporting Docker-based custom apps. The exact menu depends on your TrueNAS release. The ready-to-edit template is `compose.truenas.yaml`.

1. Put this project in a source dataset, for example `/mnt/tank/apps/emerald-boost/source`.
2. Create a separate persistent dataset at `/mnt/tank/apps/emerald-boost/data`. Grant UID/GID 10001 write access.
3. Replace the two example paths in the template. Leave `HEATER_DRIVER: demo` initially. No credentials belong in the YAML. For optional saving, follow the key-mount instructions below.
4. Paste the YAML into the custom app installer. It builds the image from the source directory. If your version cannot build there, run `docker compose -f compose.truenas.yaml build` in the TrueNAS shell first; then remove `build:` from the YAML and deploy the local image.
5. Open `http://YOUR_TRUENAS_IP:8080`. Change the published port if another app uses it.
6. Configure HTTPS and the trusted proxy IP, change the driver to `emerald`, and redeploy. Sign in through the web UI. Demo and live use separate databases, so the first live start has its schedule paused and no demo runs to recover. Then run the live compatibility check above.

To enable optional saving on TrueNAS, create a private secrets dataset outside the app data dataset, for example `/mnt/tank/app-secrets`. After building the image, generate the key without printing it:

```sh
docker run --rm --user 0:0 -v /mnt/tank/app-secrets:/keys emerald-boost:0.3.0 python -m app.keygen /keys/emerald-boost.key
chown 10001:10001 /mnt/tank/app-secrets/emerald-boost.key
chmod 400 /mnt/tank/app-secrets/emerald-boost.key
```

In your custom app YAML, add the equivalent of `compose.credentials.yaml`: set `CREDENTIAL_KEY_FILE` to `/run/secrets/emerald_key`, grant the service the `emerald_key` secret, and point its top-level `file:` to `/mnt/tank/app-secrets/emerald-boost.key`. This file must exist before deployment. Keep secret contents out of YAML. Alternatively combine both files with `CREDENTIAL_KEY_PATH` set to that absolute source path. Then sign in and explicitly check Remember login.

For an ordinary Docker host, use `compose.yaml` and `.env` instead. Both deployments keep `/data` outside the container, so updates retain schedules and active deadlines. Back up the data dataset while the container is stopped, or use SQLite's backup API for a consistent live backup.

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
