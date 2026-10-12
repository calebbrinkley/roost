"""Exercise the loopback demo's owner and Hermes integration flows."""

import asyncio
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
        for asset in ("/app.js", "/style.css", "/manifest.webmanifest", "/icon.svg"):
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

        created = await request(client, "POST", "/api/conversations", 201, {"text": "Hello from the smoke check"})
        sid = created["session_id"]
        for _ in range(20):
            await asyncio.sleep(0.2)
            transcript = await request(client, "GET", f"/api/conversations/{sid}")
            if len(transcript["messages"]) >= 2:
                break
        assert len(transcript["messages"]) >= 2
        assert "local demo reply" in transcript["messages"][-1]["content"]
        await request(client, "POST", f"/api/conversations/{sid}/messages", body={"text": "And one more"})

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
    print("PASS: static assets, origin guard, sign-in, per-device lockout, password change without restart, seeded conversations, chat turns, rename/duplicate refusal, linked run transcripts, Jobs CRUD/actions, confirmed deletion, agent name precedence, accent, time zone, About, passkey routes, SOUL, delete all, agent restart, sign-out")


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
