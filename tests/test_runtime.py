"""Runtime regressions using synthetic data and a local HTTP stand-in."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import CookieJar, WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from roost import server
from roost.__main__ import main as cli_main
from roost.auth import Sessions, make_password_hash
from roost.hermes import Hermes, HermesError


class ValidationTests(unittest.TestCase):
    def test_dot_segments_cannot_be_forwarded(self):
        for value in (".", ".."):
            with self.subTest(value=value), self.assertRaises(web.HTTPBadRequest):
                server.item_id(SimpleNamespace(match_info={"sid": value}), "sid")


    def test_uppercase_http_origin_cannot_bypass_loopback_binding(self):
        with patch.dict(os.environ, {"ROOST_PUBLIC_ORIGIN": "HTTP://localhost:8787"}), \
             patch("sys.argv", ["roost", "serve", "--host", "0.0.0.0"]), \
             patch("roost.__main__.web.run_app") as run, patch("sys.stderr"), self.assertRaises(SystemExit):
            cli_main()
        run.assert_not_called()


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_attempts_share_the_device_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            password_file = path / "password.hash"
            password_file.write_text(make_password_hash("example-demo-password"))
            sessions = Sessions(path / "sessions.sqlite3")
            app = {"login_attempts": {}, "login_inflight": {}, "password_file": password_file,
                   "sessions": sessions, "demo": False, "secure_cookie": False, "trust_forwarded": False}
            request = SimpleNamespace(app=app, remote="127.0.0.1", headers={}, content_type="application/json",
                                      json=AsyncMock(return_value={"password": "wrong"}))
            def delayed_verify(*args):
                import time
                time.sleep(0.04)
                return False
            async def attempt():
                try:
                    return (await server.login(request)).status
                except web.HTTPException as error:
                    return error.status
            try:
                with patch("roost.server.verify_password", delayed_verify):
                    statuses = await asyncio.gather(*(attempt() for _ in range(12)))
                self.assertEqual(statuses.count(401), server.DEVICE_FAILURES)
                self.assertEqual(statuses.count(429), 12 - server.DEVICE_FAILURES)
            finally:
                sessions.close()

    async def test_offset_is_bounded_before_integer_conversion(self):
        hermes = SimpleNamespace(request=AsyncMock(return_value={"sessions": []}))
        for value in ("9" * 5000, "١", "-1", "100001"):
            request = SimpleNamespace(query={"offset": value}, app={"hermes": hermes})
            with self.subTest(value=value[:10]), self.assertRaises(web.HTTPBadRequest):
                await server.conversations(request)
        hermes.request.assert_not_awaited()

    async def test_resume_uses_raw_identifier(self):
        hermes = SimpleNamespace(send_message=AsyncMock(return_value="chat:one"))
        request = SimpleNamespace(match_info={"sid": "chat:one"}, app={"hermes": hermes},
                                  content_type="application/json", json=AsyncMock(return_value={"text": "hello"}))
        await server.send_message(request)
        hermes.send_message.assert_awaited_once_with("hello", "chat:one")


    async def test_deletion_requires_a_boolean_confirmation(self):
        hermes = SimpleNamespace(request=AsyncMock(return_value={"ok": True}))
        for handler, name in ((server.delete_job, "jid"), (server.delete_conversation, "sid")):
            for value in (1, "true", False, None):
                request = SimpleNamespace(match_info={name: "one"}, app={"hermes": hermes},
                                          content_type="application/json", json=AsyncMock(return_value={"confirm": value}))
                with self.subTest(handler=handler.__name__, value=value), self.assertRaises(web.HTTPBadRequest):
                    await handler(request)
        hermes.request.assert_not_awaited()

    async def test_global_login_limit_counts_pending_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "password.hash"
            path.write_text(make_password_hash("example-demo-password"))
            app = {"login_attempts": {}, "login_inflight": {}, "password_file": path, "trust_forwarded": False}
            gate = asyncio.Event()
            async def read_file(*args, **kwargs):
                await gate.wait()
                return path.read_text()
            async def attempt(index):
                request = SimpleNamespace(app=app, remote=f"192.0.2.{index}", headers={}, content_type="application/json",
                                          json=AsyncMock(return_value={"password": "wrong"}))
                try:
                    return (await server.login(request)).status
                except web.HTTPException as error:
                    return error.status
            original_to_thread = asyncio.to_thread
            async def blocked_thread(function, *args, **kwargs):
                if function == path.read_text:
                    return await read_file()
                return await original_to_thread(function, *args, **kwargs)
            with patch("roost.server.asyncio.to_thread", blocked_thread):
                tasks = [asyncio.create_task(attempt(i)) for i in range(25)]
                await asyncio.sleep(0.02)
                self.assertEqual(sum(app["login_inflight"].values()), server.ALL_FAILURES)
                gate.set()
                statuses = await asyncio.gather(*tasks)
            self.assertEqual(statuses.count(401), server.ALL_FAILURES)
            self.assertEqual(statuses.count(429), 5)
            self.assertEqual(app["login_inflight"], {})


class HermesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.password = Path(self.directory.name) / "password"
        self.password.write_text("example-demo-password")
        self.response_body = b'{"ok": true}'
        self.chunked = False
        self.status = 200
        self.content_type = "application/json"
        async def login(request):
            return web.json_response({"ok": True})
        async def response(request):
            if not self.chunked:
                return web.Response(body=self.response_body, status=self.status, content_type=self.content_type)
            stream = web.StreamResponse(headers={"Content-Type": self.content_type})
            await stream.prepare(request)
            for start in range(0, len(self.response_body), 16384):
                try:
                    await stream.write(self.response_body[start:start + 16384])
                except ConnectionResetError:
                    break
            return stream
        app = web.Application()
        app.router.add_post("/auth/password-login", login)
        app.router.add_get("/api/data", response)
        self.upstream = TestServer(app)
        await self.upstream.start_server()
        self.hermes = Hermes(str(self.upstream.make_url("")), "demo", self.password)
        await self.hermes.start()

    async def asyncTearDown(self):
        await self.hermes.close()
        await self.upstream.close()
        self.directory.cleanup()

    async def test_invalid_json_becomes_safe_upstream_error(self):
        for body in (b'{broken', b'"not an object"', b'null', b'\xff'):
            self.response_body = body
            with self.subTest(body=body), self.assertRaises(HermesError) as caught:
                await self.hermes.request("GET", "/api/data")
            self.assertEqual(caught.exception.status, 502)

    async def test_chunked_response_is_bounded(self):
        self.chunked = True
        self.response_body = json.dumps({"data": "x" * (8 * 1024 * 1024)}).encode()
        with self.assertRaises(HermesError) as caught:
            await self.hermes.request("GET", "/api/data")
        self.assertEqual(caught.exception.status, 502)

    async def test_unreadable_password_is_a_safe_service_error(self):
        self.password.write_bytes(b"\xff")
        with self.assertRaises(HermesError) as caught:
            await self.hermes.request("GET", "/api/data")
        self.assertEqual(caught.exception.status, 503)

    async def test_valid_json_objects_and_lists(self):
        for value in ({"ok": True}, [{"id": "one"}]):
            self.response_body = json.dumps(value).encode()
            self.assertEqual(await self.hermes.request("GET", "/api/data"), value)

    async def test_malformed_rpc_frames_are_safe_errors(self):
        for frame in ([], {"id": 1, "result": []}, {"id": 1, "result": "bad"}):
            socket = SimpleNamespace(send_json=AsyncMock(), receive=AsyncMock(return_value=
                                     SimpleNamespace(type=WSMsgType.TEXT, json=lambda: frame)))
            with self.subTest(frame=frame), self.assertRaises(HermesError) as caught:
                await Hermes._rpc_on_socket(socket, 1, "session.create", {})
            self.assertEqual(caught.exception.status, 502)


    async def test_rpc_events_do_not_extend_the_deadline(self):
        async def receive():
            await asyncio.sleep(0.001)
            return SimpleNamespace(type=WSMsgType.TEXT, json=lambda: {"method": "event"})
        socket = SimpleNamespace(send_json=AsyncMock(), receive=receive)
        with patch("roost.hermes.RPC_TIMEOUT", 0.02), self.assertRaises(asyncio.TimeoutError):
            await Hermes._rpc_on_socket(socket, 1, "session.create", {})


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = Path(self.directory.name)
        (self.state / "password.hash").write_text(make_password_hash("example-demo-password"))
        with patch.dict(os.environ, {
            "ROOST_PUBLIC_ORIGIN": "https://roost.example.invalid", "ROOST_STATE_DIR": str(self.state),
            "ROOST_HERMES_URL": "http://127.0.0.1:1", "ROOST_HERMES_USERNAME": "demo",
            "ROOST_HERMES_PASSWORD_FILE": str(self.state / "upstream-password"),
        }):
            self.app = server.create_app()
        self.app["hermes"] = SimpleNamespace(start=AsyncMock(), close=AsyncMock(), request=AsyncMock(return_value=[]))
        self.client = TestClient(TestServer(self.app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.headers = {"Origin": "https://roost.example.invalid", "X-Roost-Request": "1"}

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()

    async def test_login_cookie_and_revocation(self):
        response = await self.client.post("/api/login", headers=self.headers, json={"password": "example-demo-password"})
        self.assertEqual(response.status, 200)
        cookie = response.cookies[server.COOKIE]
        self.assertTrue(cookie["secure"])
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Strict")
        token = cookie.value
        response = await self.client.get("/api/jobs", cookies={server.COOKIE: token})
        self.assertEqual(response.status, 200)
        response = await self.client.post("/api/logout", headers=self.headers, cookies={server.COOKIE: token})
        self.assertEqual(response.status, 200)
        response = await self.client.get("/api/jobs", cookies={server.COOKIE: token})
        self.assertEqual(response.status, 401)

    async def test_origin_and_auth_guards_and_error_headers(self):
        for headers in ({}, {**self.headers, "Origin": "https://other.example.invalid"}):
            response = await self.client.post("/api/login", headers=headers, json={"password": "example-demo-password"})
            self.assertEqual(response.status, 403)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        response = await self.client.get("/api/jobs")
        self.assertEqual(response.status, 401)
        self.app["hermes"].request.assert_not_awaited()

    async def test_malformed_and_oversized_bodies_are_json_errors(self):
        for body, content_type, expected in (("{broken", "application/json", 400),
                                              ("[]", "application/json", 400),
                                              ('{}', "text/plain", 400),
                                              ('{"password":"' + "x" * 70000 + '"}', "application/json", 413)):
            response = await self.client.post("/api/login", headers={**self.headers, "Content-Type": content_type}, data=body)
            self.assertEqual(response.status, expected)
            self.assertIn("error", await response.json())
            self.assertEqual(self.app["login_inflight"], {})

    async def signed_in(self):
        response = await self.client.post("/api/login", headers=self.headers, json={"password": "example-demo-password"})
        self.assertEqual(response.status, 200)
        # The cookie is Secure and this test server is plain HTTP, so hand it back explicitly.
        self.client.session.cookie_jar.update_cookies({server.COOKIE: response.cookies[server.COOKIE].value})

    async def test_restart_roost_answers_then_exits_for_the_service_manager(self):
        await self.signed_in()
        exits = []
        self.app["exit"] = exits.append
        response = await self.client.post("/api/service/restart-roost", headers=self.headers)
        self.assertEqual(response.status, 200)
        await asyncio.sleep(0.7)
        # Non-zero, so systemd's Restart=on-failure starts Roost again.
        self.assertEqual(exits, [server.RESTART_EXIT])
        self.assertNotEqual(server.RESTART_EXIT, 0)

    async def test_settings_are_validated_and_survive_a_corrupt_file(self):
        await self.signed_in()
        for body in ({"agent_name": "x" * 41}, {"agent_name": "line\nbreak"}, {"accent": "neon"}, {"theme": "dark"}, {},
                     {"time_zone": "../etc"}, {"time_zone": "America/" + "x" * 70}, {"time_zone": 5}):
            response = await self.client.patch("/api/settings", headers=self.headers, json=body)
            self.assertEqual(response.status, 400, body)
        (self.state / "settings.json").write_text("{not json")
        self.assertEqual(server.load_settings(self.app), {"agent_name": "", "accent": "forest", "time_zone": ""})
        response = await self.client.patch("/api/settings", headers=self.headers, json={"agent_name": "  Robin  ", "accent": "rose"})
        self.assertEqual(response.status, 200)
        self.assertEqual(server.load_settings(self.app), {"agent_name": "Robin", "accent": "rose", "time_zone": ""})
        self.assertEqual(oct((self.state / "settings.json").stat().st_mode & 0o777), "0o600")

    async def test_delete_all_needs_the_typed_word_and_stops_on_rows_hermes_keeps(self):
        await self.signed_in()
        for confirm in (True, "delete", None):
            response = await self.client.delete("/api/conversations", headers=self.headers, json={"confirm": confirm})
            self.assertEqual(response.status, 400)
        self.app["hermes"].request.assert_not_awaited()
        # Hermes acknowledges every delete but keeps listing the same row: Roost must not loop.
        self.app["hermes"].request = AsyncMock(side_effect=lambda method, path, *rest: (
            {"sessions": [{"id": "stuck"}]} if method == "GET" else {"ok": True}))
        response = await self.client.delete("/api/conversations", headers=self.headers, json={"confirm": "DELETE"})
        self.assertEqual((response.status, await response.json()), (200, {"deleted": 1}))

    async def test_profile_names_from_hermes_cannot_reach_other_routes(self):
        await self.signed_in()
        calls = []

        async def request(method, path, *rest):
            calls.append((method, path))
            return {"current": "../config"} if path == "/api/profiles/active" else {"content": "soul"}
        self.app["hermes"].request = request
        response = await self.client.get("/api/settings/soul")
        self.assertEqual(response.status, 200)
        self.assertIn(("GET", "/api/profiles/default/soul"), calls)

    async def test_agent_name_is_asked_of_hermes_right_after_boot(self):
        await self.signed_in()

        async def request(method, path, *rest):
            if path == "/api/profiles/active":
                return {"current": "default"}
            return {"profiles": [{"name": "default", "display_name": "Kestrel"}]}
        self.app["hermes"].request = request
        # A host that has just booted reports a small monotonic clock.
        with patch.object(server.time, "monotonic", return_value=5.0):
            response = await self.client.get("/api/me")
        self.assertEqual((await response.json())["agent"], "Kestrel")

    async def test_about_reports_service_stats_and_only_public_hermes_fields(self):
        await self.signed_in()
        self.app["hermes"].request = AsyncMock(return_value={"version": "2026.9.24", "gateway_running": True,
                                                             "hermes_home": "/private/path", "gateway_pid": 42})
        response = await self.client.get("/api/about")
        data = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(data["hermes"]["version"], "2026.9.24")
        self.assertNotIn("hermes_home", data["hermes"]); self.assertNotIn("gateway_pid", data["hermes"])
        self.assertGreater(data["stats"]["disk_total_bytes"], 0)
        # Roost's own code (with its fonts) is counted, not the shared volume's usage.
        self.assertGreater(data["stats"]["app_bytes"], 100_000)
        self.assertLess(data["stats"]["app_bytes"], 50_000_000)
        self.assertEqual(data["roost"]["revision"], "")
        self.app["hermes"].request = AsyncMock(side_effect=HermesError(503, "Hermes is unavailable."))
        self.assertEqual((await (await self.client.get("/api/about")).json())["hermes"], {"reachable": False})

    async def test_ipv6_loopback_origin_and_invalid_port(self):
        config = {"ROOST_STATE_DIR": str(self.state), "ROOST_HERMES_URL": "http://127.0.0.1:1",
                  "ROOST_HERMES_USERNAME": "demo", "ROOST_HERMES_PASSWORD_FILE": str(self.state / "password")}
        with patch.dict(os.environ, {**config, "ROOST_PUBLIC_ORIGIN": "http://[::1]:8787"}):
            app = server.create_app()
            self.assertFalse(app["secure_cookie"])
            app["sessions"].close()
        with patch.dict(os.environ, {**config, "ROOST_PUBLIC_ORIGIN": "https://roost.example.invalid:bad"}), self.assertRaises(ValueError):
            server.create_app()


    async def test_public_origin_matches_browser_normalization(self):
        config = {"ROOST_STATE_DIR": str(self.state), "ROOST_HERMES_URL": "http://127.0.0.1:1",
                  "ROOST_HERMES_USERNAME": "demo", "ROOST_HERMES_PASSWORD_FILE": str(self.state / "password")}
        with patch.dict(os.environ, {**config, "ROOST_PUBLIC_ORIGIN": "HTTPS://ROOST.EXAMPLE.INVALID:443/"}):
            app = server.create_app()
            self.assertEqual(app["origin"], "https://roost.example.invalid")
            self.assertTrue(app["secure_cookie"])
            app["sessions"].close()
        for origin in ("https://:8787", "https://example.invalid bad"):
            with patch.dict(os.environ, {**config, "ROOST_PUBLIC_ORIGIN": origin}), self.assertRaises(ValueError):
                server.create_app()


if __name__ == "__main__":
    unittest.main()
