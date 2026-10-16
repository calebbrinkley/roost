"""Deliveries from Hermes's Roost plugin, and the plugin itself against stand-ins for Hermes and Roost."""

import asyncio
import importlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import CookieJar, web
from aiohttp.test_utils import TestClient, TestServer

from roost import deliveries, server
from roost.auth import make_password_hash
from hermes_fake import fake_hermes

REPORT = ("Cronjob Response: Morning brief\n(job_id: job_7)\n-------------\n\nThree things today.\n\n"
          "To stop or manage this job, send me a new message (e.g. \"stop reminder Morning brief\").")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = deliveries.Deliveries(sqlite3.connect(":memory:"), Path(self.directory.name) / "delivery-token")

    def tearDown(self):
        self.directory.cleanup()

    def test_a_wrapped_job_report_gives_its_job_and_text(self):
        self.assertEqual(deliveries.parse(REPORT, None), {"title": "Morning brief", "job_id": "job_7", "text": "Three things today."})
        self.assertEqual(deliveries.parse(REPORT, "job_9")["job_id"], "job_9", "the plugin's job id wins")
        self.assertEqual(deliveries.parse("Dinner is at 7.", None), {"title": "", "job_id": None, "text": "Dinner is at 7."})

    def test_the_token_is_private_and_a_new_one_replaces_it(self):
        self.assertEqual(os.stat(self.store.token_path).st_mode & 0o777, 0o600)
        old = self.store.token()
        self.assertTrue(self.store.authorized("Bearer " + old))
        for header in ("", "Bearer", "Bearer wrong", "Basic " + old, old):
            self.assertFalse(self.store.authorized(header), header)
        self.store.rotate()
        self.assertFalse(self.store.authorized("Bearer " + old))

    def test_only_the_newest_deliveries_are_kept(self):
        with patch("roost.deliveries.KEEP", 3):
            items = [self.store.add(f"message {i}", None) for i in range(5)]
        self.assertIsNone(self.store.get(items[0]["id"]))
        self.assertEqual(self.store.get(items[-1]["id"])["text"], "message 4")
        self.assertEqual(self.store.last(), items[-1]["created"])


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = Path(self.directory.name)
        (self.state / "password.hash").write_text(make_password_hash("example-demo-password"))
        (self.state / "upstream-password").write_text("upstream")
        with patch.dict(os.environ, {
            "ROOST_PUBLIC_ORIGIN": "https://roost.example.invalid", "ROOST_STATE_DIR": str(self.state),
            "ROOST_HERMES_URL": "http://127.0.0.1:1", "ROOST_HERMES_USERNAME": "demo",
            "ROOST_HERMES_PASSWORD_FILE": str(self.state / "upstream-password"),
        }):
            self.app = server.create_app()
        self.app["hermes"] = fake_hermes()
        self.app["hermes_agent_name"] = ("Kestrel", 10 ** 12)
        self.client = TestClient(TestServer(self.app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.headers = {"Origin": "https://roost.example.invalid", "X-Roost-Request": "1"}
        self.sent = []
        async def send(http, message, only=None):
            self.sent.append(message)
            return 1
        self.app["push"].send = send

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()

    async def sign_in(self):
        response = await self.client.post("/api/login", headers=self.headers, json={"password": "example-demo-password"})
        return {server.COOKIE: response.cookies[server.COOKIE].value}

    async def test_hermes_delivers_with_the_token_and_every_device_is_notified(self):
        token = self.app["deliveries"].token()
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            response = await self.client.post("/api/hermes/deliveries", headers=headers, json={"text": "hi"})
            self.assertEqual(response.status, 401)
        auth = {"Authorization": "Bearer " + token}
        response = await self.client.post("/api/hermes/deliveries", headers=auth, json={"text": REPORT})
        self.assertEqual(response.status, 201)
        self.assertEqual((self.sent[-1]["title"], self.sent[-1]["body"], self.sent[-1]["url"]),
                         ("Morning brief", "Three things today.", "/?j=job_7"))
        response = await self.client.post("/api/hermes/deliveries", headers=auth, json={"text": "Dinner is at 7."})
        delivered = (await response.json())["id"]
        self.assertEqual((self.sent[-1]["title"], self.sent[-1]["url"]), ("Kestrel", "/?d=" + delivered))
        for body in ({"text": ""}, {"text": "hi", "job_id": "../x"}, {"text": 5}):
            response = await self.client.post("/api/hermes/deliveries", headers=auth, json=body)
            self.assertEqual(response.status, 400, body)
        response = await self.client.post("/api/hermes/deliveries", headers=auth, json={"text": "x" * 48000})
        self.assertEqual(response.status, 201, "a long report is over the app-wide body limit but within its own")
        response = await self.client.post("/api/hermes/deliveries", headers=auth, data=b'{"text": "' + b"x" * 300000 + b'"}',
                                          skip_auto_headers={"Content-Type"}, )
        self.assertIn(response.status, {400, 413})
        response = await self.client.get("/api/deliveries/" + delivered)
        self.assertEqual(response.status, 401, "reading one needs a signed-in browser")
        cookies = await self.sign_in()
        response = await self.client.get("/api/deliveries/" + delivered, cookies=cookies)
        self.assertEqual((await response.json())["text"], "Dinner is at 7.")

    async def test_reply_usage_requires_plugin_auth_and_exact_numeric_fields(self):
        body = {"session_id": "one", "message_id": 12, "input_tokens": 1200, "output_tokens": 240, "model_seconds": 6.0}
        cookies = await self.sign_in()
        for headers in ({}, {"Authorization": "Bearer wrong"}, self.headers):
            response = await self.client.post(server.USAGE_PATH, headers=headers, cookies=cookies, json=body)
            self.assertEqual(response.status, 401)
        auth = {"Authorization": "Bearer " + self.app["deliveries"].token()}
        for change in ({"extra": "not allowed"}, {"message_id": True}, {"message_id": 0}, {"input_tokens": -1},
                       {"output_tokens": "240"}, {"output_tokens": 2 ** 53}, {"model_seconds": 0},
                       {"model_seconds": float("nan")}, {"model_seconds": 10 ** 1000}, {"session_id": "../bad"}):
            response = await self.client.post(server.USAGE_PATH, headers=auth, json={**body, **change})
            self.assertEqual(response.status, 400, change.keys())
        response = await self.client.post(server.USAGE_PATH, headers={**auth, "Content-Type": "application/json"}, data=b"x" * 5000)
        self.assertEqual(response.status, 413)
        self.assertEqual((await self.client.post(server.USAGE_PATH, headers=auth, json=body)).status, 200)
        # A retried delivery replaces the same durable identity rather than double-counting it.
        self.assertEqual((await self.client.post(server.USAGE_PATH, headers=auth, json=body)).status, 200)
        self.app["hermes"].messages.return_value = {"session_id": "one", "messages": [
            {"id": 12, "role": "assistant", "content": "Reply"}, {"id": 13, "role": "user", "content": "Question"}]}
        result = await (await self.client.get("/api/conversations/one", cookies=cookies)).json()
        self.assertEqual(result["messages"][0]["roost_usage"], {key: body[key] for key in ("input_tokens", "output_tokens", "model_seconds")})
        self.assertNotIn("roost_usage", result["messages"][1])
        self.app["hermes"].messages.return_value = {"session_id": "two", "messages": [{"id": 12, "role": "assistant", "content": "Other"}]}
        result = await (await self.client.get("/api/conversations/two", cookies=cookies)).json()
        self.assertNotIn("roost_usage", result["messages"][0])
        self.assertEqual(self.sent, [], "measurement uploads do not create notifications")
        response = await self.client.delete("/api/conversations/one", headers=self.headers, cookies=cookies, json={"confirm": True})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.app["sessions"].db.execute("SELECT COUNT(*) FROM reply_usage").fetchone()[0], 0)


    async def test_settings_show_the_token_offer_the_plugin_and_replace_the_token(self):
        response = await self.client.get("/api/hermes")
        self.assertEqual(response.status, 401)
        cookies = await self.sign_in()
        link = await (await self.client.get("/api/hermes", cookies=cookies)).json()
        self.assertEqual((link["token"], link["url"], link["last_delivery"]),
                         (self.app["deliveries"].token(), "https://roost.example.invalid", None))
        response = await self.client.post("/api/hermes/token", headers=self.headers, json={"confirm": "yes"}, cookies=cookies)
        self.assertEqual(response.status, 400)
        response = await self.client.post("/api/hermes/token", headers=self.headers, json={"confirm": True}, cookies=cookies)
        self.assertNotEqual((await response.json())["token"], link["token"])
        response = await self.client.get("/api/hermes/plugin.zip", cookies=cookies)
        self.assertEqual(response.headers["Content-Disposition"], 'attachment; filename="roost-hermes-plugin.zip"')
        with zipfile.ZipFile(io.BytesIO(await response.read())) as bundle:
            self.assertEqual(sorted(bundle.namelist()), ["roost/__init__.py", "roost/plugin.yaml"])
            self.assertIn("name: roost", bundle.read("roost/plugin.yaml").decode())


def hermes_stand_in():
    """The few Hermes modules the plugin imports, with the behaviour it relies on."""
    modules = {name: types.ModuleType(name) for name in
               ("gateway", "gateway.config", "gateway.platforms", "gateway.platforms._shared", "gateway.platforms.base")}

    class BasePlatformAdapter:
        def __init__(self, config, platform):
            self.config, self.platform, self.name, self.connected = config, platform, "roost", False
        def _mark_connected(self):
            self.connected = True
        def _mark_disconnected(self):
            self.connected = False

    modules["gateway.config"].Platform = lambda name: name
    modules["gateway.config"].PlatformConfig = SimpleNamespace
    modules["gateway.platforms.base"].BasePlatformAdapter = BasePlatformAdapter
    modules["gateway.platforms.base"].SendResult = lambda **fields: SimpleNamespace(**fields)
    shared = modules["gateway.platforms._shared"]
    shared.get_scoped_secret = lambda name, default=None: os.environ.get(name, default)
    shared.extra_or_secret = lambda extra, key, env, default="": os.environ.get(env) or (extra or {}).get(key) or default
    shared.send_error = lambda message: {"error": str(message)}
    return modules


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.received = []
        self.received_event = asyncio.Event()
        self.status = 201
        async def deliveries_route(request):
            self.received.append((request.headers.get("Authorization"), await request.json()))
            self.received_event.set()
            return web.json_response({"id": "abc"}, status=self.status)
        app = web.Application()
        app.router.add_post("/api/hermes/deliveries", deliveries_route)
        app.router.add_post("/api/hermes/reply-usage", deliveries_route)
        self.roost = TestServer(app)
        await self.roost.start_server()
        self.env = patch.dict(os.environ, {"ROOST_URL": str(self.roost.make_url("")).rstrip("/") + "/",
                                           "ROOST_DELIVERY_TOKEN": "tok", "ROOST_HOME_CHANNEL": "roost"})
        self.env.start()
        self.modules = patch.dict(sys.modules, hermes_stand_in())
        self.modules.start()
        sys.modules.pop("roost.hermes_plugin", None)
        self.plugin = importlib.import_module("roost.hermes_plugin")

    async def asyncTearDown(self):
        sys.modules.pop("roost.hermes_plugin", None)
        self.modules.stop()
        self.env.stop()
        await self.roost.close()

    def test_it_registers_a_platform_jobs_can_deliver_to(self):
        hooks = {}
        ctx = SimpleNamespace(register_platform=lambda **fields: setattr(ctx, "fields", fields),
                              register_hook=lambda name, callback: hooks.update({name: callback}))
        self.plugin.register(ctx)
        fields = ctx.fields
        self.assertEqual(set(hooks), {"post_api_request", "post_llm_call", "on_session_end"})
        self.assertEqual((fields["name"], fields["cron_deliver_env_var"], fields["required_env"]),
                         ("roost", "ROOST_HOME_CHANNEL", ["ROOST_URL", "ROOST_DELIVERY_TOKEN"]))
        self.assertEqual(fields["env_enablement_fn"](), {"url": self.plugin._settings(None)[0],
                                                         "home_channel": {"chat_id": "roost", "name": "Roost"}})
        self.assertTrue(fields["check_fn"]())

    async def test_a_live_send_and_a_cron_send_reach_roost_with_the_token(self):
        adapter = self.plugin.RoostAdapter(SimpleNamespace(extra={}))
        self.assertTrue(await adapter.connect())
        result = await adapter.send("roost", "Report", metadata={"job_id": "job_7"})
        self.assertEqual((result.success, result.message_id), (True, "abc"))
        result = await self.plugin._standalone_send(SimpleNamespace(extra={}), "roost", "x" * 50000)
        self.assertEqual(result["success"], True)
        self.assertEqual(self.received[0], ("Bearer tok", {"text": "Report", "job_id": "job_7"}))
        self.assertEqual(len(self.received[1][1]["text"]), self.plugin.MAX_MESSAGE_LENGTH)
        self.status = 401
        result = await adapter.send("roost", "Report")
        self.assertFalse(result.success)
        self.assertIn("401", result.error)
        await adapter.disconnect()
        with patch.dict(os.environ, {"ROOST_DELIVERY_TOKEN": ""}):
            self.assertIn("error", await self.plugin._standalone_send(SimpleNamespace(extra={}), "roost", "hi"))


    async def test_whole_reply_measurements_include_all_tool_rounds_and_use_durable_identity(self):
        for call, (usage, duration) in enumerate([({"prompt_tokens": 100, "output_tokens": 30}, 3), ({"prompt_tokens": 200, "output_tokens": 50}, 5)], 1):
            self.plugin._record_usage(turn_id="turn-one", usage=usage, api_duration=duration, started_at=1700000000, api_call_count=call)
        self.plugin._record_usage(turn_id="other-turn", usage={"prompt_tokens": 999, "output_tokens": 999}, api_duration=1, started_at=1700000000, api_call_count=1)
        history = [{"role": "user", "content": "Private question", "_row_id": 10, "timestamp": 1700000000},
                   {"role": "assistant", "content": "Private reply", "_row_id": 12, "timestamp": 1700000020}]
        self.plugin._finish_usage(session_id="one", turn_id="turn-one", conversation_history=history)
        await asyncio.wait_for(self.received_event.wait(), 5)
        self.assertEqual(self.received, [("Bearer tok", {"session_id": "one", "message_id": 12,
                              "input_tokens": 300, "output_tokens": 80, "model_seconds": 8})])
        self.plugin._finish_usage(session_id="one", turn_id="turn-one", conversation_history=history)
        self.assertEqual(len(self.received), 1, "a completion hook cannot publish the same turn twice")
        self.plugin._discard_usage(turn_id="other-turn")
        self.assertEqual(len(self.plugin._turn_usage), 0)

    async def test_incomplete_usage_is_unavailable_and_never_uses_a_session_total(self):
        self.plugin._record_usage(turn_id="one", usage={"prompt_tokens": 10, "output_tokens": 5}, api_duration=2, started_at=1700000000, api_call_count=1)
        self.plugin._record_usage(turn_id="one", usage=None, api_duration=None, started_at=1700000000, api_call_count=2)
        self.plugin._finish_usage(session_id="one", turn_id="one", conversation_history=[
            {"role": "assistant", "_row_id": 5, "timestamp": 1700000010}])
        await asyncio.wait_for(self.received_event.wait(), 5)
        self.assertEqual(self.received[0][1], {"session_id": "one", "message_id": 5, "input_tokens": None,
                                              "output_tokens": None, "model_seconds": None})

    def test_a_missing_or_old_final_row_never_receives_new_turn_measurements(self):
        for last in ({"role": "user", "_row_id": 3, "timestamp": 1700000020},
                     {"role": "assistant", "_row_id": 3, "timestamp": 1699999999},
                     {"role": "assistant", "timestamp": 1700000020},
                     {"role": "assistant", "_row_id": 3, "timestamp": 1700000020, "tool_calls": [{}]}):
            self.plugin._record_usage(turn_id="one", usage={"prompt_tokens": 10, "output_tokens": 5}, api_duration=2, started_at=1700000000, api_call_count=1)
            self.plugin._finish_usage(session_id="one", turn_id="one", conversation_history=[last])
        self.assertEqual(self.received, [])
        self.assertEqual(len(self.plugin._turn_usage), 0)

    def test_abandoned_turn_measurements_are_bounded_and_discarded(self):
        for number in range(140):
            self.plugin._record_usage(turn_id=str(number), usage=None)
        self.assertEqual(len(self.plugin._turn_usage), 128)
        self.plugin._discard_usage(turn_id="139")
        self.assertNotIn("139", self.plugin._turn_usage)


    async def test_missing_earlier_calls_cannot_be_reported_as_a_whole_reply(self):
        self.plugin._record_usage(turn_id="one", api_call_count=3, usage={"prompt_tokens": 10, "output_tokens": 5},
                                  api_duration=2, started_at=1700000000)
        self.plugin._finish_usage(session_id="one", turn_id="one", conversation_history=[
            {"role": "assistant", "_row_id": 5, "timestamp": 1700000010}])
        await asyncio.wait_for(self.received_event.wait(), 5)
        self.assertTrue(all(self.received[0][1][key] is None for key in ("input_tokens", "output_tokens", "model_seconds")))
