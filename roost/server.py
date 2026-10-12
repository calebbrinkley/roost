"""Owner-only Roost HTTP service. No dashboard credential reaches the browser."""

import asyncio
import os
import re
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from aiohttp import web
from yarl import URL

from .auth import Sessions, verify_password
from .hermes import Hermes, HermesError

STATIC = Path(__file__).with_name("static")
COOKIE = "roost_session"
ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
JOB_FIELDS = {"name", "prompt", "schedule", "deliver"}


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
            if request.path not in {"/api/login", "/api/me", "/api/health"}:
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


async def login(request: web.Request):
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
        failures.pop(address, None)
        response = web.json_response({"signed_in": True, "demo": request.app["demo"]})
        secured_cookie(response, request, request.app["sessions"].issue())
        return response
    finally:
        inflight[address] -= 1
        if not inflight[address]:
            del inflight[address]


async def logout(request: web.Request):
    request.app["sessions"].revoke(request.cookies.get(COOKIE, ""))
    response = web.json_response({"signed_in": False})
    response.del_cookie(COOKIE, path="/")
    return response


async def me(request: web.Request):
    return web.json_response({"signed_in": request.app["sessions"].valid(request.cookies.get(COOKIE, "")),
                              "demo": request.app["demo"], "agent": request.app["agent_name"]})


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
    # The agent's display name. The page shows it; it is not a secret.
    agent_name = os.environ.get("ROOST_AGENT_NAME", "").strip() or "Agent"
    if not agent_name or len(agent_name) > 40 or not agent_name.isprintable():
        raise ValueError("ROOST_AGENT_NAME must be 1 to 40 printable characters.")
    app["agent_name"] = agent_name
    app["trust_forwarded"] = os.environ.get("ROOST_TRUST_FORWARDED") == "1"
    app["secure_cookie"] = origin.startswith("https://")
    app["password_file"] = password_file
    app["login_attempts"] = {}
    app["login_inflight"] = {}
    app["sessions"] = Sessions(state / "sessions.sqlite3")
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
    app.router.add_get("/api/jobs", jobs)
    app.router.add_post("/api/jobs", create_job)
    app.router.add_get("/api/jobs/{jid}", get_job)
    app.router.add_get("/api/jobs/{jid}/runs", job_runs)
    app.router.add_put("/api/jobs/{jid}", update_job)
    app.router.add_post("/api/jobs/{jid}/{action}", job_action)
    app.router.add_delete("/api/jobs/{jid}", delete_job)
    app.router.add_get("/", static)
    app.router.add_get("/{name}", static)
    return app
