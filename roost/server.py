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

from . import __version__, live, native, provider, servers, settings, speech as speech_client, turns
from .runner import Runner

from .auth import Sessions, verify_password
from .chats import INSTRUCTIONS_LIMIT, REASONING, Chats, with_instructions
from .deliveries import TEXT_LIMIT as DELIVERY_LIMIT, Deliveries
from .hermes import ID, Hermes, HermesError, plain_id
from .passkeys import PasskeyError, Passkeys
from .push import Push, PushError
from .settings import clean_name
from .store import Store, StoreError, RequestConflict
from . import reply_usage

STATIC = Path(__file__).with_name("static")
# The Hermes plugin Roost offers as a download; it runs inside Hermes, never in Roost.
HERMES_PLUGIN = Path(__file__).with_name("hermes_plugin")
# Voice: recordings up to 25 MB once encoded (Hermes's own limit); replies read up to 4,000 characters
# (about four minutes, whose audio stays under the 8 MB Roost reads from Hermes even as 128 kbps MP3 in
# base64); and the last few synthesized replies kept so a player's range requests don't synthesize again.
RECORDING_LIMIT, SPEECH_CHARACTERS, SPEECH_CACHE = 25 * 1024 * 1024, 4000, 8
# Hermes's plugin authenticates with Roost's delivery token instead of a signed-in browser's cookie.
DELIVERY_PATH = "/api/hermes/deliveries"
USAGE_PATH = "/api/hermes/reply-usage"
PLUGIN_PATHS = {DELIVERY_PATH, USAGE_PATH}
COOKIE = "roost_session"
JOB_FIELDS = {"name", "prompt", "schedule", "deliver"}
AGENT_NAME_CACHE_SECONDS = 300
# systemd restarts the service after a non-zero exit (Restart=on-failure); 75 is EX_TEMPFAIL.
RESTART_EXIT = 75
SOUL_LIMIT = 16000
# Reply notifications: how often the watcher looks, how long it waits for a reply, and how
# recently an open Roost must have reported a conversation on screen to count as looking at it.
WATCH_INTERVAL, WATCH_SECONDS, PRESENCE_SECONDS = 4, 1800, 35
# A finished reply no device accepted: how many times the watcher pushes it before giving up, and
# the wait after each failed attempt (times the attempt number), so a short outage can pass.
PUSH_ATTEMPTS, PUSH_RETRY_SECONDS = 3, 30
# Hermes status fields shown in About; the rest (paths, PIDs, auth) stays on the server.
HERMES_STATUS_FIELDS = ("version", "release_date", "gateway_running", "gateway_state", "active_sessions")
# Hermes returns at most 500 messages a page; an export reads up to this many pages.
EXPORT_PAGE, EXPORT_PAGES = 500, 40
SEND_TTL, SEND_LIMIT = 15 * 60, 500
SEND_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


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


def item_id(request: web.Request, name: str) -> str:
    value = plain_id(request.match_info[name])
    if not value:
        raise bad("Invalid identifier.")
    return value


def secured_cookie(response: web.StreamResponse, request: web.Request, token: str) -> None:
    response.set_cookie(COOKIE, token, max_age=30 * 24 * 60 * 60, httponly=True,
                        secure=request.app["secure_cookie"], samesite="Strict", path="/")


@web.middleware
async def guard(request: web.Request, handler):
    try:
        if request.path.startswith("/api/"):
            if request.method not in {"GET", "HEAD", "OPTIONS"} and request.path not in PLUGIN_PATHS:
                if request.headers.get("Origin") != request.app["origin"] or request.headers.get("X-Roost-Request") != "1":
                    raise bad("Request origin was not accepted.", 403)
            if request.path not in {"/api/login", "/api/me", "/api/health", "/api/passkeys/login/options", "/api/passkeys/login", *PLUGIN_PATHS}:
                if not request.app["sessions"].valid(request.cookies.get(COOKIE, "")):
                    raise web.HTTPUnauthorized(text="Sign in to Roost.")
        selected = native.HANDLERS.get(handler.__name__, handler) if native.enabled(request.app) else handler
        response = await selected(request)
    except speech_client.SpeechError as exc:
        response = web.json_response({"error": exc.message}, status=exc.status)
    except HermesError as exc:
        response = web.json_response({"error": exc.message}, status=exc.status)
    except StoreError as exc:
        response = web.json_response({"error": str(exc)}, status=409 if isinstance(exc, RequestConflict) else 400)
    except servers.ServerError as exc:
        response = web.json_response({"error": str(exc)}, status=503 if isinstance(exc, servers.ServerStorageError) else 400)
    except provider.ProviderError as exc:
        status = 503 if isinstance(exc, provider.ProviderBusy) else 504 if isinstance(exc, provider.ProviderTimeout) else 502
        response = web.json_response({"error": str(exc)}, status=status)
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
    saved = settings.load(request.app["settings_file"])
    return web.json_response({"signed_in": signed_in, "demo": request.app["demo"], "agent": agent,
                              "accent": saved["accent"] if signed_in else settings.ACCENTS[0],
                              "time_zone": saved["time_zone"] if signed_in else "",
                              "backend": saved["backend"] if signed_in else "hermes",
                              # Whether the sign-in page offers passkeys; not which ones exist.
                              "passkeys": request.app["passkeys"].count() > 0})


async def hermes_agent_name(app: web.Application, *, ask: bool) -> str:
    """The name Hermes shows for its profile ("" when it has none), cached for a few minutes."""
    if native.enabled(app):
        return ""
    cached, fetched = app["hermes_agent_name"]
    # `fetched` is None until the first lookup: monotonic time can be small just after boot.
    if ask and (fetched is None or time.monotonic() - fetched > AGENT_NAME_CACHE_SECONDS):
        try:
            profile = await app["hermes"].profile()
            cached = clean_name(profile.get("display_name")) or clean_name(profile.get("bot_title"))
            app["hermes_agent_name"] = (cached, time.monotonic())
        except HermesError:
            pass
    return cached


async def agent_name(app: web.Application, *, ask_hermes: bool) -> str:
    """Roost's own setting wins; then the name Hermes shows for its profile; then "Agent"."""
    if native.enabled(app) and "store" in app:
        return app["store"].effective_persona()["name"]
    return settings.load(app["settings_file"])["agent_name"] or await hermes_agent_name(app, ask=ask_hermes) or "Agent"


async def settings_view(app: web.Application) -> dict:
    """The saved settings, the presets, and the name each source gives (`agent` is the one shown)."""
    return {**settings.load(app["settings_file"]), "accents": list(settings.ACCENTS),
            "hermes_agent_name": await hermes_agent_name(app, ask=True), "agent": await agent_name(app, ask_hermes=True)}


async def get_settings(request: web.Request):
    return web.json_response(await settings_view(request.app))


async def get_servers(request: web.Request):
    return web.json_response({"servers": request.app["servers"].list()})


async def update_server(request: web.Request):
    return web.json_response(request.app["servers"].update(request.match_info["server_id"], await json_body(request)))


async def update_server_models(request: web.Request):
    return web.json_response(request.app["servers"].set_models(request.match_info["server_id"], await json_body(request)))


async def discover_server_models(request: web.Request):
    configured = request.app["servers"]
    sid = request.match_info["server_id"]
    saved = configured.get(sid)
    if not saved["base_url"]:
        raise servers.ServerError("Set a model base URL first.")
    async with aiohttp.ClientSession() as session:
        client = provider.Provider(session, saved["base_url"], configured.key(sid),
                                   timeout=aiohttp.ClientTimeout(total=30, connect=10, sock_read=15))
        discovered = await client.list_models()
    return web.json_response({"models": servers.merge_models(saved["models"], discovered)})


def fallback_ready(app: web.Application) -> bool:
    try:
        server = app["servers"].get("fallback")
    except servers.ServerError:
        return False
    return bool(server["base_url"] and any(m["enabled"] and m["id"] == server["default_model"] for m in server["models"]))


async def update_settings(request: web.Request):
    try:
        data = await json_body(request)
        if data.get("use_fallback") is True and not fallback_ready(request.app):
            raise settings.SettingsError("Set a fallback server and default model before turning the fallback on.")
        saved = settings.update(request.app["settings_file"], data)
        if saved["backend"] == "hermes" and not request.app["hermes_started"]:
            await request.app["hermes"].start()
            request.app["hermes_started"] = True
    except settings.SettingsError as exc:
        raise bad(str(exc)) from None
    return web.json_response(await settings_view(request.app))


async def get_soul(request: web.Request):
    return web.json_response({"content": await request.app["hermes"].soul()})


async def update_soul(request: web.Request):
    data = await json_body(request)
    if set(data) != {"content"} or not isinstance(data["content"], str) or len(data["content"]) > SOUL_LIMIT:
        raise bad(f"Instructions must be at most {SOUL_LIMIT} characters.")
    await request.app["hermes"].save_soul(data["content"])
    return web.json_response({"ok": True})


def confirmed_everything(data: dict) -> None:
    if set(data) != {"confirm"} or data["confirm"] != "DELETE":
        raise bad("Type DELETE to confirm.")


async def delete_all_conversations(request: web.Request):
    """Every conversation the Messages list shows. Cron run records stay with their jobs."""
    confirmed_everything(await json_body(request))
    hermes, deleted, attempted = request.app["hermes"], 0, set()
    while True:
        page = await hermes.sessions()
        ids = [item["id"] for item in page["sessions"] if item["id"] not in attempted]
        # Hermes kept rows it said it deleted: stop instead of asking again forever.
        if not ids:
            break
        for sid in ids:
            attempted.add(sid)
            try:
                await hermes.delete_session(sid)
            except HermesError as exc:
                raise HermesError(exc.status, f"Deleted {deleted} conversations, then Hermes refused the next one.") from None
            request.app["chats"].forget(sid)
            request.app["reply_usage"].forget(sid)
            deleted += 1
    return web.json_response({"deleted": deleted})


async def delete_all_jobs(request: web.Request):
    confirmed_everything(await json_body(request))
    hermes, deleted = request.app["hermes"], 0
    for job in (await hermes.jobs())["jobs"]:
        try:
            await hermes.delete_job(job["id"])
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
        if native.enabled(app):
            raise HermesError(503, "Hermes is disabled.")
        status = await app["hermes"].status()
        hermes = {key: status.get(key) for key in HERMES_STATUS_FIELDS}
        hermes.update(reachable=True, latency_ms=round((time.monotonic() - started) * 1000))
    except HermesError:
        hermes = {"reachable": False}
    return web.json_response({
        "roost": {"version": __version__, "revision": app["revision"]},
        "runtime": {"python": platform.python_version(), "aiohttp": aiohttp.__version__},
        "stats": await asyncio.to_thread(process_stats, app),
        "hermes": hermes,
    })


def preview(text: str, limit: int = 160) -> str:
    flat = " ".join(text.replace("```", " ").split())
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + "…"


def watch_reply(app: web.Application, sid: str, since: float) -> None:
    if isinstance(sid, str) and ID.fullmatch(sid):
        app["watches"][sid] = {"since": since, "started": time.monotonic()}


def sent_from_here(request: web.Request, sid: str) -> None:
    """The device that just sent a message is showing that conversation. Count it as on screen now:
    its own report can come several seconds later (a new conversation gets its id only from this
    reply), and a quick answer would otherwise notify the device it is open on."""
    if isinstance(sid, str) and ID.fullmatch(sid):
        request.app["presence"][session_key(request)] = (time.monotonic(), sid)


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
                    if not looking_at(app, sid):
                        sent.add(item["request_id"])
                        await notify(app, {
                            "title": await agent_name(app, ask_hermes=True) + " needs approval",
                            "body": preview("\n".join(filter(None, [item["description"], item["command"]])) or "Review this action in Roost.", 300),
                            "url": "/?c=" + quote(sid, safe="") + "&a=" + quote(item["request_id"], safe=""),
                            "tag": "approval-" + item["request_id"],
                            "approval": {"conversation": sid, "request_id": item["request_id"],
                                         "choices": item["choices"]}})
        try:
            rows = (await app["hermes"].messages(sid))["messages"]
        except HermesError:
            continue
        reply = turns.finished_reply(rows, watch["since"])
        if reply is None:
            # A turn that ended without a reply (an error, or quiet too long) has nothing to notify.
            if not turns.working(rows, turn=turn, since=watch["since"]):
                app["watches"].pop(sid, None)
            continue
        if looking_at(app, sid):
            app["watches"].pop(sid, None)
        elif time.monotonic() < watch.get("push_retry_at", 0):
            continue
        else:
            attempts = watch.get("push_attempts", 0) + 1
            delivered = await notify(app, {"title": await agent_name(app, ask_hermes=True),
                                           "body": preview(reply),
                                           "url": "/?c=" + quote(sid, safe=""), "tag": "reply-" + sid})
            if delivered == 0 and attempts < PUSH_ATTEMPTS and app["push"].list():
                # No device accepted it: keep the watch so later passes retry the push.
                watch["push_attempts"] = attempts
                watch["push_retry_at"] = time.monotonic() + PUSH_RETRY_SECONDS * attempts
            else:
                app["watches"].pop(sid, None)


async def notify(app: web.Application, message: dict) -> int:
    try:
        return await app["push"].send(app["push_http"], message)
    except ValueError:
        return 0


async def watch_replies(app: web.Application) -> None:
    while True:
        await asyncio.sleep(WATCH_INTERVAL)
        try:
            if native.enabled(app):
                continue
            await check_replies(app)
        except Exception:  # one bad pass must not end reply notifications until a restart
            pass


async def receive_reply_usage(request: web.Request):
    if not request.app["deliveries"].authorized(request.headers.get("Authorization", "")):
        raise web.HTTPUnauthorized(text="Roost did not accept that delivery token.")
    data = await json_body(request, 4096)
    if set(data) != reply_usage.FIELDS:
        raise bad("Unsupported reply measurement fields.")
    sid = data["session_id"]
    if not isinstance(sid, str) or not ID.fullmatch(sid) or sid in {".", ".."}:
        raise bad("Invalid conversation identifier.")
    if reply_usage.count(data["message_id"]) is None or data["message_id"] == 0:
        raise bad("Invalid message identifier.")
    for key, validate in (("input_tokens", reply_usage.count), ("output_tokens", reply_usage.count), ("model_seconds", reply_usage.seconds)):
        if data[key] is not None and validate(data[key]) is None:
            raise bad("Invalid reply measurement.")
    request.app["reply_usage"].add(data)
    return web.json_response({"ok": True})


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
    item = request.app["deliveries"].get(item_id(request, "did"))
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
    return web.json_response({"transcript": await request.app["hermes"].transcribe(data_url, mime)})


async def synthesize(app: web.Application, sid: str, mid: str) -> tuple[str, bytes]:
    if native.enabled(app):
        row = app['store'].get_message(int(mid)) if re.fullmatch(r'[0-9]{1,16}', mid) else None
        text = speakable(re.sub(r'\[\d+\]', '', row.content)) if row and row.conversation_id == sid and row.role == 'assistant' else ''
        if not text:
            raise web.HTTPNotFound(text="There is nothing to read aloud there.")
        return await native.speech_client(app).speak(text)
    rows = (await app["hermes"].messages(sid))["messages"]
    row = next((row for row in rows if isinstance(row, dict) and str(row.get("id")) == mid), None)
    text = speakable(turns.message_text(row)) if row and row.get("role") == "assistant" else ""
    if not text:
        raise web.HTTPNotFound(text="There is nothing to read aloud there.")
    header, _, encoded = (await app["hermes"].speak(text)).partition(",")
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
    key = (item_id(request, "sid"), item_id(request, "mid"))
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
    await request.app["hermes"].restart()
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
    page = await request.app["hermes"].sessions(int(page))
    # Only conversations Roost is following or waiting on: the list never reads transcripts.
    for item in page["sessions"]:
        turn = request.app["live"].get(item["id"])
        item["working"] = item["id"] in request.app["watches"] or (turn is not None and not turn.done)
    return web.json_response(page)


async def conversation(request: web.Request):
    sid = item_id(request, "sid")
    before, limit = request.query.get("before"), request.query.get("limit", "500")
    if (len(request.query.getall("before", [])) > 1 or len(request.query.getall("limit", [])) > 1
            or not re.fullmatch(r"[0-9]{1,3}", limit) or not 1 <= int(limit) <= 500
            or (before is None and "limit" in request.query)
            or (before is not None and (not re.fullmatch(r"[0-9]{1,16}", before)
                                       or not 1 <= int(before) <= 9007199254740991))):
        raise bad("Invalid history page.")
    if before is not None:
        result = await request.app["hermes"].older_messages(sid, before=int(before), limit=int(limit))
        request.app["reply_usage"].attach(sid, result["messages"])
        return web.json_response(result)
    result = await request.app["hermes"].messages(sid)
    # Hermes reports no total. A full newest window may have older rows; a history page confirms it.
    result["has_older"] = len(result["messages"]) == 500
    turn, watch = request.app["live"].get(sid), request.app["watches"].get(sid)
    # Says a reply is being written that /stream can show before Hermes saves it.
    result["live"] = turn is not None
    since = watch["since"] if watch else turn.started if turn else None
    result["working"] = turns.working(result["messages"], turn=turn, since=since)
    # Where the turn Roost sent begins: its rows are the ones with a higher id. `started` is the live
    # turn's own mark (the stream sends it too), so a browser pairs `after` with the right turn.
    result["after"] = turns.after(result["messages"], since) if since is not None else None
    result["started"] = turn.started if turn else None
    request.app["reply_usage"].attach(result.get("session_id") or sid, result["messages"])
    return web.json_response(result)


async def pending_approvals(request: web.Request):
    sid = item_id(request, "sid")
    return web.json_response({"approvals": await request.app["hermes"].approvals(sid)})


async def respond_approval(request: web.Request):
    sid = item_id(request, "sid")
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
    turn = request.app["live"].get(item_id(request, "sid"))
    if turn is None:
        raise web.HTTPNotFound(text="No reply is being written here.")
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    harden(request, response)
    await response.prepare(request)
    try:
        await live.send(response, turn,
                        lambda: request.app["sessions"].valid(request.cookies.get(COOKIE, "")))
    except ConnectionResetError:
        pass  # the browser left; it reconnects or reads the transcript
    return response


def following(app: web.Application, text: str):
    def follow(sid, events):
        if events is None:
            # Hermes keeps processing; only the ambiguous Roost view/socket is retired.
            if turn := app["live"].pop(sid, None):
                turn.task.cancel()
        else:
            live.follow(app["live"], sid, text, events)
    return follow


async def create_conversation(request: web.Request):
    data = await json_body(request)
    text = body_text(data.get("text"), "Message", 16000)
    level = reasoning_level(data.get("reasoning", "default"))
    entry, response = begin_send(request, data, 201)
    if response is not None:
        return response
    since = time.time()
    try:
        sid = await request.app["hermes"].send_message(text, follow=following(request.app, text),
                                                       reasoning=None if level == "default" else level)
    except HermesError as exc:
        failed_send(request, data, entry, exc)
        raise
    if entry is not None:
        entry.update(state="done", session_id=sid)
    request.app["chats"].set_reasoning(sid, level)
    watch_reply(request.app, sid, since)
    sent_from_here(request, sid)
    return web.json_response({"session_id": sid}, status=201)


async def send_message(request: web.Request):
    sid = item_id(request, "sid")
    data = await json_body(request)
    text = body_text(data.get("text"), "Message", 16000)
    entry, response = begin_send(request, data, 200)
    if response is not None:
        return response
    # The reply to watch for is written after this message is sent.
    since = time.time()
    chats = request.app["chats"]
    unsent = chats.unsent(sid)
    outgoing = text if unsent is None else with_instructions(text, unsent)
    applied = []
    try:
        stored = await request.app["hermes"].send_message(outgoing, sid, follow=following(request.app, text),
                                                          reasoning=chats.reasoning_to_apply(sid), applied=applied.append)
    except HermesError as exc:
        failed_send(request, data, entry, exc)
        raise
    if entry is not None:
        entry.update(state="done", session_id=stored)
    if unsent is None:
        chats.moved(sid, stored)
    else:
        chats.sent(sid, unsent, stored)
    for level in applied:
        chats.reasoning_applied(stored, level)
    watch_reply(request.app, stored, since)
    sent_from_here(request, stored)
    return web.json_response({"session_id": stored})


def begin_send(request: web.Request, data: dict, status: int):
    # Older clients can omit the key. Keyed requests reserve it before any Hermes call.
    if "request_id" not in data:
        return None, None
    key = data["request_id"]
    if not isinstance(key, str) or not SEND_ID.fullmatch(key):
        raise bad("Invalid send request identifier.")
    entries, now = request.app["send_requests"], time.monotonic()
    while entries and now - next(iter(entries.values()))["started"] >= SEND_TTL:
        entries.popitem(last=False)
    if key in entries:
        entry = entries[key]
        if entry["state"] == "done":
            return entry, web.json_response({"session_id": entry["session_id"]}, status=entry["status"])
        return entry, web.json_response({"pending": True, "error": "Still sending. Check again shortly."}, status=409)
    while len(entries) >= SEND_LIMIT:
        entries.popitem(last=False)
    entry = {"state": "pending", "session_id": None, "started": now, "status": status}
    entries[key] = entry
    return entry, None


def failed_send(request: web.Request, data: dict, entry, error: HermesError):
    if entry is not None and (error.not_accepted or 400 <= error.status < 500):
        entries = request.app["send_requests"]
        if entries.get(data["request_id"]) is entry:
            del entries[data["request_id"]]


async def rename_conversation(request: web.Request):
    sid = item_id(request, "sid")
    data = await json_body(request)
    if set(data) != {"title"}:
        raise bad("Send only a title.")
    title = body_text(data.get("title"), "Title", 200, required=False)
    try:
        result = await request.app["hermes"].rename_session(sid, title)
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
    result = await request.app["hermes"].delete_session(sid)
    request.app["chats"].forget(sid)
    request.app["reply_usage"].forget(sid)
    return web.json_response(result)


def reasoning_level(value) -> str:
    if value not in REASONING:
        raise bad("Unknown reasoning level.")
    return value


def chat_settings(chats, sid: str) -> dict:
    return {"instructions": chats.instructions(sid), "reasoning": chats.reasoning(sid)}


async def get_chat_settings(request: web.Request):
    return web.json_response(chat_settings(request.app["chats"], item_id(request, "sid")))


async def update_chat_settings(request: web.Request):
    sid = item_id(request, "sid")
    data = await json_body(request)
    if not data or set(data) - {"instructions", "reasoning"}:
        raise bad("Only instructions and reasoning can be changed.")
    chats = request.app["chats"]
    if "reasoning" in data:
        level = reasoning_level(data["reasoning"])
    if "instructions" in data:
        chats.set_instructions(sid, body_text(data["instructions"], "Instructions", INSTRUCTIONS_LIMIT, required=False))
    if "reasoning" in data:
        chats.set_reasoning(sid, level)
    return web.json_response(chat_settings(chats, sid))


async def export_conversation(request: web.Request):
    """The whole conversation as a JSON file, older compressed history included."""
    raw = item_id(request, "sid")
    messages = []
    for page in range(EXPORT_PAGES):
        rows = await request.app["hermes"].history(raw, offset=page * EXPORT_PAGE, limit=EXPORT_PAGE)
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
    return web.json_response(await request.app["hermes"].jobs())


async def create_job(request: web.Request):
    data = job_fields(await json_body(request), create=True)
    return web.json_response(await request.app["hermes"].create_job(data), status=201)


async def get_job(request: web.Request):
    return web.json_response(await request.app["hermes"].job(item_id(request, "jid")))


async def job_runs(request: web.Request):
    return web.json_response(await request.app["hermes"].runs(item_id(request, "jid")))


async def update_job(request: web.Request):
    jid = item_id(request, "jid")
    updates = job_fields(await json_body(request), create=False)
    return web.json_response(await request.app["hermes"].update_job(jid, updates))


async def job_action(request: web.Request):
    return web.json_response(await request.app["hermes"].job_action(item_id(request, "jid"), request.match_info["action"]))


async def delete_job(request: web.Request):
    jid = item_id(request, "jid")
    data = await json_body(request)
    if set(data) != {"confirm"} or data["confirm"] is not True:
        raise bad("Confirm job deletion.")
    return web.json_response(await request.app["hermes"].delete_job(jid))


async def static(request: web.Request):
    name = request.match_info.get("name") or "index.html"
    if name not in {"index.html", "app.js", "theme.js", "style.css", "manifest.webmanifest", "sw.js",
                    "icon-192.png", "icon-512.png", "apple-touch-icon.png",
                    "SpaceGrotesk-Regular.ttf", "SpaceGrotesk-Bold.ttf"}:
        raise web.HTTPNotFound()
    return web.FileResponse(STATIC / name, headers={"Cache-Control": "no-store"})


async def native_store(app: web.Application):
    from .jobs import Jobs, enqueue
    app["store"] = Store(app["state_dir"] / "native.sqlite3")
    app["provider_http"] = aiohttp.ClientSession()
    async def completed(cid, tid, text):
        turn = app['store'].get_turn(tid)
        if turn and not turn.settings.get('job_run') and not looking_at(app, cid):
            enqueue(app["store"], "reply-" + tid, {"title": await agent_name(app, ask_hermes=False), "body": preview(text),
                    "url": "/?c=" + quote(cid, safe=""), "tag": "reply-" + tid}, time.time())
    app["runner"] = Runner(app["store"], app["servers"], app["provider_http"], completed=completed,
                           limit=int(os.environ.get("ROOST_NATIVE_ADMISSION", "1")),
                           preferences=lambda: settings.load(app["settings_file"]))
    app["jobs"] = Jobs(app["store"], app["runner"])
    app["runner"].start()
    try:
        yield
    finally:
        await app["jobs"].close()
        await app["runner"].close()
        await app["provider_http"].close()
        app["store"].close()


async def startup(app: web.Application):
    app["hermes_started"] = False
    if not native.enabled(app):
        await app["hermes"].start()
        app["hermes_started"] = True
    app["push_http"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    from .jobs import Outbox, serve, deliver
    app["outbox"] = Outbox(app["store"], app["push"], app["push_http"])
    app["job_scheduler"] = asyncio.create_task(serve(app))
    app["outbox_worker"] = asyncio.create_task(deliver(app))
    app["watcher"] = asyncio.create_task(watch_replies(app))


async def shutdown(app: web.Application):
    for key in ('job_scheduler', 'outbox_worker'):
        app[key].cancel()
    await asyncio.gather(app['job_scheduler'], app['outbox_worker'], return_exceptions=True)
    await app['jobs'].close()
    streams = list(app["native_streams"])
    for task in streams:
        task.cancel()
    await asyncio.gather(*streams, return_exceptions=True)
    await app["runner"].close()
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
    # An explicit VAPID subject overrides the origin (Apple's push service rejects private host names).
    subject = os.environ.get("ROOST_PUSH_SUBJECT", "")
    if subject:
        if subject.startswith("mailto:"):
            if not subject.removeprefix("mailto:") or any(char.isspace() for char in subject):
                raise ValueError("ROOST_PUSH_SUBJECT must be an https URL or a mailto: address.")
        else:
            parsed = urlsplit(subject)
            if parsed.scheme != "https" or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password or not parsed.hostname or any(char.isspace() for char in subject):
                raise ValueError("ROOST_PUSH_SUBJECT must be an https URL or a mailto: address.")
            try:
                subject = str(URL(subject).origin())
            except ValueError:
                raise ValueError("ROOST_PUSH_SUBJECT has an invalid port.") from None
    state = Path(os.environ.get("ROOST_STATE_DIR", "./.roost-state"))
    password_file = state / "password.hash"
    password_file.read_text(encoding="utf-8")  # fail at startup, not at first sign-in, if it is missing
    model_servers = servers.Servers(state)
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
    app["servers"] = model_servers
    app["voice_settings"] = speech_client.VoiceSettings(state)
    # Passkeys are bound to the browser origin's host name (the WebAuthn relying party ID).
    app["passkeys"] = Passkeys(app["sessions"].db, origin, URL(origin).host)
    # VAPID's subject must be an https: or mailto: URL; a local HTTP preview can't receive pushes anyway.
    app["chats"] = Chats(app["sessions"].db)
    app["reply_usage"] = reply_usage.ReplyUsage(app["sessions"].db)
    app["deliveries"] = Deliveries(app["sessions"].db, state / "delivery-token")
    if not subject:
        subject = origin if origin.startswith("https://") else "https://localhost"
    app["push"] = Push(app["sessions"].db, state / "vapid.pem", subject)
    app["watches"], app["presence"], app["live"], app["speech"], app["speech_pending"] = {}, {}, {}, OrderedDict(), {}
    app["send_requests"] = OrderedDict()
    app["native_streams"] = set()
    app["hermes"] = Hermes(os.environ.get("ROOST_HERMES_URL", "http://127.0.0.1:1"), os.environ.get("ROOST_HERMES_USERNAME", ""),
                            Path(os.environ.get("ROOST_HERMES_PASSWORD_FILE", str(state / "hermes-password"))))
    app.cleanup_ctx.append(native_store)
    app.on_startup.append(startup)
    app.on_shutdown.append(shutdown)
    app.on_cleanup.append(cleanup)
    app.router.add_get("/api/personas", native.personas)
    app.router.add_post("/api/personas", native.personas)
    app.router.add_put("/api/personas/default", native.default_persona)
    app.router.add_put("/api/personas/{pid}", native.persona)
    app.router.add_delete("/api/personas/{pid}", native.persona)
    app.router.add_get("/api/memory", native.memory)
    app.router.add_post("/api/memory", native.memory)
    app.router.add_put("/api/memory/{fid}", native.memory_fact)
    app.router.add_delete("/api/memory/{fid}", native.memory_fact)
    app.router.add_get("/api/models", native.models)
    app.router.add_post("/api/settings/web-search/test", native.test_web_search)
    app.router.add_get("/api/search", native.search)
    app.router.add_post("/api/attachments/extract", native.extract_attachment)
    app.router.add_post("/api/conversations/{sid}/turns/{action}", native.control)
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
    app.router.add_post(USAGE_PATH, receive_reply_usage)
    app.router.add_get("/api/deliveries/{did}", get_delivery)
    app.router.add_get("/api/hermes", hermes_link)
    app.router.add_post("/api/hermes/token", rotate_delivery_token)
    app.router.add_get("/api/hermes/plugin.zip", hermes_plugin)
    app.router.add_post("/api/audio/transcribe", transcribe)
    app.router.add_get("/api/audio/silence", silence)
    app.router.add_get("/api/conversations/{sid}/speech/{mid}", speech)
    app.router.add_get("/api/settings", get_settings)
    app.router.add_patch("/api/settings", update_settings)
    app.router.add_get("/api/settings/voice", native.voice_settings)
    app.router.add_put("/api/settings/voice", native.voice_settings)
    app.router.add_post("/api/settings/voice/test", native.voice_test)
    app.router.add_get("/api/settings/servers", get_servers)
    app.router.add_put("/api/settings/servers/{server_id}", update_server)
    app.router.add_post("/api/settings/servers/{server_id}/discover", discover_server_models)
    app.router.add_put("/api/settings/servers/{server_id}/models", update_server_models)
    app.router.add_get("/api/settings/soul", get_soul)
    app.router.add_put("/api/settings/soul", update_soul)
    app.router.add_get("/api/about", about)
    app.router.add_post("/api/service/restart-agent", restart_agent)
    app.router.add_post("/api/service/restart-roost", restart_roost)
    app.router.add_get("/", static)
    app.router.add_get("/{name}", static)
    return app
