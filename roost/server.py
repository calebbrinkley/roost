"""Owner-only Roost HTTP service. No dashboard credential reaches the browser."""

import asyncio
import os
import re
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from aiohttp import web

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


def item_id(request: web.Request, name: str) -> str:
    value = request.match_info[name]
    if not ID.fullmatch(value):
        raise bad("Invalid identifier.")
    return quote(value, safe="")


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


async def login(request: web.Request):
    address = request.remote or "unknown"
    now = time.monotonic()
    attempts = [t for t in request.app["login_attempts"].get(address, []) if now - t < 300]
    if len(attempts) >= 5:
        raise web.HTTPTooManyRequests(text="Try again in a few minutes.")
    data = await json_body(request)
    password = data.get("password")
    if not isinstance(password, str) or len(password) > 1024:
        raise bad("Password is required.")
    encoded = request.app["password_hash"]
    if not await asyncio.to_thread(verify_password, password, encoded):
        attempts.append(now)
        request.app["login_attempts"][address] = attempts
        raise web.HTTPUnauthorized(text="Incorrect password.")
    request.app["login_attempts"].pop(address, None)
    response = web.json_response({"signed_in": True, "demo": request.app["demo"]})
    secured_cookie(response, request, request.app["sessions"].issue())
    return response


async def logout(request: web.Request):
    request.app["sessions"].revoke(request.cookies.get(COOKIE, ""))
    response = web.json_response({"signed_in": False})
    response.del_cookie(COOKIE, path="/")
    return response


async def me(request: web.Request):
    return web.json_response({"signed_in": request.app["sessions"].valid(request.cookies.get(COOKIE, "")),
                              "demo": request.app["demo"]})


async def health(request: web.Request):
    return web.json_response({"status": "ready"})


async def conversations(request: web.Request):
    page = request.query.get("offset", "0")
    if not page.isdecimal() or int(page) > 100000:
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
    sid = item_id(request, "sid")
    data = await json_body(request)
    text = body_text(data.get("text"), "Message", 16000)
    stored = await request.app["hermes"].send_message(text, sid)
    return web.json_response({"session_id": stored})


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
    if data != {"confirm": True}:
        raise bad("Confirm job deletion.")
    result = await request.app["hermes"].request("DELETE", f"/api/cron/jobs/{jid}")
    return web.json_response(result)


async def static(request: web.Request):
    name = request.match_info.get("name") or "index.html"
    if name not in {"index.html", "app.js", "theme.js", "style.css", "manifest.webmanifest", "icon.svg"}:
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
    if (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"})) or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password or not parsed.netloc:
        raise ValueError("ROOST_PUBLIC_ORIGIN must be HTTPS or a local HTTP origin.")
    state = Path(os.environ.get("ROOST_STATE_DIR", "./.roost-state"))
    password_hash = (state / "password.hash").read_text(encoding="utf-8").strip()
    app = web.Application(middlewares=[guard], client_max_size=64 * 1024)
    app["origin"] = origin
    app["demo"] = os.environ.get("ROOST_DEMO") == "1"
    app["secure_cookie"] = origin.startswith("https://")
    app["password_hash"] = password_hash
    app["login_attempts"] = {}
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
