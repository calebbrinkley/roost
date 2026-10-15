"""Owner-only Roost HTTP service. No dashboard credential reaches the browser."""

import asyncio
import base64
import binascii
import io
import json
import os
import platform
import re
import time
import wave
import zipfile
from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote, urlsplit

import aiohttp
from aiohttp import web
from yarl import URL

from . import __version__, live

from .auth import Sessions, verify_password
from .chats import INSTRUCTIONS_LIMIT, Chats, with_instructions
from .deliveries import TEXT_LIMIT as DELIVERY_LIMIT, Deliveries
from .hermes import Hermes, HermesError
from .passkeys import PasskeyError, Passkeys
from .push import Push, PushError

STATIC = Path(__file__).with_name("static")
# The Hermes plugin Roost offers as a download; it runs inside Hermes, never in Roost.
HERMES_PLUGIN = Path(__file__).with_name("hermes_plugin")
# Voice: recordings up to 25 MB once encoded (Hermes's own limit); replies read up to 4,000 characters
# (about four minutes, whose audio stays under the 8 MB Roost reads from Hermes even as 128 kbps MP3 in
# base64); and the last few synthesized replies kept so a player's range requests don't synthesize again.
RECORDING_LIMIT, SPEECH_CHARACTERS, SPEECH_CACHE = 25 * 1024 * 1024, 4000, 8
# Hermes's plugin authenticates with Roost's delivery token instead of a signed-in browser's cookie.
DELIVERY_PATH = "/api/hermes/deliveries"
COOKIE = "roost_session"
ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
JOB_FIELDS = {"name", "prompt", "schedule", "deliver"}
# Accent presets; the browser holds each one's light and dark colours.
ACCENTS = ("forest", "ocean", "violet", "rose", "amber", "slate")
AGENT_NAME_CACHE_SECONDS = 300
# systemd restarts the service after a non-zero exit (Restart=on-failure); 75 is EX_TEMPFAIL.
RESTART_EXIT = 75
SOUL_LIMIT = 16000
# Reply notifications: how often the watcher looks, how long it waits for a reply, and how
# recently an open Roost must have reported a conversation on screen to count as looking at it.
WATCH_INTERVAL, WATCH_SECONDS, PRESENCE_SECONDS = 4, 1800, 35
# "" follows each device's own time zone; otherwise an IANA name such as "America/New_York".
TIME_ZONE = re.compile(r"^(UTC|[A-Za-z]+(/[A-Za-z0-9_+-]+){1,2})$")
# Hermes status fields shown in About; the rest (paths, PIDs, auth) stays on the server.
HERMES_STATUS_FIELDS = ("version", "release_date", "gateway_running", "gateway_state", "active_sessions")
# Hermes returns at most 500 messages a page; an export reads up to this many pages.
EXPORT_PAGE, EXPORT_PAGES = 500, 40


def bad(message: str, status: int = 400) -> web.HTTPException:
    return web.HTTPBadRequest(text=message) if status == 400 else web.HTTPForbidden(text=message)


def body_text(value, field: str, limit: int, *, required: bool = True) -> str:
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise bad(f"{field} is required and must be at most {limit} characters.")
    return value.strip()


async def json_body(request: web.Request, limit: int | None = None) -> dict:
    """The request's JSON object; `limit` (bytes) replaces the app-wide size limit for this route."""
    if request.content_type != "application/json":
        raise bad("Expected JSON.")
    try:
        value = json.loads(await bounded_body(request, limit)) if limit else await request.json()
    except (ValueError, UnicodeDecodeError):
        raise bad("Invalid JSON.") from None
    if not isinstance(value, dict):
        raise bad("Expected a JSON object.")
    return value


async def bounded_body(request: web.Request, limit: int) -> bytes:
    body = bytearray()
    async for chunk in request.content.iter_chunked(65536):
        if len(body) + len(chunk) > limit:
            raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=len(body) + len(chunk))
        body.extend(chunk)
    return bytes(body)


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
            if request.method not in {"GET", "HEAD", "OPTIONS"} and request.path != DELIVERY_PATH:
                if request.headers.get("Origin") != request.app["origin"] or request.headers.get("X-Roost-Request") != "1":
                    raise bad("Request origin was not accepted.", 403)
            if request.path not in {"/api/login", "/api/me", "/api/health", "/api/passkeys/login/options", "/api/passkeys/login", DELIVERY_PATH}:
                if not request.app["sessions"].valid(request.cookies.get(COOKIE, "")):
                    raise web.HTTPUnauthorized(text="Sign in to Roost.")
        response = await handler(request)
    except HermesError as exc:
        response = web.json_response({"error": exc.message}, status=exc.status)
    except web.HTTPException as exc:
        response = web.json_response({"error": exc.text or exc.reason}, status=exc.status)
    if not response.prepared:  # an event stream sent its headers already
        harden(request, response)
    return response


def harden(request: web.Request, response: web.StreamResponse) -> None:
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; form-action 'self'; frame-ancestors 'none'"


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
            request.app["chats"].forget(sid)
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


def message_text(row: dict) -> str:
    value = row.get("display_content") or row.get("content")
    if isinstance(value, list):
        value = " ".join(part if isinstance(part, str) else str((part or {}).get("text") or "") for part in value)
    return value if isinstance(value, str) else ""


def finished_reply(rows: list, since: float) -> str | None:
    """The reply text once the turn has ended: the transcript ends with an assistant message
    written after `since` that has text and no tool calls. None while the agent is still working.
    Hermes returns at most the newest 500 rows, so a row count can't say what is new; Hermes and
    Roost share the host's clock, so the reply's timestamp can."""
    if not rows or not isinstance(rows[-1], dict):
        return None
    last = rows[-1]
    text = message_text(last).strip()
    if last.get("role") != "assistant" or not text or last.get("tool_calls"):
        return None
    try:
        written = float(last.get("timestamp"))
    except (TypeError, ValueError):
        return None
    return text if written >= since else None


def preview(text: str, limit: int = 160) -> str:
    flat = " ".join(text.replace("```", " ").split())
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + "…"


def watch_reply(app: web.Application, sid: str, since: float) -> None:
    if isinstance(sid, str) and ID.fullmatch(sid):
        app["watches"][sid] = {"since": since, "started": time.monotonic()}


def looking_at(app: web.Application, sid: str) -> bool:
    now = time.monotonic()
    return any(now - seen < PRESENCE_SECONDS and conversation == sid for seen, conversation in app["presence"].values())


async def check_replies(app: web.Application) -> None:
    """One pass over conversations waiting for a reply; notify for each that finished unseen."""
    for sid, watch in list(app["watches"].items()):
        if time.monotonic() - watch["started"] > WATCH_SECONDS:
            app["watches"].pop(sid, None)
            continue
        turn = app.get("live", {}).get(sid)
        if turn is not None:
            sent = watch.setdefault("approvals_notified", set())
            for item in list(turn.approvals.values()):
                if item["request_id"] not in sent:
                    sent.add(item["request_id"])
                    if not looking_at(app, sid):
                        await notify(app, {
                            "title": await agent_name(app, ask_hermes=True) + " needs approval",
                            "body": preview("\n".join(filter(None, [item["description"], item["command"]])) or "Review this action in Roost.", 300),
                            "url": "/?c=" + quote(sid, safe="") + "&a=" + quote(item["request_id"], safe=""),
                            "tag": "approval-" + item["request_id"],
                            "approval": {"conversation": sid, "request_id": item["request_id"],
                                         "choices": item["choices"]}})
        try:
            data = await app["hermes"].request("GET", f"/api/sessions/{quote(sid, safe='')}/messages?inline_images=false")
        except HermesError:
            continue
        rows = data.get("messages") if isinstance(data, dict) else None
        reply = finished_reply(rows if isinstance(rows, list) else [], watch["since"])
        if reply is None:
            continue
        app["watches"].pop(sid, None)
        if not looking_at(app, sid):
            await notify(app, {"title": await agent_name(app, ask_hermes=True), "body": preview(reply),
                               "url": "/?c=" + quote(sid, safe=""), "tag": "reply-" + sid})


async def notify(app: web.Application, message: dict) -> int:
    try:
        return await app["push"].send(app["push_http"], message)
    except ValueError:
        return 0


async def watch_replies(app: web.Application) -> None:
    while True:
        await asyncio.sleep(WATCH_INTERVAL)
        try:
            await check_replies(app)
        except Exception:  # one bad pass must not end reply notifications until a restart
            pass


async def receive_delivery(request: web.Request):
    """A job report or a message the agent sends, from Hermes's Roost plugin."""
    deliveries = request.app["deliveries"]
    if not deliveries.authorized(request.headers.get("Authorization", "")):
        raise web.HTTPUnauthorized(text="Roost did not accept that delivery token.")
    data = await json_body(request, 4 * DELIVERY_LIMIT + 4096)
    text = body_text(data.get("text"), "Text", DELIVERY_LIMIT)
    job_id = data.get("job_id")
    if job_id is not None and not (isinstance(job_id, str) and ID.fullmatch(job_id)):
        raise bad("Invalid job identifier.")
    item = deliveries.add(text, job_id)
    url = "/?j=" + quote(item["job_id"], safe="") if item["job_id"] else "/?d=" + item["id"]
    await notify(request.app, {"title": item["title"] or await agent_name(request.app, ask_hermes=True),
                               "body": preview(item["text"]), "url": url, "tag": "delivery-" + item["id"]})
    return web.json_response({"id": item["id"]}, status=201)


async def get_delivery(request: web.Request):
    item = request.app["deliveries"].get(item_id(request, "did", encode=False))
    if item is None:
        raise web.HTTPNotFound(text="That message is no longer kept.")
    return web.json_response(item)


async def hermes_link(request: web.Request):
    """What Settings shows for connecting Hermes's plugin: the token, Roost's address and the last delivery."""
    deliveries = request.app["deliveries"]
    return web.json_response({"token": deliveries.token(), "url": request.app["origin"], "last_delivery": deliveries.last()})


async def rotate_delivery_token(request: web.Request):
    data = await json_body(request)
    if set(data) != {"confirm"} or data["confirm"] is not True:
        raise bad("Confirm replacing the delivery token.")
    request.app["deliveries"].rotate()
    return await hermes_link(request)


async def hermes_plugin(request: web.Request):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name in ("plugin.yaml", "__init__.py"):
            bundle.write(HERMES_PLUGIN / name, "roost/" + name)
    return web.Response(body=archive.getvalue(), content_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="roost-hermes-plugin.zip"'})


def speakable(text: str) -> str:
    """A reply as it should sound: no code, link targets, table rules or Markdown symbols."""
    text = re.sub(r"```.*?(```|\Z)", " ", text, flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s*\|?[\s:|-]{3,}\|?\s*$", " ", text, flags=re.M)
    text = re.sub(r"[`*_#>|~]+", " ", text)
    return " ".join(text.split())[:SPEECH_CHARACTERS]


def audio_response(request: web.Request, mime: str, audio: bytes) -> web.Response:
    """Audio with byte ranges: Safari plays media only from a server that answers them."""
    size, match = len(audio), re.fullmatch(r"bytes=(\d*)-(\d*)", request.headers.get("Range", "").strip())
    headers = {"Accept-Ranges": "bytes"}
    if not match or not (match[1] or match[2]):
        return web.Response(body=audio, content_type=mime, headers=headers)
    if match[1]:
        start, end = int(match[1]), min(int(match[2]) if match[2] else size - 1, size - 1)
    else:
        start, end = max(0, size - int(match[2])), size - 1
    if start > end:
        return web.Response(status=416, headers={**headers, "Content-Range": f"bytes */{size}"})
    return web.Response(status=206, body=audio[start:end + 1], content_type=mime,
                        headers={**headers, "Content-Range": f"bytes {start}-{end}/{size}"})


def silent_wav() -> bytes:
    """A tenth of a second of silence. Playing it from a tap lets a later reply play without one."""
    out = io.BytesIO()
    with wave.open(out, "wb") as clip:
        clip.setnchannels(1); clip.setsampwidth(1); clip.setframerate(8000)
        clip.writeframes(b"\x80" * 800)
    return out.getvalue()


SILENCE = silent_wav()


async def silence(request: web.Request):
    return audio_response(request, "audio/wav", SILENCE)


async def transcribe(request: web.Request):
    data = await json_body(request, RECORDING_LIMIT + 4096)
    data_url, mime = data.get("data_url"), data.get("mime_type")
    if not (isinstance(data_url, str) and data_url.startswith("data:") and ";base64," in data_url[:200]
            and isinstance(mime, str) and re.fullmatch(r"(audio/[a-z0-9.+-]+|video/webm)(;[ a-z0-9=.,\"-]*)?", mime)):
        raise bad("Expected an audio recording.")
    result = await request.app["hermes"].request("POST", "/api/audio/transcribe",
                                                 {"data_url": data_url, "mime_type": mime}, timeout=180)
    text = result.get("transcript") if isinstance(result, dict) else None
    return web.json_response({"transcript": text.strip() if isinstance(text, str) else ""})


async def synthesize(app: web.Application, sid: str, mid: str) -> tuple[str, bytes]:
    data = await app["hermes"].request("GET", f"/api/sessions/{quote(sid, safe='')}/messages?inline_images=false")
    rows = data.get("messages") if isinstance(data, dict) else None
    row = next((row for row in rows or [] if isinstance(row, dict) and str(row.get("id")) == mid), None)
    text = speakable(message_text(row)) if row and row.get("role") == "assistant" else ""
    if not text:
        raise web.HTTPNotFound(text="There is nothing to read aloud there.")
    result = await app["hermes"].request("POST", "/api/audio/speak", {"text": text}, timeout=180)
    spoken = result.get("data_url") if isinstance(result, dict) else None
    header, _, encoded = spoken.partition(",") if isinstance(spoken, str) else ("", "", "")
    mime = header[5:].split(";", 1)[0] if header.startswith("data:") and header.endswith(";base64") else ""
    try:
        audio = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        audio = b""
    if not mime.startswith("audio/") or not audio:
        raise HermesError(502, "Hermes returned no audio.")
    return mime, audio


async def speech(request: web.Request):
    """One of the agent's replies, read aloud. The URL names the reply, so a tap can start the player at once."""
    app = request.app
    key = (item_id(request, "sid", encode=False), item_id(request, "mid", encode=False))
    cache, pending = app["speech"], app["speech_pending"]
    if key not in cache:
        # A player's first range request and the next one share one synthesis.
        if key not in pending:
            pending[key] = asyncio.ensure_future(synthesize(app, *key))
            pending[key].add_done_callback(lambda _task: pending.pop(key, None))
        cache[key] = await asyncio.shield(pending[key])
        while len(cache) > SPEECH_CACHE:
            cache.popitem(last=False)
    cache.move_to_end(key)
    return audio_response(request, *cache[key])


async def presence(request: web.Request):
    data = await json_body(request)
    conversation = data.get("conversation")
    if set(data) != {"conversation"} or not (conversation is None or (isinstance(conversation, str) and ID.fullmatch(conversation))):
        raise bad("Send the conversation on screen, or null.")
    request.app["presence"][session_key(request)] = (time.monotonic(), conversation)
    return web.json_response({"ok": True})


async def push_settings(request: web.Request):
    return web.json_response({"public_key": request.app["push"].public_key(), "devices": request.app["push"].list()})


async def add_push_device(request: web.Request):
    data = await json_body(request)
    name = clean_name(data.get("name", ""))
    if set(data) != {"subscription", "name"} or not isinstance(data["subscription"], dict) or not name:
        raise bad("Send this browser's push subscription and a name for the device.")
    try:
        added = request.app["push"].subscribe(data["subscription"], name)
    except PushError as error:
        raise bad(str(error)) from None
    return web.json_response(added, status=201)


async def remove_push_device(request: web.Request):
    data = await json_body(request)
    if set(data) != {"confirm"} or data["confirm"] is not True:
        raise bad("Confirm removing the device.")
    if not request.app["push"].remove(request.match_info["did"]):
        raise web.HTTPNotFound(text="That device doesn't get Roost notifications.")
    return web.json_response({"ok": True})


async def test_push(request: web.Request):
    data = await json_body(request)
    device = data.get("device")
    if set(data) - {"device"} or not (device is None or isinstance(device, str)):
        raise bad("Send a device to test, or none for all.")
    delivered = await request.app["push"].send(request.app["push_http"], {
        "title": "Roost", "body": "Notifications from Roost are working.", "url": "/", "tag": "test"}, only=device)
    if not delivered:
        raise web.HTTPBadGateway(text="The push service didn't accept the test. Turn notifications off and on again on that device.")
    return web.json_response({"delivered": delivered})


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
    if isinstance(result, dict):
        # Says a reply is being written that /stream can show before Hermes saves it.
        result["live"] = item_id(request, "sid", encode=False) in request.app["live"]
    return web.json_response(result)


async def pending_approvals(request: web.Request):
    sid = item_id(request, "sid", encode=False)
    return web.json_response({"approvals": await request.app["hermes"].approvals(sid)})


async def respond_approval(request: web.Request):
    sid = item_id(request, "sid", encode=False)
    data = await json_body(request)
    if set(data) != {"request_id", "choice"} or data.get("choice") not in ("once", "deny"):
        raise bad("Choose Allow once or Deny for this request.")
    request_id = body_text(data.get("request_id"), "Request identifier", 256)
    result = await request.app["hermes"].approvals(sid, request_id, data["choice"])
    turn = request.app["live"].get(sid)
    if turn is not None:
        turn.approvals.pop(request_id, None)
        turn.notify()
    return web.json_response(result)


async def stream(request: web.Request):
    turn = request.app["live"].get(item_id(request, "sid", encode=False))
    if turn is None:
        raise web.HTTPNotFound(text="No reply is being written here.")
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    harden(request, response)
    await response.prepare(request)
    try:
        await live.send(response, turn)
    except ConnectionResetError:
        pass  # the browser left; it reconnects or reads the transcript
    return response


def following(app: web.Application, text: str):
    return lambda sid, events: live.follow(app["live"], sid, text, events)


async def create_conversation(request: web.Request):
    data = await json_body(request)
    text = body_text(data.get("text"), "Message", 16000)
    since = time.time()
    sid = await request.app["hermes"].send_message(text, follow=following(request.app, text))
    watch_reply(request.app, sid, since)
    return web.json_response({"session_id": sid}, status=201)


async def send_message(request: web.Request):
    sid = item_id(request, "sid", encode=False)
    data = await json_body(request)
    text = body_text(data.get("text"), "Message", 16000)
    # The reply to watch for is written after this message is sent.
    since = time.time()
    chats = request.app["chats"]
    unsent = chats.unsent(sid)
    outgoing = text if unsent is None else with_instructions(text, unsent)
    stored = await request.app["hermes"].send_message(outgoing, sid, follow=following(request.app, text))
    if unsent is None:
        chats.moved(sid, stored)
    else:
        chats.sent(sid, unsent, stored)
    watch_reply(request.app, stored, since)
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
    request.app["chats"].forget(item_id(request, "sid", encode=False))
    return web.json_response(result)


async def get_chat_settings(request: web.Request):
    sid = item_id(request, "sid", encode=False)
    return web.json_response({"instructions": request.app["chats"].instructions(sid)})


async def update_chat_settings(request: web.Request):
    sid = item_id(request, "sid", encode=False)
    data = await json_body(request)
    if set(data) != {"instructions"}:
        raise bad("Only instructions can be changed.")
    request.app["chats"].set_instructions(sid, body_text(data["instructions"], "Instructions", INSTRUCTIONS_LIMIT, required=False))
    return web.json_response({"instructions": request.app["chats"].instructions(sid)})


async def export_conversation(request: web.Request):
    """The whole conversation as a JSON file, older compressed history included."""
    sid, raw = item_id(request, "sid"), item_id(request, "sid", encode=False)
    messages = []
    for page in range(EXPORT_PAGES):
        data = await request.app["hermes"].request(
            "GET", f"/api/sessions/{sid}/messages?inline_images=false&order=oldest&include_compacted=true"
                   f"&limit={EXPORT_PAGE}&offset={page * EXPORT_PAGE}")
        rows = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise HermesError(502, "Hermes returned invalid data.")
        messages.extend(rows)
        if len(rows) < EXPORT_PAGE:
            break
    else:
        raise bad(f"This conversation is longer than {EXPORT_PAGE * EXPORT_PAGES} messages, too long to export.")
    body = {"conversation": raw, "exported": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "instructions": request.app["chats"].instructions(raw), "messages": messages}
    name = "conversation-" + re.sub(r"[^A-Za-z0-9_.-]", "_", raw) + ".json"
    return web.Response(body=json.dumps(body, ensure_ascii=False, indent=2).encode(), content_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})


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
    if name not in {"index.html", "app.js", "theme.js", "style.css", "manifest.webmanifest", "sw.js",
                    "icon-192.png", "icon-512.png", "apple-touch-icon.png",
                    "SpaceGrotesk-Regular.ttf", "SpaceGrotesk-Bold.ttf"}:
        raise web.HTTPNotFound()
    return web.FileResponse(STATIC / name, headers={"Cache-Control": "no-store"})


async def startup(app: web.Application):
    await app["hermes"].start()
    app["push_http"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    app["watcher"] = asyncio.create_task(watch_replies(app))


async def shutdown(app: web.Application):
    # Ends open event streams, which would otherwise hold up a restart until their turns finish.
    for turn in list(app["live"].values()):
        turn.task.cancel()


async def cleanup(app: web.Application):
    app["watcher"].cancel()
    await app["push_http"].close()
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
    # VAPID's subject must be an https: or mailto: URL; a local HTTP preview can't receive pushes anyway.
    app["chats"] = Chats(app["sessions"].db)
    app["deliveries"] = Deliveries(app["sessions"].db, state / "delivery-token")
    app["push"] = Push(app["sessions"].db, state / "vapid.pem", origin if origin.startswith("https://") else "https://localhost")
    app["watches"], app["presence"], app["live"], app["speech"], app["speech_pending"] = {}, {}, {}, OrderedDict(), {}
    app["hermes"] = Hermes(os.environ["ROOST_HERMES_URL"], os.environ["ROOST_HERMES_USERNAME"],
                            Path(os.environ["ROOST_HERMES_PASSWORD_FILE"]))
    app.on_startup.append(startup)
    app.on_shutdown.append(shutdown)
    app.on_cleanup.append(cleanup)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/me", me)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_get("/api/conversations", conversations)
    app.router.add_post("/api/conversations", create_conversation)
    app.router.add_get("/api/conversations/{sid}", conversation)
    app.router.add_get("/api/conversations/{sid}/stream", stream)
    app.router.add_get("/api/conversations/{sid}/approvals", pending_approvals)
    app.router.add_post("/api/conversations/{sid}/approvals", respond_approval)
    app.router.add_get("/api/conversations/{sid}/settings", get_chat_settings)
    app.router.add_put("/api/conversations/{sid}/settings", update_chat_settings)
    app.router.add_get("/api/conversations/{sid}/export", export_conversation)
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
    app.router.add_get("/api/push", push_settings)
    app.router.add_post("/api/push/devices", add_push_device)
    app.router.add_delete("/api/push/devices/{did}", remove_push_device)
    app.router.add_post("/api/push/test", test_push)
    app.router.add_post("/api/presence", presence)
    app.router.add_post(DELIVERY_PATH, receive_delivery)
    app.router.add_get("/api/deliveries/{did}", get_delivery)
    app.router.add_get("/api/hermes", hermes_link)
    app.router.add_post("/api/hermes/token", rotate_delivery_token)
    app.router.add_get("/api/hermes/plugin.zip", hermes_plugin)
    app.router.add_post("/api/audio/transcribe", transcribe)
    app.router.add_get("/api/audio/silence", silence)
    app.router.add_get("/api/conversations/{sid}/speech/{mid}", speech)
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
