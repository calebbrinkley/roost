"""Exercise the loopback demo's owner and Hermes integration flows."""

import asyncio

from aiohttp import ClientSession, CookieJar

BASE = "http://127.0.0.1:8787"
HEADERS = {"Origin": BASE, "X-Roost-Request": "1"}


async def request(client, method, path, status=200, body=None):
    async with client.request(method, BASE + path, headers=HEADERS, json=body) as response:
        data = await response.json()
        assert response.status == status, (path, response.status, data)
        return data


async def main():
    async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
        async with client.get(BASE + "/") as response:
            assert response.status == 200
            assert "demo-banner" not in await response.text()
        for asset in ("/app.js", "/style.css", "/manifest.webmanifest", "/icon.svg"):
            async with client.get(BASE + asset) as response:
                assert response.status == 200, asset
        await request(client, "GET", "/api/conversations", 401)
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

        jobs = await request(client, "GET", "/api/jobs")
        assert len(jobs) >= 2
        new_job = await request(client, "POST", "/api/jobs", 201, {
            "name": "Demo check", "prompt": "Say hello", "schedule": "0 9 * * *", "deliver": "local",
        })
        jid = new_job["id"]
        await request(client, "PUT", f"/api/jobs/{jid}", body={"name": "Demo check updated"})
        await request(client, "POST", f"/api/jobs/{jid}/pause")
        paused = await request(client, "GET", f"/api/jobs/{jid}")
        assert paused["paused"] is True
        await request(client, "POST", f"/api/jobs/{jid}/resume")
        await request(client, "POST", f"/api/jobs/{jid}/trigger")
        await request(client, "DELETE", f"/api/jobs/{jid}", 400, {"confirm": False})
        await request(client, "DELETE", f"/api/jobs/{jid}", body={"confirm": True})
        await request(client, "POST", "/api/logout")
        await request(client, "GET", "/api/jobs", 401)
    print("PASS: static assets, origin guard, sign-in, seeded conversations, chat turns, Jobs CRUD/actions, confirmed deletion, sign-out")


if __name__ == "__main__":
    asyncio.run(main())
