"""Owner-only Roost HTTP service. Provider credentials stay on the server."""

import asyncio
import io
import json
import os
import platform
import re
import time
import wave
from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote, urlsplit

import aiohttp
from aiohttp import web
from yarl import URL

from . import __version__, handlers, provider, servers, settings, speech as speech_client
from .runner import Runner

from .auth import Sessions, verify_password
from .passkeys import PasskeyError, Passkeys
from .push import Push, PushError
from .settings import clean_name
from .store import Store, StoreError, RequestConflict

STATIC = Path(__file__).with_name("static")
# Reply synthesis is bounded and cached for repeated byte-range requests.
SPEECH_CHARACTERS, SPEECH_CACHE = 4000, 8
# A conversation, job or message ID is also a path segment.
ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
HEARTBEAT_SECONDS = 15
COOKIE = "roost_session"
# systemd restarts the service after a non-zero exit (Restart=on-failure); 75 is EX_TEMPFAIL.
RESTART_EXIT = 75
PRESENCE_SECONDS = 35
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


def plain_id(value) -> str:
    """The ID when it is a safe path segment, else an empty string."""
    return value if isinstance(value, str) and ID.fullmatch(value) and value not in {".", ".."} else ""


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
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                if request.headers.get("Origin") != request.app["origin"] or request.headers.get("X-Roost-Request") != "1":
                    raise bad("Request origin was not accepted.", 403)
            if request.path not in {"/api/login", "/api/me", "/api/health", "/api/passkeys/login/options", "/api/passkeys/login"}:
                if not request.app["sessions"].valid(request.cookies.get(COOKIE, "")):
                    raise web.HTTPUnauthorized(text="Sign in to Roost.")
        response = await handler(request)
    except speech_client.SpeechError as exc:
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
    agent = await agent_name(request.app)
    saved = settings.load(request.app["settings_file"])
    return web.json_response({"signed_in": signed_in, "demo": request.app["demo"], "agent": agent,
                              "accent": saved["accent"] if signed_in else settings.ACCENTS[0],
                              "time_zone": saved["time_zone"] if signed_in else "",
                              # Whether the sign-in page offers passkeys; not which ones exist.
                              "passkeys": request.app["passkeys"].count() > 0})


async def agent_name(app: web.Application) -> str:
    """The default persona also names the assistant before sign-in."""
    return app["store"].effective_persona()["name"]


async def settings_view(app: web.Application) -> dict:
    return {**settings.load(app["settings_file"]), "accents": list(settings.ACCENTS),
            "agent": await agent_name(app)}


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
        settings.update(request.app["settings_file"], data)
    except settings.SettingsError as exc:
        raise bad(str(exc)) from None
    return web.json_response(await settings_view(request.app))


def confirmed_everything(data: dict) -> None:
    if set(data) != {"confirm"} or data["confirm"] != "DELETE":
        raise bad("Type DELETE to confirm.")


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
    return web.json_response({
        "roost": {"version": __version__, "revision": app["revision"]},
        "runtime": {"python": platform.python_version(), "aiohttp": aiohttp.__version__},
        "stats": await asyncio.to_thread(process_stats, app),
    })


def preview(text: str, limit: int = 160) -> str:
    flat = " ".join(text.replace("```", " ").split())
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + "…"


def sent_from_here(request: web.Request, sid: str) -> None:
    """The device that just sent a message is showing that conversation. Count it as on screen now:
    its own report can come several seconds later (a new conversation gets its id only from this
    reply), and a quick answer would otherwise notify the device it is open on."""
    if isinstance(sid, str) and ID.fullmatch(sid):
        request.app["presence"][session_key(request)] = (time.monotonic(), sid)


def looking_at(app: web.Application, sid: str) -> bool:
    now = time.monotonic()
    return any(now - seen < PRESENCE_SECONDS and conversation == sid for seen, conversation in app["presence"].values())


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


async def synthesize(app: web.Application, sid: str, mid: str) -> tuple[str, bytes]:
    row = app['store'].get_message(int(mid)) if re.fullmatch(r'[0-9]{1,16}', mid) else None
    text = speakable(re.sub(r'\[\d+\]', '', row.content)) if row and row.conversation_id == sid and row.role == 'assistant' else ''
    if not text:
        raise web.HTTPNotFound(text="There is nothing to read aloud there.")
    return await handlers.speech_client(app).speak(text)


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


async def restart_roost(request: web.Request):
    # Answer first; the service manager starts Roost again after this non-zero exit.
    asyncio.get_running_loop().call_later(0.5, request.app["exit"], RESTART_EXIT)
    return web.json_response({"ok": True})


async def health(request: web.Request):
    return web.json_response({"status": "ready"})


async def static(request: web.Request):
    name = request.match_info.get("name") or "index.html"
    if name not in {"index.html", "app.js", "theme.js", "style.css", "manifest.webmanifest", "sw.js",
                    "icon-192.png", "icon-512.png", "apple-touch-icon.png",
                    "SpaceGrotesk-Regular.ttf", "SpaceGrotesk-Bold.ttf"}:
        raise web.HTTPNotFound()
    return web.FileResponse(STATIC / name, headers={"Cache-Control": "no-store"})


async def chat_store(app: web.Application):
    from .jobs import Jobs, enqueue
    app["store"] = Store(app["state_dir"] / "native.sqlite3")
    app["provider_http"] = aiohttp.ClientSession()
    async def completed(cid, tid, text):
        turn = app['store'].get_turn(tid)
        if turn and not turn.settings.get('job_run') and not looking_at(app, cid):
            enqueue(app["store"], "reply-" + tid, {"title": await agent_name(app), "body": preview(text),
                    "url": "/?c=" + quote(cid, safe=""), "tag": "reply-" + tid}, time.time())
    app["runner"] = Runner(app["store"], app["servers"], app["provider_http"], completed=completed,
                           limit=int(os.environ.get("ROOST_ADMISSION", os.environ.get("ROOST_NATIVE_ADMISSION", "1"))),
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
    app["push_http"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    from .jobs import Outbox, serve, deliver
    app["outbox"] = Outbox(app["store"], app["push"], app["push_http"])
    app["job_scheduler"] = asyncio.create_task(serve(app))
    app["outbox_worker"] = asyncio.create_task(deliver(app))


async def shutdown(app: web.Application):
    for key in ('job_scheduler', 'outbox_worker'):
        app[key].cancel()
    await asyncio.gather(app['job_scheduler'], app['outbox_worker'], return_exceptions=True)
    await app['jobs'].close()
    streams = list(app["streams"])
    for task in streams:
        task.cancel()
    await asyncio.gather(*streams, return_exceptions=True)
    await app["runner"].close()


async def cleanup(app: web.Application):
    await app["push_http"].close()
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
    app["settings_file"] = state / "settings.json"
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
    if not subject:
        subject = origin if origin.startswith("https://") else "https://localhost"
    app["push"] = Push(app["sessions"].db, state / "vapid.pem", subject)
    app["presence"], app["speech"], app["speech_pending"] = {}, OrderedDict(), {}
    app["streams"] = set()
    app.cleanup_ctx.append(chat_store)
    app.on_startup.append(startup)
    app.on_shutdown.append(shutdown)
    app.on_cleanup.append(cleanup)
    app.router.add_get("/api/personas", handlers.personas)
    app.router.add_post("/api/personas", handlers.personas)
    app.router.add_put("/api/personas/default", handlers.default_persona)
    app.router.add_put("/api/personas/{pid}", handlers.persona)
    app.router.add_delete("/api/personas/{pid}", handlers.persona)
    app.router.add_get("/api/memory", handlers.memory)
    app.router.add_post("/api/memory", handlers.memory)
    app.router.add_put("/api/memory/{fid}", handlers.memory_fact)
    app.router.add_delete("/api/memory/{fid}", handlers.memory_fact)
    app.router.add_get("/api/models", handlers.models)
    app.router.add_post("/api/settings/web-search/test", handlers.test_web_search)
    app.router.add_get("/api/search", handlers.search)
    app.router.add_post("/api/attachments/extract", handlers.extract_attachment)
    app.router.add_post("/api/conversations/{sid}/turns/{action}", handlers.control)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/me", me)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_get("/api/conversations", handlers.conversations)
    app.router.add_post("/api/conversations", handlers.create_conversation)
    app.router.add_get("/api/conversations/{sid}", handlers.conversation)
    app.router.add_get("/api/conversations/{sid}/stream", handlers.stream)
    app.router.add_get("/api/conversations/{sid}/settings", handlers.get_chat_settings)
    app.router.add_put("/api/conversations/{sid}/settings", handlers.update_chat_settings)
    app.router.add_get("/api/conversations/{sid}/export", handlers.export_conversation)
    app.router.add_post("/api/conversations/{sid}/messages", handlers.send_message)
    app.router.add_patch("/api/conversations/{sid}", handlers.rename_conversation)
    app.router.add_delete("/api/conversations/{sid}", handlers.delete_conversation)
    app.router.add_delete("/api/conversations", handlers.delete_all_conversations)
    app.router.add_get("/api/jobs", handlers.jobs)
    app.router.add_post("/api/jobs", handlers.create_job)
    app.router.add_get("/api/jobs/{jid}", handlers.get_job)
    app.router.add_get("/api/jobs/{jid}/runs", handlers.job_runs)
    app.router.add_put("/api/jobs/{jid}", handlers.update_job)
    app.router.add_post("/api/jobs/{jid}/{action}", handlers.job_action)
    app.router.add_delete("/api/jobs/{jid}", handlers.delete_job)
    app.router.add_delete("/api/jobs", handlers.delete_all_jobs)
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
    app.router.add_post("/api/audio/transcribe", handlers.transcribe)
    app.router.add_get("/api/audio/silence", silence)
    app.router.add_get("/api/conversations/{sid}/speech/{mid}", speech)
    app.router.add_get("/api/settings", get_settings)
    app.router.add_patch("/api/settings", update_settings)
    app.router.add_get("/api/settings/voice", handlers.voice_settings)
    app.router.add_put("/api/settings/voice", handlers.voice_settings)
    app.router.add_post("/api/settings/voice/test", handlers.voice_test)
    app.router.add_get("/api/settings/servers", get_servers)
    app.router.add_put("/api/settings/servers/{server_id}", update_server)
    app.router.add_post("/api/settings/servers/{server_id}/discover", discover_server_models)
    app.router.add_put("/api/settings/servers/{server_id}/models", update_server_models)
    app.router.add_get("/api/about", about)
    app.router.add_post("/api/service/restart-roost", restart_roost)
    app.router.add_get("/", static)
    app.router.add_get("/{name}", static)
    return app
