"""Exercise the loopback demo's owner and Hermes integration flows."""

import asyncio
import json
import secrets
import os
import socket
import tempfile
from unittest.mock import patch
from pathlib import Path

from aiohttp import ClientSession, CookieJar, web

from demo import mock_hermes, DEMO_PASSWORD, HERMES_PASSWORD
from roost.server import create_app

from roost.auth import make_password_hash

BASE = "http://127.0.0.1:8787"
PASSWORD_FILE = Path(__file__).resolve().parents[1] / ".roost-demo-state" / "password.hash"
HEADERS = {"Origin": BASE, "X-Roost-Request": "1"}


async def request(client, method, path, status=200, body=None):
    async with client.request(method, BASE + path, headers=HEADERS, json=body) as response:
        data = await response.json()
        assert response.status == status, (path, response.status, data)
        return data


async def reply_contains(client, sid, text):
    """Waits for the demo's saved reply to the latest message."""
    for _ in range(100):
        rows = (await request(client, "GET", f"/api/conversations/{sid}"))["messages"]
        if rows and rows[-1]["role"] == "assistant" and text in rows[-1]["content"]:
            return
        await asyncio.sleep(0.1)
    raise AssertionError((sid, text))


async def sign_in(device, password):
    """Sign in as a device behind Tailscale Serve, which forwards the device's address."""
    async with ClientSession() as client:
        async with client.post(BASE + "/api/login", headers={**HEADERS, "X-Forwarded-For": device},
                               json={"password": password}) as response:
            return response.status


async def sign_in_checks():
    # Fresh device addresses each run, so the smoke check can be repeated against one demo.
    # Each run still adds six failures toward the overall limit of twenty per five minutes.
    net = f"100.64.{secrets.randbelow(250)}."
    # Five wrong passwords lock that device only; another device still signs in.
    for _ in range(5):
        assert await sign_in(net + "1", "wrong password") == 401
    assert await sign_in(net + "1", "roost-demo-only") == 429
    assert await sign_in(net + "2", "roost-demo-only") == 200
    # A new password applies at the next sign-in, without restarting Roost.
    original = PASSWORD_FILE.read_text(encoding="utf-8")
    try:
        PASSWORD_FILE.write_text(make_password_hash("a brand new password") + "\n", encoding="utf-8")
        assert await sign_in(net + "3", "a brand new password") == 200
        assert await sign_in(net + "4", "roost-demo-only") == 401
    finally:
        PASSWORD_FILE.write_text(original, encoding="utf-8")


async def checks():
    async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
        async with client.get(BASE + "/") as response:
            assert response.status == 200
            assert "demo-banner" not in await response.text()
        for asset in ("/app.js", "/style.css", "/manifest.webmanifest", "/icon-192.png", "/icon-512.png", "/sw.js",
                      "/apple-touch-icon.png"):
            async with client.get(BASE + asset) as response:
                assert response.status == 200, asset
        await request(client, "GET", "/api/conversations", 401)
        # Passkeys: sign-in challenges are public, adding one needs a session.
        assert (await request(client, "POST", "/api/passkeys/login/options"))["userVerification"] == "required"
        await request(client, "POST", "/api/passkeys/register/options", 401)
        assert (await request(client, "GET", "/api/me"))["passkeys"] is False
        async with client.post(BASE + "/api/login", headers={"Origin": "http://invalid.example", "X-Roost-Request": "1"},
                               json={"password": "roost-demo-only"}) as response:
            assert response.status == 403
        login = await request(client, "POST", "/api/login", body={"password": "roost-demo-only"})
        assert login["demo"] is True
        conversations = await request(client, "GET", "/api/conversations")
        assert any(row.get("source") == "desktop" for row in conversations["sessions"])
        transcript = await request(client, "GET", "/api/conversations/roost_benchmark")
        assert len(transcript["messages"]) == 4
        # Only the authenticated Hermes plugin can attach numeric whole-reply measurements.
        measured = next(row for row in transcript["messages"] if row["role"] == "assistant")
        body = {"session_id": "roost_benchmark", "message_id": measured["id"],
                "input_tokens": 1200, "output_tokens": 240, "model_seconds": 6}
        token = (PASSWORD_FILE.parent / "delivery-token").read_text(encoding="utf-8").strip()
        async with client.post(BASE + "/api/hermes/reply-usage", json=body) as response:
            assert response.status == 401
        async with client.post(BASE + "/api/hermes/reply-usage", json=body, headers={"Authorization": "Bearer " + token}) as response:
            assert response.status == 200
        usage_rows = (await request(client, "GET", "/api/conversations/roost_benchmark"))["messages"]
        assert next(row for row in usage_rows if row["id"] == measured["id"])["roost_usage"]["output_tokens"] == 240
        assert all("roost_usage" not in row for row in usage_rows if row["role"] == "user")
        # Voice: a recording becomes text, and a reply is read aloud with byte ranges.
        heard = await request(client, "POST", "/api/audio/transcribe", body={"data_url": "data:audio/mp4;base64,AAAA", "mime_type": "audio/mp4"})
        assert heard["transcript"].startswith("This is the demo transcript")
        reply = next(row for row in transcript["messages"] if row["role"] == "assistant")
        async with client.get(BASE + f"/api/conversations/roost_benchmark/speech/{reply['id']}", headers={"Range": "bytes=0-3"}) as response:
            assert response.status == 206 and await response.read() == b"RIFF"

        created = await request(client, "POST", "/api/conversations", 201, {"text": "Hello from the smoke check"})
        sid = created["session_id"]
        # The reply streams while it is written: thoughts, then text, then the end.
        assert (await request(client, "GET", f"/api/conversations/{sid}"))["live"] is True
        streamed = {"thoughts": "", "text": ""}
        async with client.get(BASE + f"/api/conversations/{sid}/stream") as response:
            assert response.status == 200 and response.content_type == "text/event-stream"
            async for line in response.content:
                if line.startswith(b"data: "):
                    event = json.loads(line[6:])
                    if event.get("reset"):
                        assert event["user"] == "Hello from the smoke check"
                    for field in streamed:
                        streamed[field] += event.get(field, "")
                    if event.get("done"):
                        break
        assert streamed["thoughts"].startswith("The owner sent a message")
        assert "local demo reply" in streamed["text"]
        for _ in range(20):
            await asyncio.sleep(0.2)
            transcript = await request(client, "GET", f"/api/conversations/{sid}")
            if len(transcript["messages"]) >= 2:
                break
        assert len(transcript["messages"]) >= 2
        assert "local demo reply" in transcript["messages"][-1]["content"]
        # Hold a harmless demo turn at approval so follow-up admission cannot race its completion.
        overlapping = await request(client, "POST", "/api/conversations", 201, {"text": "/approval-demo"})
        overlap_sid = overlapping["session_id"]
        path = f"/api/conversations/{overlap_sid}"
        for _ in range(20):
            waiting = (await request(client, "GET", path + "/approvals"))["approvals"]
            if waiting:
                break
            await asyncio.sleep(0.05)
        assert waiting
        await request(client, "POST", path + "/messages", body={"text": "Queued follow up"})
        snapshot = await request(client, "GET", path)
        assert snapshot["live"] is False, "A queued acknowledgement must not own the first turn's stream."
        assert not any(row["content"] == "Queued follow up" for row in snapshot["messages"])
        await request(client, "POST", path + "/approvals", body={"request_id": waiting[0]["request_id"], "choice": "once"})
        for _ in range(80):
            snapshot = await request(client, "GET", path)
            if len(snapshot["messages"]) == 4:
                break
            await asyncio.sleep(0.05)
        assert [row["role"] for row in snapshot["messages"]] == ["user", "assistant", "user", "assistant"]
        assert [row["content"] for row in snapshot["messages"] if row["role"] == "user"] == ["/approval-demo", "Queued follow up"]
        await request(client, "DELETE", path, body={"confirm": True})

        # A native approval request must survive browser reconnect and resolve once only.
        for choice in ("once", "deny"):
            demo = await request(client, "POST", "/api/conversations", 201, {"text": "/approval-demo"})
            approval_sid = demo["session_id"]
            approval_path = f"/api/conversations/{approval_sid}/approvals"
            for _ in range(20):
                pending = (await request(client, "GET", approval_path))["approvals"]
                if pending:
                    break
                await asyncio.sleep(0.05)
            assert pending, "The approval must be visible after resuming the conversation."
            decision = {"request_id": pending[0]["request_id"], "choice": choice}
            assert (await request(client, "POST", approval_path, body=decision))["resolved"] is True
            await asyncio.sleep(0.05)
            await request(client, "POST", approval_path, 409, decision)
            await request(client, "DELETE", f"/api/conversations/{approval_sid}", body={"confirm": True})

        # Instructions ride once with the next message; the export has the whole conversation.
        await request(client, "PUT", f"/api/conversations/{sid}/settings", body={"instructions": "Be brief."})
        await request(client, "POST", f"/api/conversations/{sid}/messages", body={"text": "And one more"})
        async with client.get(BASE + f"/api/conversations/{sid}/export") as response:
            assert response.status == 200 and response.headers["Content-Disposition"].startswith("attachment;")
            exported = await response.json()
        sent = [row["content"] for row in exported["messages"] if row["role"] == "user"]
        assert exported["instructions"] == "Be brief." and sent[-1].endswith("\n\nAnd one more") and "Be brief." in sent[-1]

        # Thinking: chosen per conversation, set in Hermes with each message, and restored by Default.
        assert (await request(client, "GET", f"/api/conversations/{sid}/settings"))["reasoning"] == "default"
        await request(client, "PUT", f"/api/conversations/{sid}/settings", 400, {"reasoning": "extreme"})
        settings = await request(client, "PUT", f"/api/conversations/{sid}/settings", body={"reasoning": "low"})
        assert settings == {"instructions": "Be brief.", "reasoning": "low"}
        await request(client, "POST", f"/api/conversations/{sid}/messages", body={"text": "Think less"})
        await reply_contains(client, sid, "Thinking for this conversation: low.")
        await request(client, "PUT", f"/api/conversations/{sid}/settings", body={"reasoning": "default"})
        await request(client, "POST", f"/api/conversations/{sid}/messages", body={"text": "Back to normal"})
        await reply_contains(client, sid, "Thinking for this conversation: medium.")
        assert (await request(client, "GET", f"/api/conversations/{sid}/settings"))["reasoning"] == "default"
        thinking = await request(client, "POST", "/api/conversations", 201, {"text": "Quick one", "reasoning": "none"})
        await reply_contains(client, thinking["session_id"], "Thinking for this conversation: none.")
        assert (await request(client, "GET", f"/api/conversations/{thinking['session_id']}/settings"))["reasoning"] == "none"
        await request(client, "POST", "/api/conversations", 400, {"text": "Quick one", "reasoning": "max"})

        await request(client, "PATCH", f"/api/conversations/{sid}", body={"title": "Smoke conversation"})
        await request(client, "PATCH", f"/api/conversations/{sid}", 400, {"title": "Benchmark at 96K"})
        await request(client, "DELETE", f"/api/conversations/{sid}", 400, {"confirm": 1})
        await request(client, "DELETE", f"/api/conversations/{sid}", body={"confirm": True})
        remaining = await request(client, "GET", "/api/conversations")
        assert not any(row.get("id") == sid for row in remaining["sessions"])

        jobs = await request(client, "GET", "/api/jobs")
        assert len(jobs) >= 2
        for job in jobs:
            runs = await request(client, "GET", f"/api/jobs/{job['id']}/runs")
            for run in runs["runs"]:
                transcript = await request(client, "GET", f"/api/conversations/{run['id']}")
                assert transcript["messages"], "A demo run must open a nonempty conversation."
        new_job = await request(client, "POST", "/api/jobs", 201, {
            "name": "Demo check", "prompt": "Say hello", "schedule": "0 9 * * *", "deliver": "local",
        })
        jid = new_job["id"]
        await request(client, "PUT", f"/api/jobs/{jid}", body={"name": "Demo check updated"})
        await request(client, "POST", f"/api/jobs/{jid}/pause")
        paused = await request(client, "GET", f"/api/jobs/{jid}")
        assert paused["enabled"] is False and paused["schedule"]["expr"] == "0 9 * * *"
        await request(client, "POST", f"/api/jobs/{jid}/resume")
        await request(client, "POST", f"/api/jobs/{jid}/trigger")
        await request(client, "DELETE", f"/api/jobs/{jid}", 400, {"confirm": False})
        await request(client, "DELETE", f"/api/jobs/{jid}", body={"confirm": True})

        # The agent's name: Hermes's profile name until Roost's own setting overrides it.
        assert (await request(client, "GET", "/api/me"))["agent"] == "Kestrel"
        settings = await request(client, "PATCH", "/api/settings", body={"agent_name": "Robin", "accent": "ocean"})
        assert settings["agent_name"] == "Robin" and settings["hermes_agent_name"] == "Kestrel" and settings["accent"] == "ocean"
        assert (await request(client, "GET", "/api/me"))["agent"] == "Robin"
        await request(client, "PATCH", "/api/settings", body={"agent_name": ""})
        assert (await request(client, "GET", "/api/me"))["agent"] == "Kestrel"
        await request(client, "PATCH", "/api/settings", 400, {"accent": "neon"})
        await request(client, "PATCH", "/api/settings", 400, {"time_zone": "Mars/Olympus Mons"})
        await request(client, "PATCH", "/api/settings", body={"time_zone": "America/New_York"})
        assert (await request(client, "GET", "/api/me"))["time_zone"] == "America/New_York"
        assert (await request(client, "GET", "/api/passkeys"))["passkeys"] == []
        assert (await request(client, "POST", "/api/passkeys/register/options"))["authenticatorSelection"]["residentKey"] == "required"
        # Notifications: Roost's push key and an empty device list; presence accepts the open conversation.
        # Hermes's plugin delivers a job report with Roost's token; Settings offers the plugin.
        token = (PASSWORD_FILE.parent / "delivery-token").read_text(encoding="utf-8").strip()
        async with client.post(BASE + "/api/hermes/deliveries", headers={"Authorization": "Bearer " + token},
                               json={"text": "Cronjob Response: Smoke\n(job_id: job_smoke)\n---\n\nAll clear."}) as response:
            assert response.status == 201
            delivered = (await response.json())["id"]
        item = await request(client, "GET", f"/api/deliveries/{delivered}")
        assert (item["title"], item["job_id"], item["text"]) == ("Smoke", "job_smoke", "All clear.")
        assert (await request(client, "GET", "/api/hermes"))["last_delivery"] == item["created"]
        async with client.get(BASE + "/api/hermes/plugin.zip") as response:
            assert response.status == 200 and (await response.read())[:2] == b"PK"
        push = await request(client, "GET", "/api/push")
        assert push["public_key"] and push["devices"] == []
        await request(client, "POST", "/api/presence", body={"conversation": "roost_benchmark"})
        about = await request(client, "GET", "/api/about")
        assert about["hermes"]["reachable"] and about["hermes"]["version"] == "demo" and "hermes_home" not in about["hermes"]
        assert about["roost"]["version"] and about["stats"]["state_bytes"] > 0 and about["stats"]["disk_total_bytes"] > 0
        await request(client, "PUT", "/api/settings/soul", body={"content": "Be brief."})
        assert (await request(client, "GET", "/api/settings/soul"))["content"] == "Be brief."
        await request(client, "POST", "/api/service/restart-agent")
        await request(client, "DELETE", "/api/jobs", 400, {"confirm": True})
        assert (await request(client, "DELETE", "/api/jobs", body={"confirm": "DELETE"}))["deleted"] >= 2
        assert await request(client, "GET", "/api/jobs") == []
        assert (await request(client, "DELETE", "/api/conversations", body={"confirm": "DELETE"}))["deleted"] >= 1
        left = await request(client, "GET", "/api/conversations")
        assert not left["sessions"] and left["total"] == 0
        await request(client, "POST", "/api/logout")
        await request(client, "GET", "/api/jobs", 401)
    await sign_in_checks()
    print("PASS: static assets, origin guard, sign-in, per-device lockout, password change without restart, seeded conversations, authenticated reply measurements, voice, chat turns, streamed replies, busy-session follow-ups, approval reconnect/allow/deny/duplicate rejection, conversation instructions and export, per-conversation thinking, rename/duplicate refusal, linked run transcripts, Jobs CRUD/actions, confirmed deletion, agent name precedence, accent, time zone, About, passkey routes, notification settings and presence, Hermes deliveries and the plugin download, SOUL, delete all, agent restart, sign-out")


async def main():
    global BASE, PASSWORD_FILE, HEADERS
    # Reserve sockets rather than guessing a port or accidentally testing an existing demo.
    with tempfile.TemporaryDirectory(prefix="roost-smoke-") as directory, socket.socket() as upstream_socket, socket.socket() as roost_socket:
        upstream_socket.bind(("127.0.0.1", 0))
        roost_socket.bind(("127.0.0.1", 0))
        state = Path(directory)
        PASSWORD_FILE = state / "password.hash"
        PASSWORD_FILE.write_text(make_password_hash(DEMO_PASSWORD))
        hermes_password = state / "hermes-password"
        hermes_password.write_text(HERMES_PASSWORD)
        BASE = f"http://127.0.0.1:{roost_socket.getsockname()[1]}"
        HEADERS = {"Origin": BASE, "X-Roost-Request": "1"}
        with patch.dict(os.environ, {
            "ROOST_STATE_DIR": str(state), "ROOST_PUBLIC_ORIGIN": BASE,
            "ROOST_HERMES_URL": f"http://127.0.0.1:{upstream_socket.getsockname()[1]}",
            "ROOST_HERMES_USERNAME": "demo", "ROOST_HERMES_PASSWORD_FILE": str(hermes_password),
            "ROOST_DEMO": "1", "ROOST_TRUST_FORWARDED": "0",
        }):
            upstream = web.AppRunner(mock_hermes(), access_log=None)
            roost = web.AppRunner(create_app(), access_log=None)
            try:
                await upstream.setup()
                await web.SockSite(upstream, upstream_socket).start()
                await roost.setup()
                await web.SockSite(roost, roost_socket).start()
                await checks()
            finally:
                await roost.cleanup()
                await upstream.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
