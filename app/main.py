import asyncio
import fcntl
import hmac
import re
import secrets
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.auth import LoginRequired, Sessions
from app.config import Config
from app.heater import DemoHeater, EmeraldHeater, Heater
from app.scheduler import Scheduler, next_start
from app.storage import Store
from app.vault import CredentialVault, VaultError

ROOT = Path(__file__).parent
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
SESSION_COOKIE = "boost_session"
LOGIN_COOKIE = "boost_login_csrf"


def create_app(config: Config | None = None, heater: Heater | None = None, run_loop=True):
    config = config or Config.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config.data_dir.mkdir(parents=True, exist_ok=True)
        lock_file = (config.data_dir / "controller.lock").open("a")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            raise RuntimeError(
                "Another controller is using this data directory. Use one worker."
            ) from None
        task = None
        controller = heater or (DemoHeater() if config.driver == "demo" else EmeraldHeater(config))
        try:
            filename = "boost-demo.sqlite3" if config.driver == "demo" else "boost.sqlite3"
            store = Store(config.data_dir / filename)
            if isinstance(controller, EmeraldHeater):
                controller.device_id = store.bound_device()
            vault = CredentialVault(store, config.credential_key_file)
            scheduler = Scheduler(store, controller, config.timezone, vault)
            if active := store.active():
                if active["status"] == "starting":
                    store.update_run(
                        active["id"],
                        "uncertain",
                        "Restarted during start; boost will not be resent.",
                    )
                store.event(
                    datetime.now(UTC),
                    "Controller restarted. Preserved boost deadline; boost will not be resent.",
                )
            app.state.store = store
            app.state.scheduler = scheduler
            app.state.vault = vault
            app.state.csrf = secrets.token_urlsafe(32)
            app.state.sessions = Sessions()
            if run_loop:
                task = asyncio.create_task(scheduler.loop())
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            if hasattr(app.state, "sessions"):
                app.state.sessions.clear()
            await asyncio.to_thread(controller.close)
            lock_file.close()

    app = FastAPI(
        title="Emerald Boost", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    templates = Jinja2Templates(directory=ROOT / "templates")

    def authenticate(request: Request):
        if config.driver == "demo":
            return
        if app.state.sessions.valid(request.cookies.get(SESSION_COOKIE, "")):
            return
        raise LoginRequired()

    auth = [Depends(authenticate)]

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, error):
        if request.url.path == "/":
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"detail": "Sign in required"}, status_code=401)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "same-origin",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' data:; frame-ancestors 'none'; "
                    "form-action 'self'; base-uri 'self'"
                ),
                "Cache-Control": "no-store",
            }
        )
        return response

    def snapshot():
        scheduler = app.state.scheduler
        now = datetime.now(UTC)
        schedule = app.state.store.schedule()
        upcoming = next_start(schedule, now, scheduler.zone, app.state.store)
        return {
            "demo": config.driver == "demo",
            "timezone": config.timezone,
            "local_time": now.astimezone(scheduler.zone).strftime("%a %d %b · %H:%M"),
            "schedule": schedule,
            "next_start": upcoming.isoformat() if upcoming else None,
            "active": app.state.store.active(),
            "heater": scheduler.heater.status(),
            "history": app.state.store.history(),
            "error": scheduler.last_error,
            "saved_login": app.state.vault.saved,
            "auth_error": scheduler.auth_error,
            "cloud_signed_in": scheduler.heater.logged_in,
        }

    def login_page(request: Request, error="", status_code=200):
        csrf = secrets.token_urlsafe(32)
        response = templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "csrf": csrf,
                "error": error,
                "demo": config.driver == "demo",
                "transport_ok": not config.secure_cookies or request.url.scheme == "https",
                "saving_available": app.state.vault.available,
                "saved_login": app.state.vault.saved,
            },
            status_code=status_code,
        )
        response.set_cookie(
            LOGIN_COOKIE,
            csrf,
            httponly=True,
            secure=config.secure_cookies,
            samesite="strict",
            path="/login",
            max_age=600,
        )
        return response

    @app.get("/login", response_class=HTMLResponse)
    async def login_screen(request: Request):
        if config.driver == "demo":
            return RedirectResponse("/", status_code=303)
        return login_page(request)

    @app.post("/login")
    async def login(request: Request):
        if config.driver == "demo":
            return RedirectResponse("/", status_code=303)
        if config.secure_cookies and request.url.scheme != "https":
            return login_page(request, "Use HTTPS before entering Emerald credentials.", 400)
        form = await request.form(max_files=0, max_fields=4, max_part_size=4096)
        csrf = str(form.get("csrf", ""))
        cookie = request.cookies.get(LOGIN_COOKIE, "")
        if not csrf or not cookie or not hmac.compare_digest(csrf.encode(), cookie.encode()):
            raise HTTPException(403, "Login page expired. Reload and try again.")
        if not app.state.sessions.allow_attempt():
            return login_page(request, "Too many login attempts. Wait one minute.", 429)
        email = str(form.get("email", ""))
        password = str(form.get("password", ""))
        remember = form.get("remember") == "on"
        error = ""
        try:
            if not email or not password or len(email) > 320 or len(password) > 1024:
                error = "Enter your Emerald email and password."
            else:
                scheduler = app.state.scheduler
                async with scheduler.lock:
                    if remember and not app.state.vault.available:
                        raise VaultError(
                            "Encrypted storage is unavailable. "
                            "Uncheck Remember login or configure its key."
                        )
                    await scheduler._call(scheduler.heater.login, email, password)
                    if remember:
                        app.state.vault.save(email, password)
                    else:
                        app.state.vault.forget()
                    app.state.store.pause_auto_login(False)
                    scheduler.reset_auth_retry()
                    device_id = getattr(scheduler.heater, "device_id", None)
                    if device_id:
                        app.state.store.bind_device(device_id)
                    app.state.store.event(datetime.now(UTC), "Signed in. Automation resumed.")
        except VaultError as problem:
            error = str(problem)
        except Exception:
            error = "Sign-in failed. Check your credentials, device selection and Emerald service."
        finally:
            email = password = ""
            request._form = None
            del form
        if error:
            return login_page(request, error, 400)
        app.state.sessions.clear()
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            app.state.sessions.issue(),
            httponly=True,
            secure=config.secure_cookies,
            samesite="strict",
            path="/",
        )
        response.delete_cookie(LOGIN_COOKIE, path="/login")
        return response

    @app.get("/", response_class=HTMLResponse, dependencies=auth)
    async def home(request: Request):
        state = snapshot()
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "state": state,
                "csrf": app.state.csrf,
                "days": DAYS,
                "message": request.query_params.get("message", ""),
            },
        )

    @app.get("/api/status", dependencies=auth)
    async def status():
        return snapshot()

    @app.get("/healthz")
    async def health():
        scheduler = app.state.scheduler
        if scheduler.last_error or (
            run_loop
            and (
                scheduler.last_tick is None
                or (datetime.now(UTC) - scheduler.last_tick).total_seconds() > 120
            )
        ):
            raise HTTPException(503, "Scheduler is not responding")
        return {"status": "ok"}

    async def checked_form(request: Request):
        form = await request.form(max_fields=20, max_files=0)
        if not hmac.compare_digest(str(form.get("csrf", "")).encode(), app.state.csrf.encode()):
            raise HTTPException(403, "Page expired. Reload before making changes.")
        return form

    def redirect(message):
        return RedirectResponse("/?" + urlencode({"message": message}), status_code=303)

    @app.post("/schedule", dependencies=auth)
    async def save_schedule(request: Request):
        form = await checked_form(request)
        start = str(form.get("start_time", ""))
        try:
            duration = int(str(form.get("duration", "")))
            days = sorted(set(int(str(day)) for day in form.getlist("days")))
        except ValueError:
            return redirect("Choose a valid duration and days.")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", start):
            return redirect("Choose a valid start time.")
        if not 5 <= duration <= 720 or not days or not all(0 <= day <= 6 for day in days):
            return redirect("Choose at least one day and a duration between 5 and 720 minutes.")
        async with app.state.scheduler.lock:
            app.state.store.save_schedule(form.get("enabled") == "on", start, duration, days)
            app.state.store.event(
                datetime.now(UTC), "Schedule saved. Active boost deadlines are unchanged."
            )
        return redirect("Schedule saved.")

    @app.post("/boost", dependencies=auth)
    async def boost(request: Request):
        await checked_form(request)
        try:
            await app.state.scheduler.manual_start()
        except ValueError as error:
            return redirect(str(error))
        return redirect("Boost request finished. See device status and activity below.")

    @app.post("/stop", dependencies=auth)
    async def stop(request: Request):
        await checked_form(request)
        await app.state.scheduler.stop()
        return redirect("Return to Standard requested. See activity for confirmation.")

    @app.post("/logout", dependencies=auth)
    async def logout(request: Request):
        await checked_form(request)
        scheduler = app.state.scheduler
        async with scheduler.lock:
            if active := app.state.store.active():
                await scheduler._stop(active, datetime.now(UTC))
                if app.state.store.active():
                    return redirect("Sign-out paused: Standard not confirmed. Check Emerald app.")
            await scheduler._call(scheduler.heater.close)
            app.state.store.pause_auto_login(True)
            app.state.sessions.clear()
            app.state.store.event(datetime.now(UTC), "Signed out. Automation paused until sign-in.")
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(LOGIN_COOKIE, path="/login")
        return response

    @app.post("/credentials/forget", dependencies=auth)
    async def forget_credentials(request: Request):
        await checked_form(request)
        async with app.state.scheduler.lock:
            app.state.vault.forget()
            app.state.scheduler.reset_auth_retry()
            app.state.store.event(
                datetime.now(UTC), "Credential saving disabled. Saved login deleted."
            )
        return redirect(
            "Saving disabled and saved login deleted. Your current session stays active."
        )

    return app


app = create_app()
