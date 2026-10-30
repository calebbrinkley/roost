"""Runtime regressions using synthetic data and a local HTTP stand-in."""

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import CookieJar, web
from aiohttp.test_utils import TestClient, TestServer

from roost import server
from roost.__main__ import main as cli_main
from roost.auth import Sessions, make_password_hash
from roost.push import unb64url


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


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = Path(self.directory.name)
        (self.state / "password.hash").write_text(make_password_hash("example-demo-password"))
        with patch.dict(os.environ, {
            "ROOST_PUBLIC_ORIGIN": "https://roost.example.invalid", "ROOST_STATE_DIR": str(self.state),
        }):
            self.app = server.create_app()
        self.client = TestClient(TestServer(self.app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.headers = {"Origin": "https://roost.example.invalid", "X-Roost-Request": "1"}

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()


    async def test_push_routes_need_a_session_and_validate_input(self):
        response = await self.client.get("/api/push")
        self.assertEqual(response.status, 401)
        response = await self.client.post("/api/login", headers=self.headers, json={"password": "example-demo-password"})
        self.assertEqual(response.status, 200)
        cookies = {server.COOKIE: response.cookies[server.COOKIE].value}
        response = await self.client.get("/api/push", cookies=cookies)
        data = await response.json()
        self.assertEqual((len(unb64url(data["public_key"])), data["devices"]), (65, []))
        for body in ({"conversation": "../x"}, {"conversation": 1}, {}, {"conversation": None, "extra": 1}):
            response = await self.client.post("/api/presence", headers=self.headers, json=body, cookies=cookies)
            self.assertEqual(response.status, 400, body)
        response = await self.client.post("/api/presence", headers=self.headers, json={"conversation": "chat:one"}, cookies=cookies)
        self.assertEqual(response.status, 200)
        response = await self.client.post("/api/push/devices", headers=self.headers, json={
            "subscription": {"endpoint": "https://untrusted.example.invalid:9119/x", "keys": {}}, "name": "iPhone"}, cookies=cookies)
        self.assertEqual(response.status, 400)
        self.assertIn("isn't one Roost sends to", (await response.json())["error"])
        response = await self.client.get("/sw.js")
        self.assertEqual(response.status, 200)

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

    async def test_settings_routes_refuse_bad_changes_and_answer_with_the_name_shown(self):
        await self.signed_in()
        response = await self.client.patch("/api/settings", headers=self.headers, json={"accent": "neon"})
        self.assertEqual((response.status, (await response.json())["error"]), (400, "Choose one of the accent colours, or a colour like #2f6f4e."))
        response = await self.client.patch("/api/settings", headers=self.headers, json={"accent": "rose"})
        data = await response.json()
        self.assertEqual((data["agent"], data["accent"], data["accents"][0]), ("Assistant", "rose", "forest"))

    async def test_delete_all_needs_the_typed_word(self):
        await self.signed_in()
        for confirm in (True, "delete", None):
            response = await self.client.delete("/api/conversations", headers=self.headers, json={"confirm": confirm})
            self.assertEqual(response.status, 400)
        response = await self.client.delete("/api/conversations", headers=self.headers, json={"confirm": "DELETE"})
        self.assertEqual((response.status, await response.json()), (200, {"deleted": 0}))

    async def test_about_reports_only_public_service_stats(self):
        await self.signed_in()
        response = await self.client.get("/api/about")
        data = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(set(data), {"roost", "runtime", "stats"})
        self.assertGreater(data["stats"]["disk_total_bytes"], 0)
        # Roost's own code (with its fonts) is counted, not the shared volume's usage.
        self.assertGreater(data["stats"]["app_bytes"], 100_000)
        self.assertLess(data["stats"]["app_bytes"], 50_000_000)
        self.assertEqual(data["roost"]["revision"], "")

    async def test_existing_admission_environment_is_still_honored(self):
        env = {'ROOST_STATE_DIR': str(self.state), 'ROOST_PUBLIC_ORIGIN': self.app['origin'],
               'ROOST_ADMISSION': '3', 'ROOST_NATIVE_ADMISSION': '2'}
        for current, expected in ((True, 3), (False, 2)):
            with patch.dict(os.environ, env):
                if not current:
                    os.environ.pop('ROOST_ADMISSION')
                app = server.create_app()
                async with TestClient(TestServer(app)):
                    self.assertEqual(app['runner'].limit, expected)

    async def test_old_settings_and_retired_state_files_are_ignored(self):
        (self.state / 'settings.json').write_text('{"backend":"retired","agent_name":"Old name","accent":"rose"}')
        for name in ('old-password', 'delivery-token'):
            (self.state / name).write_bytes(b'\xffinvalid retired data')
        preset = self.app['store'].save_persona('Guide', 'Be helpful.')
        self.app['store'].set_default_persona(preset['id'])
        token = self.app['sessions'].issue()
        with patch.dict(os.environ, {'ROOST_STATE_DIR': str(self.state), 'ROOST_PUBLIC_ORIGIN': self.app['origin']}):
            restored = server.create_app()
        async with TestClient(TestServer(restored)) as client:
            public = await (await client.get('/api/me')).json()
            self.assertEqual(public['agent'], 'Guide')
            self.assertFalse(public['signed_in'])
            saved = await (await client.get('/api/settings', cookies={server.COOKIE: token})).json()
            self.assertEqual((saved['accent'], saved['agent']), ('rose', 'Guide'))
            self.assertNotIn('backend', saved)
            self.assertNotIn('agent_name', saved)
            self.assertEqual((await client.get('/api/conversations', cookies={server.COOKIE: token})).status, 200)
        for name in ('old-password', 'delivery-token'):
            self.assertEqual((self.state / name).read_bytes(), b'\xffinvalid retired data')

    async def test_ipv6_loopback_origin_and_invalid_port(self):
        config = {"ROOST_STATE_DIR": str(self.state)}
        with patch.dict(os.environ, {**config, "ROOST_PUBLIC_ORIGIN": "http://[::1]:8787"}):
            app = server.create_app()
            self.assertFalse(app["secure_cookie"])
            app["sessions"].close()
        with patch.dict(os.environ, {**config, "ROOST_PUBLIC_ORIGIN": "https://roost.example.invalid:bad"}), self.assertRaises(ValueError):
            server.create_app()


    async def test_public_origin_matches_browser_normalization(self):
        config = {"ROOST_STATE_DIR": str(self.state)}
        with patch.dict(os.environ, {**config, "ROOST_PUBLIC_ORIGIN": "HTTPS://ROOST.EXAMPLE.INVALID:443/"}):
            app = server.create_app()
            self.assertEqual(app["origin"], "https://roost.example.invalid")
            self.assertTrue(app["secure_cookie"])
            app["sessions"].close()
        for origin in ("https://:8787", "https://example.invalid bad"):
            with patch.dict(os.environ, {**config, "ROOST_PUBLIC_ORIGIN": origin}), self.assertRaises(ValueError):
                server.create_app()

    async def test_push_subject_env_var(self):
        config = {"ROOST_STATE_DIR": str(self.state)}
        # Unset and empty keep the current expression: the origin, or https://localhost for a local preview.
        for subject in (None, ""):
            env = dict(config)
            if subject is not None:
                env["ROOST_PUSH_SUBJECT"] = subject
            with patch.dict(os.environ, {**env, "ROOST_PUBLIC_ORIGIN": "https://roost.example.invalid"}):
                app = server.create_app()
                self.assertEqual(app["push"].subject, "https://roost.example.invalid")
                app["sessions"].close()
        # A valid https: or mailto: value becomes the subject, overriding the origin.
        for subject in ("https://roost.example.com", "mailto:roost@example.com"):
            with patch.dict(os.environ, {**config, "ROOST_PUBLIC_ORIGIN": "https://roost.example.invalid",
                                          "ROOST_PUSH_SUBJECT": subject}):
                app = server.create_app()
                self.assertEqual(app["push"].subject, subject)
                app["sessions"].close()
        # Invalid values fail at startup, naming the variable.
        for subject in ("http://roost.example.com", "https://roost.example.com/path",
                        "https://roost.example.com bad", "mailto:"):
            with patch.dict(os.environ, {**config, "ROOST_PUBLIC_ORIGIN": "https://roost.example.invalid",
                                          "ROOST_PUSH_SUBJECT": subject}), self.assertRaises(ValueError) as raised:
                server.create_app()
            self.assertIn("ROOST_PUSH_SUBJECT", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
