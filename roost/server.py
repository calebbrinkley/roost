"""Owner-only Roost HTTP service. No dashboard credential reaches the browser."""

import asyncio
import json
import os
import platform
import re
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import aiohttp
from aiohttp import web
from yarl import URL

from . import __version__

from .auth import Sessions, verify_password
from .hermes import Hermes, HermesError
from .passkeys import PasskeyError, Passkeys

STATIC = Path(__file__).with_name("static")
COOKIE = "roost_session"
ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
JOB_FIELDS = {"name", "prompt", "schedule", "deliver"}
# Accent presets; the browser holds each one's light and dark colours.
ACCENTS = ("forest", "ocean", "violet", "rose", "amber", "slate")
AGENT_NAME_CACHE_SECONDS = 300
# systemd restarts the service after a non-zero exit (Restart=on-failure); 75 is EX_TEMPFAIL.
RESTART_EXIT = 75
SOUL_LIMIT = 16000
# "" follows each device's own time zone; otherwise an IANA name such as "America/New_York".
TIME_ZONE = re.compile(r"^(UTC|[A-Za-z]+(/[A-Za-z0-9_+-]+){1,2})$")
# Hermes status fields shown in About; the rest (paths, PIDs, auth) stays on the server.
HERMES_STATUS_FIELDS = ("version", "release_date", "gateway_running", "gateway_state", "active_sessions")


def bad(message: str, status: int = 400) -> web.HTTPException:
    return web.HTTPBadRequest(text=message) if status == 400 else web.HTTPForbidden(text=message)


def body_text(value, field: str, limit: int, *, required: bool = True) -> str:
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise bad(f"{field} is required and must be at most {limit} characters.")
    return value.strip()


async def json_body(request: web.Request) -> dict:
    if request.content_type != "application/json":
        raise bad("Expected JSON.")
    try:
        value = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise bad("Invalid JSON.") from None
    if not isinstance(value, dict):
        raise bad("Expected a JSON object.")
    return value


def item_id(request: web.Request, name: str, *, encode: bool = True) -> str:
    value = request.match_info[name]
    if not ID.fullmatch(value) or value in {".", ".."}:
        raise bad("Invalid identifier.")
    return quote(value, safe="") if encode else value


def secured_cookie(response: web.StreamResponse, request: web.Request, token: str) -> None:
    response.set_cookie(COOKIE, token, max_age=30 * 24 * 60 * 60, httponly=True,
                        secure=request.app["secure_cookie"], samesite="Strict", path="/")


@web.middleware
async def guard(request: web.Request, handler):
    try:
        if request.path.startswith("/api/"):
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                if request.headers.get("Origin") != request.app["origin"] or request.headers.get("X-Roost-Request") != "1":
                    raise bad("Request origin was not accepted.", 403)
            if request.path not in {"/api/login", "/api/me", "/api/health", "/api/passkeys/login/options", "/api/passkeys/login"}:
                if not request.app["sessions"].valid(request.cookies.get(COOKIE, "")):
                    raise web.HTTPUnauthorized(text="Sign in to Roost.")
        response = await handler(request)
    except HermesError as exc:
        response = web.json_response({"error": exc.message}, status=exc.status)
    except web.HTTPException as exc:
        response = web.json_response({"error": exc.text or exc.reason}, status=exc.status)
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; form-action 'self'; frame-ancestors 'none'"
    return response


LOOPBACK = {"127.0.0.1", "::1"}
DEVICE_FAILURES, ALL_FAILURES, FAILURE_WINDOW = 5, 20, 300


def client_address(request: web.Request) -> str:
    """The signing-in device. Behind Tailscale Serve every request comes from loopback, and Serve sets
    (not appends) X-Forwarded-For to the device's tailnet address, so that header names the device.
    In a container the proxy is not loopback, so ROOST_TRUST_FORWARDED=1 trusts the header from any
    peer; set it only when the proxy is the sole way in."""
    remote = request.remote or "unknown"
    forwarded = request.headers.get("X-Forwarded-For", "").strip()
    trusted = remote in LOOPBACK or request.app["trust_forwarded"]
    if trusted and forwarded and "," not in forwarded and len(forwarded) <= 64:
        return forwarded
    return remote


def reserve_attempt(request: web.Request) -> str:
    """Count a sign-in attempt (password or passkey) against the device and overall limits."""
    address = client_address(request)
    now = time.monotonic()
    failures = request.app["login_attempts"]
    for key in list(failures):
        failures[key] = [t for t in failures[key] if now - t < FAILURE_WINDOW]
        if not failures[key]:
            del failures[key]
    # Reserve capacity before awaiting body/file reads or password hashing. Concurrent requests
    # must share both limits, and only bounded password checks may run in worker threads.
    inflight = request.app["login_inflight"]
    if (len(failures.get(address, [])) + inflight.get(address, 0) >= DEVICE_FAILURES or
            sum(map(len, failures.values())) + sum(inflight.values()) >= ALL_FAILURES):
        raise web.HTTPTooManyRequests(text="Too many attempts. Try again in a few minutes.")
    inflight[address] = inflight.get(address, 0) + 1
    return address


def release_attempt(request: web.Request, address: str) -> None:
    inflight = request.app["login_inflight"]
    inflight[address] -= 1
    if not inflight[address]:
        del inflight[address]


def signed_in_response(request: web.Request, address: str) -> web.Response:
    request.app["login_attempts"].pop(address, None)
    response = web.json_response({"signed_in": True, "demo": request.app["demo"]})
    secured_cookie(response, request, request.app["sessions"].issue())
    return response


async def login(request: web.Request):
    address = reserve_attempt(request)
    failures = request.app["login_attempts"]
    try:
        data = await json_body(request)
        password = data.get("password")
        if not isinstance(password, str) or len(password) > 1024:
            raise bad("Password is required.")
        # Read on every attempt so `roost set-password` takes effect without a restart.
        try:
            encoded = await asyncio.to_thread(request.app["password_file"].read_text, encoding="utf-8")
        except (OSError, UnicodeError):
            raise web.HTTPServiceUnavailable(text="Roost has no password yet. Run `python -m roost set-password`.") from None
        if not await asyncio.to_thread(verify_password, password, encoded):
            failures.setdefault(address, []).append(time.monotonic())
            raise web.HTTPUnauthorized(text="Incorrect password.")
        return signed_in_response(request, address)
    finally:
        release_attempt(request, address)


async def passkey_login_options(request: web.Request):
    return web.json_response(request.app["passkeys"].authentication_options())


async def passkey_login(request: web.Request):
    address = reserve_attempt(request)
    try:
        data = await json_body(request)
        try:
            # Runs here, not in a worker thread: the sessions database belongs to this thread,
            # and checking one signature takes well under a millisecond.
            request.app["passkeys"].authenticate(data)
        except PasskeyError as error:
            request.app["login_attempts"].setdefault(address, []).append(time.monotonic())
            raise web.HTTPUnauthorized(text=str(error)) from None
        return signed_in_response(request, address)
    finally:
        release_attempt(request, address)


def session_key(request: web.Request) -> str:
    """Binds a registration challenge to the session that asked for it."""
    return Sessions._hash(request.cookies.get(COOKIE, ""))


async def list_passkeys(request: web.Request):
    return web.json_response({"passkeys": request.app["passkeys"].list()})


async def passkey_registration_options(request: web.Request):
    return web.json_response(request.app["passkeys"].registration_options(session_key(request)))


async def add_passkey(request: web.Request):
    data = await json_body(request)
    name = clean_name(data.pop("name", ""))
    if not name:
        raise bad("Name the passkey (1 to 40 characters), for example the device it's on.")
    try:
        added = request.app["passkeys"].register(data, session_key(request), name)
    except PasskeyError as error:
        raise bad(str(error)) from None
    return web.json_response(added, status=201)


async def remove_passkey(request: web.Request):
    credential_id = request.match_info["pid"]
    data = await json_body(request)
    if set(data) != {"confirm"} or data["confirm"] is not True:
        raise bad("Confirm removing the passkey.")
    if len(credential_id) > 1400 or not request.app["passkeys"].remove(credential_id):
        raise web.HTTPNotFound(text="That passkey isn't set up for Roost.")
    return web.json_response({"ok": True})


async def logout(request: web.Request):
    request.app["sessions"].revoke(request.cookies.get(COOKIE, ""))
    response = web.json_response({"signed_in": False})
    response.del_cookie(COOKIE, path="/")
    return response


async def me(request: web.Request):
    signed_in = request.app["sessions"].valid(request.cookies.get(COOKIE, ""))
    agent = await agent_name(request.app, ask_hermes=signed_in)
    settings = load_settings(request.app)
    return web.json_response({"signed_in": signed_in, "demo": request.app["demo"], "agent": agent,
                              "accent": settings["accent"] if signed_in else ACCENTS[0],
                              "time_zone": settings["time_zone"] if signed_in else "",
                              # Whether the sign-in page offers passkeys; not which ones exist.
                              "passkeys": request.app["passkeys"].count() > 0})


def clean_name(value) -> str:
    return value.strip() if isinstance(value, str) and 0 < len(value.strip()) <= 40 and value.strip().isprintable() else ""


def load_settings(app: web.Application) -> dict:
    """Roost's own preferences, shared by every signed-in device."""
    try:
        stored = json.loads(app["settings_file"].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    zone = stored.get("time_zone")
    return {"agent_name": clean_name(stored.get("agent_name")),
            "accent": stored.get("accent") if stored.get("accent") in ACCENTS else ACCENTS[0],
            "time_zone": zone if isinstance(zone, str) and len(zone) <= 64 and TIME_ZONE.fullmatch(zone) else ""}


def save_settings(app: web.Application, settings: dict) -> None:
    path = app["settings_file"]
    temporary = path.with_name(path.name + ".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(settings, stream)
    os.replace(temporary, path)


async def hermes_profile(app: web.Application) -> str:
    """The profile this dashboard serves; its name is a path segment, so it must be a plain identifier."""
    active = await app["hermes"].request("GET", "/api/profiles/active")
    current = active.get("current") if isinstance(active, dict) else None
    return current if isinstance(current, str) and ID.fullmatch(current) and current not in {".", ".."} else "default"


async def hermes_agent_name(app: web.Application, *, ask: bool) -> str:
    """The name Hermes shows for its profile ("" when it has none), cached for a few minutes."""
    cached, fetched = app["hermes_agent_name"]
    # `fetched` is None until the first lookup: monotonic time can be small just after boot.
    if ask and (fetched is None or time.monotonic() - fetched > AGENT_NAME_CACHE_SECONDS):
        try:
            current = await hermes_profile(app)
            listed = await app["hermes"].request("GET", "/api/profiles")
            profiles = listed.get("profiles") if isinstance(listed, dict) else None
            profile = next((item for item in profiles or [] if isinstance(item, dict) and item.get("name") == current), {})
            cached = clean_name(profile.get("display_name")) or clean_name(profile.get("bot_title"))
            app["hermes_agent_name"] = (cached, time.monotonic())
        except HermesError:
            pass
    return cached


async def agent_name(app: web.Application, *, ask_hermes: bool) -> str:
    """Roost's own setting wins; then the name Hermes shows for its profile; then "Agent"."""
    return load_settings(app)["agent_name"] or await hermes_agent_name(app, ask=ask_hermes) or "Agent"


async def settings_view(app: web.Application) -> dict:
    return {**load_settings(app), "hermes_agent_name": await hermes_agent_name(app, ask=True), "accents": list(ACCENTS)}


async def get_settings(request: web.Request):
    return web.json_response(await settings_view(request.app))


async def update_settings(request: web.Request):
    data = await json_body(request)
    if not data or set(data) - {"agent_name", "accent", "time_zone"}:
        raise bad("Unsupported setting.")
    settings = load_settings(request.app)
    if "agent_name" in data:
        value = data["agent_name"]
        if not isinstance(value, str) or (value.strip() and not clean_name(value)):
            raise bad("The name must be 1 to 40 printable characters, or empty to use Hermes's name.")
        settings["agent_name"] = value.strip()
    if "accent" in data:
        if data["accent"] not in ACCENTS:
            raise bad("Choose one of the accent colours.")
        settings["accent"] = data["accent"]
    if "time_zone" in data:
        zone = data["time_zone"]
        if not isinstance(zone, str) or len(zone) > 64 or (zone and not TIME_ZONE.fullmatch(zone)):
            raise bad("Choose a time zone from the list, or the device's own.")
        settings["time_zone"] = zone
    save_settings(request.app, settings)
    return web.json_response(await settings_view(request.app))


async def get_soul(request: web.Request):
    profile = await hermes_profile(request.app)
    result = await request.app["hermes"].request("GET", f"/api/profiles/{quote(profile, safe='')}/soul")
    content = result.get("content") if isinstance(result, dict) else None
    return web.json_response({"content": content if isinstance(content, str) else ""})


async def update_soul(request: web.Request):
    data = await json_body(request)
    if set(data) != {"content"} or not isinstance(data["content"], str) or len(data["content"]) > SOUL_LIMIT:
        raise bad(f"Instructions must be at most {SOUL_LIMIT} characters.")
    profile = await hermes_profile(request.app)
    await request.app["hermes"].request("PUT", f"/api/profiles/{quote(profile, safe='')}/soul", {"content": data["content"]})
    return web.json_response({"ok": True})


def confirmed_everything(data: dict) -> None:
    if set(data) != {"confirm"} or data["confirm"] != "DELETE":
        raise bad("Type DELETE to confirm.")


async def delete_all_conversations(request: web.Request):
    """Every conversation the Messages list shows. Cron run records stay with their jobs."""
    confirmed_everything(await json_body(request))
    hermes, deleted, attempted = request.app["hermes"], 0, set()
    while True:
        page = await hermes.request("GET", "/api/sessions?limit=100&order=recent&exclude_sources=cron&offset=0")
        ids = [conversation_key(item) for item in (page.get("sessions") or [] if isinstance(page, dict) else [])]
        ids = [sid for sid in ids if sid and sid not in attempted]
        # Hermes kept rows it said it deleted: stop instead of asking again forever.
        if not ids:
            break
        for sid in ids:
            attempted.add(sid)
            try:
                await hermes.request("DELETE", f"/api/sessions/{quote(sid, safe='')}")
            except HermesError as exc:
                raise HermesError(exc.status, f"Deleted {deleted} conversations, then Hermes refused the next one.") from None
            deleted += 1
    return web.json_response({"deleted": deleted})


def conversation_key(item) -> str:
    value = item.get("session_id") or item.get("id") if isinstance(item, dict) else None
    return value if isinstance(value, str) and ID.fullmatch(value) and value not in {".", ".."} else ""


async def delete_all_jobs(request: web.Request):
    confirmed_everything(await json_body(request))
    hermes, deleted = request.app["hermes"], 0
    listed = await hermes.request("GET", "/api/cron/jobs")
    rows = listed if isinstance(listed, list) else (listed.get("jobs") or [] if isinstance(listed, dict) else [])
    for job in rows:
        jid = conversation_key({"id": job.get("id") or job.get("job_id")}) if isinstance(job, dict) else ""
        if not jid:
            continue
        try:
            await hermes.request("DELETE", f"/api/cron/jobs/{quote(jid, safe='')}")
        except HermesError as exc:
            raise HermesError(exc.status, f"Deleted {deleted} jobs, then Hermes refused the next one.") from None
        deleted += 1
    return web.json_response({"deleted": deleted})


def read_number(path: str) -> int | None:
    try:
        value = open(path, encoding="ascii").read().strip()
    except OSError:
        return None
    return int(value) if value.isdigit() else None


def directory_bytes(path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def process_stats(app: web.Application) -> dict:
    """Roost's own footprint. Memory is the container's cgroup when it is visible, else this process."""
    now, used = time.monotonic(), time.process_time()
    last_now, last_used = app["cpu_sample"]
    app["cpu_sample"] = (now, used)
    rss = None
    try:
        for line in open("/proc/self/status", encoding="ascii"):
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    disk = os.statvfs(app["state_dir"])
    return {
        "uptime_seconds": int(now - app["started"]),
        "cpu_percent": round(100 * (used - last_used) / (now - last_now), 1) if now > last_now else 0.0,
        "memory_bytes": read_number("/sys/fs/cgroup/memory.current") or rss,
        "memory_limit_bytes": read_number("/sys/fs/cgroup/memory.max"),
        "process_memory_bytes": rss,
        # Roost's own footprint: its code and assets, plus its data (sessions and settings).
        "app_bytes": directory_bytes(Path(__file__).parent),
        "state_bytes": directory_bytes(app["state_dir"]),
        # The whole volume the data lives on, which other services share; only its free space is shown.
        "disk_total_bytes": disk.f_frsize * disk.f_blocks,
        "disk_free_bytes": disk.f_frsize * disk.f_bavail,
    }


async def about(request: web.Request):
    app = request.app
    started = time.monotonic()
    try:
        status = await app["hermes"].request("GET", "/api/status", timeout=10)
        hermes = {key: status.get(key) for key in HERMES_STATUS_FIELDS} if isinstance(status, dict) else {}
        hermes.update(reachable=True, latency_ms=round((time.monotonic() - started) * 1000))
    except HermesError:
        hermes = {"reachable": False}
    return web.json_response({
        "roost": {"version": __version__, "revision": app["revision"]},
        "runtime": {"python": platform.python_version(), "aiohttp": aiohttp.__version__},
        "stats": await asyncio.to_thread(process_stats, app),
        "hermes": hermes,
    })


async def restart_agent(request: web.Request):
    await request.app["hermes"].request("POST", "/api/gateway/restart")
    return web.json_response({"ok": True})


async def restart_roost(request: web.Request):
    # Answer first; the service manager starts Roost again after this non-zero exit.
    asyncio.get_running_loop().call_later(0.5, request.app["exit"], RESTART_EXIT)
    return web.json_response({"ok": True})


async def health(request: web.Request):
    return web.json_response({"status": "ready"})


async def conversations(request: web.Request):
    page = request.query.get("offset", "0")
    if not re.fullmatch(r"[0-9]{1,6}", page) or int(page) > 100000:
        raise bad("Invalid page offset.")
    result = await request.app["hermes"].request("GET", f"/api/sessions?limit=100&order=recent&exclude_sources=cron&offset={page}")
    return web.json_response(result)


async def conversation(request: web.Request):
    sid = item_id(request, "sid")
    result = await request.app["hermes"].request("GET", f"/api/sessions/{sid}/messages?inline_images=false")
    return web.json_response(result)


async def create_conversation(request: web.Request):
    data = await json_body(request)
    text = body_text(data.get("text"), "Message", 16000)
    sid = await request.app["hermes"].send_message(text)
    return web.json_response({"session_id": sid}, status=201)


async def send_message(request: web.Request):
    sid = item_id(request, "sid", encode=False)
    data = await json_body(request)
    text = body_text(data.get("text"), "Message", 16000)
    stored = await request.app["hermes"].send_message(text, sid)
    return web.json_response({"session_id": stored})


async def rename_conversation(request: web.Request):
    sid = item_id(request, "sid")
    data = await json_body(request)
    if set(data) != {"title"}:
        raise bad("Send only a title.")
    title = body_text(data.get("title"), "Title", 200, required=False)
    try:
        result = await request.app["hermes"].request("PATCH", f"/api/sessions/{sid}", {"title": title})
    except HermesError as exc:
        if exc.status == 400:
            raise HermesError(400, "Hermes didn't accept that name. It may be too long or already used by another conversation.") from None
        raise
    return web.json_response(result)


async def delete_conversation(request: web.Request):
    sid = item_id(request, "sid")
    data = await json_body(request)
    if set(data) != {"confirm"} or data["confirm"] is not True:
        raise bad("Confirm conversation deletion.")
    result = await request.app["hermes"].request("DELETE", f"/api/sessions/{sid}")
    return web.json_response(result)


def job_fields(data: dict, *, create: bool) -> dict:
    if set(data) - JOB_FIELDS:
        raise bad("Unsupported job field.")
    if create and not {"name", "prompt", "schedule"}.issubset(data):
        raise bad("Name, prompt, and schedule are required.")
    if not data:
        raise bad("No job changes supplied.")
    result = {}
    for key, value in data.items():
        result[key] = body_text(value, key.capitalize(), 16000 if key == "prompt" else 500)
    if "deliver" not in result and create:
        result["deliver"] = "local"
    return result


async def jobs(request: web.Request):
    result = await request.app["hermes"].request("GET", "/api/cron/jobs")
    return web.json_response(result)


async def create_job(request: web.Request):
    data = job_fields(await json_body(request), create=True)
    result = await request.app["hermes"].request("POST", "/api/cron/jobs", data)
    return web.json_response(result, status=201)


async def get_job(request: web.Request):
    jid = item_id(request, "jid")
    result = await request.app["hermes"].request("GET", f"/api/cron/jobs/{jid}")
    return web.json_response(result)


async def job_runs(request: web.Request):
    jid = item_id(request, "jid")
    result = await request.app["hermes"].request("GET", f"/api/cron/jobs/{jid}/runs?limit=20")
    return web.json_response(result)


async def update_job(request: web.Request):
    jid = item_id(request, "jid")
    updates = job_fields(await json_body(request), create=False)
    result = await request.app["hermes"].request("PUT", f"/api/cron/jobs/{jid}", {"updates": updates})
    return web.json_response(result)


async def job_action(request: web.Request):
    jid = item_id(request, "jid")
    action = request.match_info["action"]
    if action not in {"pause", "resume", "trigger"}:
        raise web.HTTPNotFound()
    result = await request.app["hermes"].request("POST", f"/api/cron/jobs/{jid}/{action}")
    return web.json_response(result)


async def delete_job(request: web.Request):
    jid = item_id(request, "jid")
    data = await json_body(request)
    if set(data) != {"confirm"} or data["confirm"] is not True:
        raise bad("Confirm job deletion.")
    result = await request.app["hermes"].request("DELETE", f"/api/cron/jobs/{jid}")
    return web.json_response(result)


async def static(request: web.Request):
    name = request.match_info.get("name") or "index.html"
    if name not in {"index.html", "app.js", "theme.js", "style.css", "manifest.webmanifest", "icon.svg",
                    "SpaceGrotesk-Regular.ttf", "SpaceGrotesk-Bold.ttf"}:
        raise web.HTTPNotFound()
    return web.FileResponse(STATIC / name, headers={"Cache-Control": "no-store"})


async def startup(app: web.Application):
    await app["hermes"].start()


async def cleanup(app: web.Application):
    await app["hermes"].close()
    app["sessions"].close()


def create_app() -> web.Application:
    origin = os.environ["ROOST_PUBLIC_ORIGIN"].rstrip("/")
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})) or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password or not parsed.hostname or any(char.isspace() for char in origin):
        raise ValueError("ROOST_PUBLIC_ORIGIN must be HTTPS or a local HTTP origin.")
    try:
        origin = str(URL(origin).origin())
    except ValueError:
        raise ValueError("ROOST_PUBLIC_ORIGIN has an invalid port.") from None
    state = Path(os.environ.get("ROOST_STATE_DIR", "./.roost-state"))
    password_file = state / "password.hash"
    password_file.read_text(encoding="utf-8")  # fail at startup, not at first sign-in, if it is missing
    app = web.Application(middlewares=[guard], client_max_size=64 * 1024)
    app["origin"] = origin
    app["demo"] = os.environ.get("ROOST_DEMO") == "1"
    # The agent's display name comes from Roost's settings or Hermes; it is not a secret.
    app["settings_file"] = state / "settings.json"
    app["hermes_agent_name"] = ("", None)
    app["exit"] = os._exit
    app["state_dir"] = state
    app["started"] = time.monotonic()
    app["cpu_sample"] = (app["started"], time.process_time())
    # The commit this build came from, set by whatever built it; "" when unknown (a checkout, say).
    revision = os.environ.get("ROOST_REVISION", "").strip()
    app["revision"] = revision if re.fullmatch(r"[0-9a-f]{7,40}", revision) else ""
    app["trust_forwarded"] = os.environ.get("ROOST_TRUST_FORWARDED") == "1"
    app["secure_cookie"] = origin.startswith("https://")
    app["password_file"] = password_file
    app["login_attempts"] = {}
    app["login_inflight"] = {}
    app["sessions"] = Sessions(state / "sessions.sqlite3")
    # Passkeys are bound to the browser origin's host name (the WebAuthn relying party ID).
    app["passkeys"] = Passkeys(app["sessions"].db, origin, URL(origin).host)
    app["hermes"] = Hermes(os.environ["ROOST_HERMES_URL"], os.environ["ROOST_HERMES_USERNAME"],
                            Path(os.environ["ROOST_HERMES_PASSWORD_FILE"]))
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/me", me)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_get("/api/conversations", conversations)
    app.router.add_post("/api/conversations", create_conversation)
    app.router.add_get("/api/conversations/{sid}", conversation)
    app.router.add_post("/api/conversations/{sid}/messages", send_message)
    app.router.add_patch("/api/conversations/{sid}", rename_conversation)
    app.router.add_delete("/api/conversations/{sid}", delete_conversation)
    app.router.add_delete("/api/conversations", delete_all_conversations)
    app.router.add_get("/api/jobs", jobs)
    app.router.add_post("/api/jobs", create_job)
    app.router.add_get("/api/jobs/{jid}", get_job)
    app.router.add_get("/api/jobs/{jid}/runs", job_runs)
    app.router.add_put("/api/jobs/{jid}", update_job)
    app.router.add_post("/api/jobs/{jid}/{action}", job_action)
    app.router.add_delete("/api/jobs/{jid}", delete_job)
    app.router.add_delete("/api/jobs", delete_all_jobs)
    app.router.add_post("/api/passkeys/login/options", passkey_login_options)
    app.router.add_post("/api/passkeys/login", passkey_login)
    app.router.add_get("/api/passkeys", list_passkeys)
    app.router.add_post("/api/passkeys/register/options", passkey_registration_options)
    app.router.add_post("/api/passkeys", add_passkey)
    app.router.add_delete("/api/passkeys/{pid}", remove_passkey)
    app.router.add_get("/api/settings", get_settings)
    app.router.add_patch("/api/settings", update_settings)
    app.router.add_get("/api/settings/soul", get_soul)
    app.router.add_put("/api/settings/soul", update_soul)
    app.router.add_get("/api/about", about)
    app.router.add_post("/api/service/restart-agent", restart_agent)
    app.router.add_post("/api/service/restart-roost", restart_roost)
    app.router.add_get("/", static)
    app.router.add_get("/{name}", static)
    return app
