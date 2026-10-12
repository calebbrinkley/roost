"""Loopback-only Roost preview with a small, disposable Hermes stand-in."""

import asyncio
import os
import re
import secrets
import time
from pathlib import Path

from aiohttp import WSMsgType, web

from roost.auth import make_password_hash
from roost.server import create_app

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".roost-demo-state"
ROOST_PORT = 8787
HERMES_PORT = 18719
DEMO_PASSWORD = "roost-demo-only"
HERMES_PASSWORD = "example-demo-hermes-password"


def schedule(expr):
    """Hermes stores a parsed schedule; the editable form is `expr`."""
    return {"kind": "cron", "expr": expr, "display": expr}


def job_state(job, enabled):
    job.update(enabled=enabled, state="scheduled" if enabled else "paused")
    return job


def sample_data():
    now = time.time()
    sessions = [
        {"id": "roost_benchmark", "title": "Benchmark at 96K", "preview": "I made a one-time job for tonight.",
         "source": "roost", "last_active_at": now - 60},
        {"id": "desktop_stems", "title": "Stem split: Clair de Lune", "preview": "Both stems are ready. Want the instrumental in your vault?",
         "source": "desktop", "last_active_at": now - 86400},
        {"id": "terminal_ntfy", "title": "Fix the ntfy port", "preview": "Moved the listener to 2586.",
         "source": "terminal", "last_active_at": now - 2 * 86400},
        {"id": "roost_packing", "title": "Trip packing list", "preview": "Added rain shell and the travel adapter.",
         "source": "roost", "last_active_at": now - 4 * 86400},
        {"id": "cron_job_vault_sample", "title": "Vault tidy run", "source": "cron", "last_active_at": now - 3 * 86400},
        {"id": "cron_job_morning_sample", "title": "Morning briefing run", "source": "cron", "last_active_at": now - 7200},
    ]
    messages = {
        "roost_benchmark": [
            {"role": "user", "content": "Did the 96K benchmark finish? Does it still fit?"},
            {"role": "assistant", "content": "Sample report: it finished, and the model fits in this fictional demo.\n\n- Generation: 37 tok/s\n- Long-prompt check: passed\n\n```text\nprefill 93,551 tok   164.0 s\ngen     128 tok      29.5 tok/s\n```"},
            {"role": "user", "content": "Nice. Run the full one tonight after 11?"},
            {"role": "assistant", "content": "Done. I made a one-time job for 23:00, and I will message you when it finishes."},
        ],
        "desktop_stems": [
            {"role": "user", "content": "How did the stem split turn out?"},
            {"role": "assistant", "content": "Both stems are ready. Want the instrumental in your vault?"},
        ],
        "terminal_ntfy": [
            {"role": "user", "content": "Can you fix the ntfy port?"},
            {"role": "assistant", "content": "Moved the listener to 2586; containers drop the capability for low ports.", "reasoning": "The bind fails with permission denied. Rootless containers cannot bind below 1024, so moving to a high port is simpler than adding a capability."},
        ],
        "roost_packing": [
            {"role": "user", "content": "Add a rain shell to the packing list."},
            {"role": "assistant", "content": "Added rain shell and the travel adapter."},
        ],
        "cron_job_vault_sample": [
            {"role": "assistant", "content": "Two sample notes link to pages that no longer exist."},
        ],
        "cron_job_morning_sample": [
            {"role": "assistant", "content": "Morning briefing\n\n• Your project outline is the main focus today.\n• A short reading break fits after lunch.\n• The evening walk is still on the calendar."},
        ],
    }
    # Hermes stamps every message (epoch seconds); space each conversation's messages a few minutes apart.
    for session in sessions:
        thread = messages.get(session["id"], [])
        for back, message in enumerate(reversed(thread)):
            message["timestamp"] = session["last_active_at"] - back * 240
    jobs = [
        {"id": "job_morning", "name": "Morning brief", "prompt": "Give me a clear, friendly overview of today's priorities and calendar.",
         "schedule": schedule("0 7 * * 1-5"), "deliver": "local", "enabled": True, "state": "scheduled", "last_status": "ok"},
        {"id": "job_benchmark", "name": "Full benchmark", "prompt": "Run the full 96K benchmark and report the result.",
         "schedule": schedule("0 23 * * *"), "deliver": "local", "enabled": True, "state": "scheduled"},
        {"id": "job_vault", "name": "Vault tidy", "prompt": "Check notes for broken links and report what needs attention.",
         "schedule": schedule("0 20 * * 0"), "deliver": "local", "enabled": True, "state": "scheduled", "last_status": "blocked_config"},
        {"id": "job_backup", "name": "Backup reminder", "prompt": "Remind me to check the latest backup.",
         "schedule": schedule("0 9 1 * *"), "deliver": "local", "enabled": False, "state": "paused"},
    ]
    return sessions, messages, jobs


def mock_hermes():
    app = web.Application()
    sessions, messages, jobs = sample_data()
    tickets = set()
    # The profile's display name is where Hermes keeps the agent's name.
    profile = {"name": "default", "display_name": "Kestrel", "soul": "You are a calm, concise assistant."}

    def require(request):
        if request.cookies.get("demo_hermes") != "signed-in":
            raise web.HTTPUnauthorized()

    async def login(request):
        data = await request.json()
        if data.get("provider") != "basic" or data.get("username") != "demo" or data.get("password") != HERMES_PASSWORD:
            raise web.HTTPUnauthorized()
        response = web.json_response({"ok": True})
        response.set_cookie("demo_hermes", "signed-in", httponly=True)
        return response

    async def ticket(request):
        require(request)
        value = secrets.token_urlsafe(24)
        tickets.add(value)
        return web.json_response({"ticket": value, "ttl_seconds": 30})

    async def session_list(request):
        require(request)
        selected = [s for s in sessions if s["source"] != "cron"] if request.query.get("exclude_sources") == "cron" else sessions
        offset = int(request.query.get("offset", "0"))
        limit = int(request.query.get("limit", "100"))
        return web.json_response({"sessions": selected[offset:offset + limit], "total": len(selected), "limit": limit, "offset": offset})

    async def transcript(request):
        require(request)
        sid = request.match_info["sid"]
        if sid not in messages:
            raise web.HTTPNotFound()
        return web.json_response({"session_id": sid, "messages": messages[sid], "pagination": {"returned": len(messages[sid])}})

    async def rename_session(request):
        require(request)
        sid, title = request.match_info["sid"], (await request.json()).get("title", "")
        session = next((s for s in sessions if s["id"] == sid), None)
        if session is None:
            raise web.HTTPNotFound()
        # Hermes refuses a title another conversation already uses.
        if title and any(s["title"] == title and s["id"] != sid for s in sessions):
            raise web.HTTPBadRequest(text="Title already in use")
        session["title"] = title
        return web.json_response({"ok": True, "title": title})

    async def delete_session(request):
        require(request)
        sid = request.match_info["sid"]
        sessions[:] = [s for s in sessions if s["id"] != sid]
        messages.pop(sid, None)
        return web.json_response({"ok": True})

    async def list_jobs(request):
        require(request)
        return web.json_response(jobs)

    def find_job(request):
        for job in jobs:
            if job["id"] == request.match_info["jid"]:
                return job
        raise web.HTTPNotFound()

    async def get_job(request):
        require(request)
        return web.json_response(find_job(request))

    async def runs(request):
        require(request)
        job = find_job(request)
        if job["id"] == "job_morning":
            return web.json_response({"runs": [{"id": "cron_job_morning_sample", "source": "cron", "title": "Completed · Morning briefing", "started_at": time.time() - 7200,
                                                "preview": "Your project outline is the main focus today."}]})
        if job["id"] == "job_vault":
            return web.json_response({"runs": [{"id": "cron_job_vault_sample", "source": "cron", "title": "Vault tidy", "started_at": time.time() - 3 * 86400,
                                                "preview": "Two sample notes link to pages that no longer exist."}]})
        return web.json_response({"runs": []})

    async def create_job(request):
        require(request)
        data = await request.json()
        job = job_state({"id": "job_" + secrets.token_hex(4), **data, "schedule": schedule(data["schedule"])}, True)
        jobs.insert(0, job)
        return web.json_response(job)

    async def update_job(request):
        require(request)
        job = find_job(request)
        updates = (await request.json()).get("updates", {})
        if "schedule" in updates:
            updates["schedule"] = schedule(updates["schedule"])
        job.update(updates)
        return web.json_response(job)

    async def job_action(request):
        require(request)
        job = find_job(request)
        action = request.match_info["action"]
        if action == "pause":
            job_state(job, False)
        elif action == "resume":
            job_state(job, True)
        elif action != "trigger":
            raise web.HTTPNotFound()
        return web.json_response(job)

    async def delete_job(request):
        require(request)
        job = find_job(request)
        jobs.remove(job)
        return web.json_response({"deleted": True})

    async def ws(request):
        ticket_value = request.query.get("ticket")
        if ticket_value not in tickets:
            raise web.HTTPUnauthorized()
        tickets.remove(ticket_value)
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        await socket.send_json({"jsonrpc": "2.0", "method": "event", "params": {"type": "gateway.ready", "payload": {}}})
        async for message in socket:
            if message.type != WSMsgType.TEXT:
                continue
            frame = message.json()
            method, params = frame.get("method"), frame.get("params") or {}
            if method == "session.create":
                stored_id = "roost_" + secrets.token_hex(5)
                runtime_id = "runtime_" + secrets.token_hex(5)
                sessions.insert(0, {"id": stored_id, "title": "New conversation", "preview": "", "source": "roost", "last_active_at": time.time()})
                messages[stored_id] = []
                result = {"session_id": runtime_id, "stored_session_id": stored_id}
                app["runtime_map"][runtime_id] = stored_id
            elif method == "session.resume":
                stored_id = params.get("session_id")
                if stored_id not in messages:
                    await socket.send_json({"jsonrpc": "2.0", "id": frame.get("id"), "error": {"code": 404}})
                    continue
                runtime_id = "runtime_" + secrets.token_hex(5)
                app["runtime_map"][runtime_id] = stored_id
                result = {"session_id": runtime_id, "stored_session_id": stored_id}
            elif method == "prompt.submit":
                stored_id = app["runtime_map"].get(params.get("session_id"))
                if not stored_id:
                    await socket.send_json({"jsonrpc": "2.0", "id": frame.get("id"), "error": {"code": 404}})
                    continue
                text = params.get("text", "")
                messages[stored_id].append({"role": "user", "content": text, "timestamp": time.time()})
                session = next(s for s in sessions if s["id"] == stored_id)
                session["title"] = text[:42]
                session["preview"] = text[:80]
                session["last_active_at"] = time.time()

                async def reply(runtime_id, sid, prompt):
                    # Streams like Hermes: thoughts, then the reply, a few words at a time, then the saved row.
                    async def emit(kind, payload=None):
                        if not socket.closed:
                            await socket.send_json({"jsonrpc": "2.0", "method": "event", "params": {
                                "type": kind, "session_id": runtime_id, **({"payload": payload} if payload else {})}})
                    answer = (f"This is the local demo reply to: “{prompt}”\n\n"
                              "The agent's real answer will appear here when Roost is connected to Hermes.")
                    thoughts = "The owner sent a message to the demo. I'll answer with the demo reply and say where the real one comes from."
                    await emit("message.start")
                    for kind, text in (("reasoning.delta", thoughts), ("message.delta", answer)):
                        for word in re.findall(r"\S+\s*", text):
                            await asyncio.sleep(0.03)
                            await emit(kind, {"text": word})
                    if sid not in messages:
                        return  # The owner may delete a conversation while its demo reply is pending.
                    messages[sid].append({"role": "assistant", "timestamp": time.time(), "content": answer, "reasoning": thoughts})
                    next(s for s in sessions if s["id"] == sid)["last_active_at"] = time.time()
                    await emit("message.complete", {"text": answer, "status": "complete"})

                asyncio.create_task(reply(params.get("session_id"), stored_id, text))
                result = {"status": "accepted"}
            else:
                await socket.send_json({"jsonrpc": "2.0", "id": frame.get("id"), "error": {"code": 404}})
                continue
            await socket.send_json({"jsonrpc": "2.0", "id": frame.get("id"), "result": result})
        return socket

    async def active_profile(request):
        require(request)
        return web.json_response({"active": "default", "current": "default"})

    async def profiles(request):
        require(request)
        return web.json_response({"profiles": [{"name": "default", "display_name": profile["display_name"], "bot_title": ""}]})

    async def get_soul(request):
        require(request)
        if request.match_info["name"] != "default":
            raise web.HTTPNotFound()
        return web.json_response({"content": profile["soul"], "exists": True})

    async def put_soul(request):
        require(request)
        if request.match_info["name"] != "default":
            raise web.HTTPNotFound()
        profile["soul"] = (await request.json())["content"]
        return web.json_response({"ok": True})

    async def status(request):
        # Public in Hermes too; the real one also carries paths and PIDs that Roost drops.
        return web.json_response({"version": "demo", "release_date": "2026-09-24", "gateway_running": True,
                                  "gateway_state": "running", "active_sessions": 1, "hermes_home": "/demo/home"})

    async def restart_gateway(request):
        require(request)
        return web.json_response({"ok": True, "pid": 1, "name": "gateway-restart"})

    app["runtime_map"] = {}
    app.router.add_get("/api/profiles/active", active_profile)
    app.router.add_get("/api/profiles", profiles)
    app.router.add_get("/api/profiles/{name}/soul", get_soul)
    app.router.add_put("/api/profiles/{name}/soul", put_soul)
    app.router.add_post("/api/gateway/restart", restart_gateway)
    app.router.add_get("/api/status", status)
    app.router.add_post("/auth/password-login", login)
    app.router.add_post("/api/auth/ws-ticket", ticket)
    app.router.add_get("/api/sessions", session_list)
    app.router.add_get("/api/sessions/{sid}/messages", transcript)
    app.router.add_patch("/api/sessions/{sid}", rename_session)
    app.router.add_delete("/api/sessions/{sid}", delete_session)
    app.router.add_get("/api/cron/jobs", list_jobs)
    app.router.add_post("/api/cron/jobs", create_job)
    app.router.add_get("/api/cron/jobs/{jid}", get_job)
    app.router.add_get("/api/cron/jobs/{jid}/runs", runs)
    app.router.add_put("/api/cron/jobs/{jid}", update_job)
    app.router.add_post("/api/cron/jobs/{jid}/{action}", job_action)
    app.router.add_delete("/api/cron/jobs/{jid}", delete_job)
    app.router.add_get("/api/ws", ws)
    return app


async def main():
    STATE.mkdir(mode=0o700, exist_ok=True)
    os.chmod(STATE, 0o700)
    password_hash = STATE / "password.hash"
    if not password_hash.exists():
        password_hash.write_text(make_password_hash(DEMO_PASSWORD) + "\n", encoding="utf-8")
        os.chmod(password_hash, 0o600)
    hermes_password = STATE / "hermes-password.txt"
    hermes_password.write_text(HERMES_PASSWORD + "\n", encoding="utf-8")
    os.chmod(hermes_password, 0o600)

    os.environ.update({
        "ROOST_STATE_DIR": str(STATE),
        "ROOST_PUBLIC_ORIGIN": f"http://127.0.0.1:{ROOST_PORT}",
        "ROOST_HERMES_URL": f"http://127.0.0.1:{HERMES_PORT}",
        "ROOST_HERMES_USERNAME": "demo",
        "ROOST_HERMES_PASSWORD_FILE": str(hermes_password),
        "ROOST_DEMO": "1",
    })
    mock = web.AppRunner(mock_hermes(), access_log=None)
    roost = web.AppRunner(create_app(), access_log=None)
    await mock.setup()
    try:
        await web.TCPSite(mock, "127.0.0.1", HERMES_PORT).start()
        await roost.setup()
        await web.TCPSite(roost, "127.0.0.1", ROOST_PORT).start()
        print(f"Roost demo: http://127.0.0.1:{ROOST_PORT}", flush=True)
        print(f"Demo password: {DEMO_PASSWORD}", flush=True)
        await asyncio.Event().wait()
    finally:
        await roost.cleanup()
        await mock.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
