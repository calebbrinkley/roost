"""Runtime regressions using synthetic data and a local HTTP stand-in."""

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

from aiohttp import CookieJar, WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from roost import chats, live, server
from roost.__main__ import main as cli_main
from roost.auth import Sessions, make_password_hash
from roost.hermes import Hermes, HermesError
from hermes_fake import fake_hermes
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

    async def test_offset_is_bounded_before_integer_conversion(self):
        hermes = fake_hermes()
        for value in ("9" * 5000, "١", "-1", "100001"):
            request = SimpleNamespace(query={"offset": value}, app={"hermes": hermes})
            with self.subTest(value=value[:10]), self.assertRaises(web.HTTPBadRequest):
                await server.conversations(request)
        hermes.sessions.assert_not_awaited()

    async def test_resume_uses_raw_identifier(self):
        hermes = SimpleNamespace(send_message=AsyncMock(return_value="chat:one"))
        import sqlite3
        request = SimpleNamespace(match_info={"sid": "chat:one"}, app={"hermes": hermes, "watches": {}, "chats": chats.Chats(sqlite3.connect(":memory:"))},
                                  content_type="application/json", json=AsyncMock(return_value={"text": "hello"}))
        await server.send_message(request)
        hermes.send_message.assert_awaited_once_with("hello", "chat:one", follow=ANY, reasoning=None, applied=ANY)
        self.assertIn("chat:one", request.app["watches"])


    async def test_deletion_requires_a_boolean_confirmation(self):
        hermes = fake_hermes()
        for handler, name in ((server.delete_job, "jid"), (server.delete_conversation, "sid")):
            for value in (1, "true", False, None):
                request = SimpleNamespace(match_info={name: "one"}, app={"hermes": hermes},
                                          content_type="application/json", json=AsyncMock(return_value={"confirm": value}))
                with self.subTest(handler=handler.__name__, value=value), self.assertRaises(web.HTTPBadRequest):
                    await handler(request)
        hermes.sessions.assert_not_awaited()

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
        self.submit_status = "accepted"
        self.frames, self.configured = [], "medium"
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
        async def ticket(request):
            return web.json_response({"ticket": "one-use"})
        async def socket(request):
            # A turn whose first reasoning arrives before prompt.submit answers, among other sessions' events.
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            def event(kind, sid="runtime", **payload):
                return {"jsonrpc": "2.0", "method": "event", "params": {"type": kind, "session_id": sid, "payload": payload}}
            async for message in ws:
                frame = message.json()
                self.frames.append(frame)
                if frame["method"] == "config.get":
                    await ws.send_json({"jsonrpc": "2.0", "id": frame["id"], "result": {"value": self.configured, "display": "show"}})
                elif frame["method"] == "config.set":
                    answer = ({"error": {"code": 4002}} if frame["params"]["value"] == "rejected" else
                              {"result": {"key": "reasoning", "value": frame["params"]["value"]}})
                    await ws.send_json({"jsonrpc": "2.0", "id": frame["id"], **answer})
                elif frame["method"] in {"session.create", "session.resume"}:
                    await ws.send_json({"jsonrpc": "2.0", "id": frame["id"], "result": {"session_id": "runtime", "stored_session_id": "chat:one"}})
                else:
                    await ws.send_json(event("reasoning.delta", text="Early "))
                    await ws.send_json({"jsonrpc": "2.0", "id": frame["id"], "result": {"status": self.submit_status} if frame["method"] == "prompt.submit" else {}})
                    for item in (event("message.delta", "elsewhere", text="not ours"), event("reasoning.delta", text="thought"),
                                 event("message.delta", text="Hi"), event("message.complete", text="Hi")):
                        await ws.send_json(item)
            return ws
        app = web.Application()
        app.router.add_post("/auth/password-login", login)
        app.router.add_get("/api/data", response)
        app.router.add_post("/api/auth/ws-ticket", ticket)
        app.router.add_get("/api/ws", socket)
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
                await self.hermes._request("GET", "/api/data")
            self.assertEqual(caught.exception.status, 502)

    async def test_chunked_response_is_bounded(self):
        self.chunked = True
        self.response_body = json.dumps({"data": "x" * (8 * 1024 * 1024)}).encode()
        with self.assertRaises(HermesError) as caught:
            await self.hermes._request("GET", "/api/data")
        self.assertEqual(caught.exception.status, 502)

    async def test_unreadable_password_is_a_safe_service_error(self):
        self.password.write_bytes(b"\xff")
        with self.assertRaises(HermesError) as caught:
            await self.hermes._request("GET", "/api/data")
        self.assertEqual(caught.exception.status, 503)

    async def test_valid_json_objects_and_lists(self):
        for value in ({"ok": True}, [{"id": "one"}]):
            self.response_body = json.dumps(value).encode()
            self.assertEqual(await self.hermes._request("GET", "/api/data"), value)

    async def test_malformed_rpc_frames_are_safe_errors(self):
        for frame in ([], {"id": 1, "result": []}, {"id": 1, "result": "bad"}):
            socket = SimpleNamespace(send_json=AsyncMock(), receive=AsyncMock(return_value=
                                     SimpleNamespace(type=WSMsgType.TEXT, json=lambda: frame)))
            with self.subTest(frame=frame), self.assertRaises(HermesError) as caught:
                await Hermes._rpc_on_socket(socket, 1, "session.create", {})
            self.assertEqual(caught.exception.status, 502)


    async def test_a_followed_turn_yields_its_own_events_from_the_start(self):
        followed = []
        sid = await self.hermes.send_message("hello", follow=lambda sid, events: followed.append((sid, events)))
        self.assertEqual((sid, followed[0][0]), ("chat:one", "chat:one"))
        events = []
        async for event in followed[0][1]:
            events.append(event)
            if event[0] == "message.complete":
                break
        await followed[0][1].aclose()
        self.assertEqual(events, [("reasoning.delta", {"text": "Early "}), ("reasoning.delta", {"text": "thought"}),
                                  ("message.delta", {"text": "Hi"}), ("message.complete", {"text": "Hi"})])

    async def test_rpc_events_do_not_extend_the_deadline(self):
        async def receive():
            await asyncio.sleep(0.001)
            return SimpleNamespace(type=WSMsgType.TEXT, json=lambda: {"method": "event"})
        socket = SimpleNamespace(send_json=AsyncMock(), receive=receive)
        with patch("roost.hermes.RPC_TIMEOUT", 0.02), self.assertRaises(asyncio.TimeoutError):
            await Hermes._rpc_on_socket(socket, 1, "session.create", {})


    async def test_busy_acknowledgements_do_not_claim_a_new_turn_stream(self):
        for status in ("queued", "steered", "redirected"):
            self.submit_status = status
            followed = []
            sid = await self.hermes.send_message("follow up", "chat:one", follow=lambda sid, events: followed.append((sid, events)))
            self.assertEqual(sid, "chat:one")
            self.assertEqual(followed, [("chat:one", None)], status)


    async def test_a_reasoning_level_is_set_for_the_conversation_before_the_message(self):
        applied = []
        await self.hermes.send_message("hello", "chat:one", reasoning="low", applied=applied.append)
        methods = [frame["method"] for frame in self.frames]
        self.assertEqual(methods[-3:], ["session.resume", "config.set", "prompt.submit"])
        self.assertEqual(self.frames[-2]["params"], {"session_id": "runtime", "key": "reasoning", "value": "low"})
        self.assertEqual(applied, ["low"])

    async def test_default_restores_the_level_hermes_is_configured_with(self):
        self.configured, applied = "high", []
        await self.hermes.send_message("hello", "chat:one", reasoning="default", applied=applied.append)
        get, put = (frame for frame in self.frames if frame["method"].startswith("config."))
        self.assertEqual(get["params"], {"key": "reasoning"})  # no conversation: Hermes's own setting
        self.assertEqual(put["params"]["value"], "high")
        self.assertEqual(applied, ["default"])

    async def test_a_rejected_level_still_sends_the_message(self):
        applied = []
        await self.hermes.send_message("hello", "chat:one", reasoning="rejected", applied=applied.append)
        self.assertEqual(self.frames[-1]["method"], "prompt.submit")
        self.assertEqual(applied, [])

    async def test_no_level_sends_no_configuration(self):
        await self.hermes.send_message("hello", "chat:one")
        self.assertFalse(any(frame["method"].startswith("config.") for frame in self.frames))


class ChatSettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import sqlite3
        self.chats = chats.Chats(sqlite3.connect(":memory:"))

    def test_instructions_go_once_per_change_and_a_change_in_flight_still_goes(self):
        store = self.chats
        self.assertIsNone(store.unsent("chat:one"))
        store.set_instructions("chat:one", "Answer in French.")
        self.assertEqual(store.unsent("chat:one"), "Answer in French.")
        store.set_instructions("chat:one", "Answer in Spanish.")  # changed while the first was on its way
        store.sent("chat:one", "Answer in French.", "chat:one")
        self.assertEqual(store.unsent("chat:one"), "Answer in Spanish.")
        store.sent("chat:one", "Answer in Spanish.", "chat:one")
        self.assertIsNone(store.unsent("chat:one"))
        store.set_instructions("chat:one", "Answer in Spanish.")  # unchanged: nothing to send
        self.assertIsNone(store.unsent("chat:one"))

    def test_clearing_tells_the_model_once_then_leaves_nothing(self):
        store = self.chats
        store.set_instructions("chat:one", "")
        self.assertIsNone(store.unsent("chat:one"), "nothing was set, so nothing to clear")
        store.set_instructions("chat:one", "Be brief.")
        store.sent("chat:one", "Be brief.", "chat:one")
        store.set_instructions("chat:one", "")
        self.assertEqual(store.unsent("chat:one"), "")
        self.assertIn(chats.CLEARED, chats.with_instructions("hi", ""))
        store.sent("chat:one", "", "chat:one")
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM chat_settings").fetchone()[0], 0)

    def test_settings_follow_a_conversation_hermes_continues_under_a_new_id_and_are_forgotten(self):
        store = self.chats
        store.set_instructions("chat:one", "Be brief.")
        store.sent("chat:one", "Be brief.", "chat:two")
        self.assertEqual((store.instructions("chat:one"), store.instructions("chat:two")), ("", "Be brief."))
        store.forget("chat:two")
        self.assertEqual(store.instructions("chat:two"), "")

    def test_reasoning_is_left_alone_until_chosen_and_a_restored_default_goes_once(self):
        store = self.chats
        self.assertEqual((store.reasoning("chat:one"), store.reasoning_to_apply("chat:one")), ("default", None))
        store.set_reasoning("chat:one", "default")  # never chosen: still nothing to apply
        self.assertIsNone(store.reasoning_to_apply("chat:one"))
        store.set_reasoning("chat:one", "high")
        store.reasoning_applied("chat:one", "high")
        self.assertEqual(store.reasoning_to_apply("chat:one"), "high", "Hermes forgets it on restart, so it goes every time")
        store.set_reasoning("chat:one", "default")
        self.assertEqual((store.reasoning("chat:one"), store.reasoning_to_apply("chat:one")), ("default", "default"))
        store.reasoning_applied("chat:one", "default")
        self.assertIsNone(store.reasoning_to_apply("chat:one"))
        with self.assertRaises(ValueError):
            store.set_reasoning("chat:one", "max")

    def test_reasoning_follows_a_renamed_conversation_and_is_forgotten(self):
        store = self.chats
        store.set_reasoning("chat:one", "none")
        store.moved("chat:one", "chat:two")
        self.assertEqual((store.reasoning("chat:one"), store.reasoning("chat:two")), ("default", "none"))
        store.forget("chat:two")
        self.assertEqual(store.reasoning("chat:two"), "default")

    async def test_messages_carry_the_conversations_reasoning_and_settings_validate_it(self):
        async def send(text, sid=None, follow=None, reasoning=None, applied=None):
            if applied and reasoning:
                applied(reasoning)
            return "chat:two"
        hermes = SimpleNamespace(send_message=AsyncMock(side_effect=send))
        app = {"hermes": hermes, "watches": {}, "chats": self.chats, "live": {}}
        def request(body, sid="chat:one"):
            return SimpleNamespace(match_info={"sid": sid}, app=app, content_type="application/json", json=AsyncMock(return_value=body))
        with self.assertRaises(web.HTTPBadRequest):
            await server.update_chat_settings(request({"reasoning": "extreme"}))
        with self.assertRaises(web.HTTPBadRequest):
            await server.update_chat_settings(request({"title": "x"}))
        response = await server.update_chat_settings(request({"reasoning": "low"}))
        self.assertEqual(json.loads(response.body), {"instructions": "", "reasoning": "low"})
        with patch("roost.server.following", lambda app, text: None):
            await server.send_message(request({"text": "hello"}))
        self.assertEqual(hermes.send_message.await_args.kwargs["reasoning"], "low")
        self.assertEqual(self.chats.reasoning("chat:two"), "low", "kept when Hermes continues under a new id")
        with patch("roost.server.following", lambda app, text: None):
            created = await server.create_conversation(request({"text": "new", "reasoning": "none"}))
            self.assertEqual(hermes.send_message.await_args.kwargs["reasoning"], "none")
            await server.create_conversation(request({"text": "plain"}))
            self.assertIsNone(hermes.send_message.await_args.kwargs["reasoning"])
            with self.assertRaises(web.HTTPBadRequest):
                await server.create_conversation(request({"text": "x", "reasoning": "max"}))
        self.assertEqual(created.status, 201)

    async def test_the_next_message_carries_changed_instructions_and_shows_as_typed(self):
        followed = []
        hermes = SimpleNamespace(send_message=AsyncMock(return_value="chat:one"))
        app = {"hermes": hermes, "watches": {}, "chats": self.chats, "live": {}}
        def request(text):
            return SimpleNamespace(match_info={"sid": "chat:one"}, app=app, content_type="application/json",
                                   json=AsyncMock(return_value={"text": text}))
        self.chats.set_instructions("chat:one", "Answer in French.")
        with patch("roost.server.following", lambda app, text: followed.append(text)):
            await server.send_message(request("hello"))
            await server.send_message(request("again"))
        first, second = (call.args[0] for call in hermes.send_message.await_args_list)
        self.assertEqual(first, chats.with_instructions("hello", "Answer in French."))
        self.assertTrue(first.startswith(chats.BEGIN + "\nAnswer in French.\n" + chats.END + "\n\n"))
        self.assertEqual(second, "again")
        self.assertEqual(followed, ["hello", "again"])

    def test_the_browser_strips_the_same_markers(self):
        source = (Path(__file__).resolve().parents[1] / "roost/static/app.js").read_text(encoding="utf-8")
        self.assertIn(f"const INSTRUCTIONS_BEGIN = {json.dumps(chats.BEGIN)};", source)
        self.assertIn(f"const INSTRUCTIONS_END = {json.dumps(chats.END)};", source)


class LiveTurnTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def events(*items):
        for item in items:
            yield item
            await asyncio.sleep(0)

    async def test_a_turn_streams_in_steps_then_leaves_the_registry(self):
        registry, written = {}, []
        task = live.follow(registry, "chat:one", "hello", self.events(
            ("reasoning.delta", {"text": "Look it up."}), ("message.delta", {"text": "Searching."}),
            ("tool.start", {"name": "web_search"}), ("reasoning.delta", {"text": "\nFound it."}),
            ("message.delta", {"text": "Here."}), ("message.complete", {}), ("message.delta", {"text": "late"})))
        turn = registry["chat:one"]
        response = SimpleNamespace(write=AsyncMock(side_effect=lambda data: written.append(data)))
        await asyncio.gather(task, live.send(response, turn))
        self.assertEqual((turn.thoughts, turn.text, turn.done), ("Look it up.\n\nFound it.", "Searching.\n\nHere.", True))
        self.assertNotIn("chat:one", registry)
        sent = [json.loads(line[6:]) for line in written]
        self.assertEqual((sent[0]["reset"], sent[0]["user"], sent[-1]["done"]), (True, "hello", True))
        self.assertEqual("".join(event.get("text", "") for event in sent), turn.text)

    async def test_a_turn_is_bounded_and_a_new_one_keeps_its_place(self):
        registry = {}
        with patch("roost.live.TEXT_LIMIT", 8):
            first = live.follow(registry, "chat:one", "one", self.events(("message.delta", {"text": "12345"}), ("message.delta", {"text": "6789"})))
            turn = registry["chat:one"]
            second = live.follow(registry, "chat:one", "two", self.events())
            await first
        self.assertEqual(turn.text, "12345")
        await second
        self.assertEqual(registry, {})

    async def test_busy_followup_retires_the_ambiguous_live_view_without_cancelling_hermes(self):
        async def waiting():
            yield ("message.delta", {"text": "Still working"})
            await asyncio.Event().wait()
        registry = {}
        task = live.follow(registry, "chat:one", "first", waiting())
        turn = registry["chat:one"]
        await asyncio.sleep(0)
        server.following({"live": registry}, "second")("chat:one", None)
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 0.5)
        self.assertNotIn("chat:one", registry)
        self.assertTrue(turn.done)

    async def test_a_broken_stream_ends_the_turn(self):
        async def broken():
            yield ("message.delta", {"text": "Hi"})
            raise ConnectionResetError
        registry = {}
        task = live.follow(registry, "chat:one", "hello", broken())
        turn = registry["chat:one"]
        await task
        self.assertTrue(turn.done)
        self.assertEqual(registry, {})


class ReplyNotificationTests(unittest.IsolatedAsyncioTestCase):
    def watching(self, rows, since):
        sent = []

        async def send(http, message, only=None):
            sent.append(message)
            return 1
        hermes = fake_hermes()
        hermes.messages.return_value = {"messages": rows}
        app = {"hermes": hermes,
               "push": SimpleNamespace(send=send), "push_http": None, "watches": {}, "presence": {},
               "settings_file": Path("/nonexistent/settings.json"), "hermes_agent_name": ("Kestrel", time.monotonic())}
        server.watch_reply(app, "chat:one", since)
        return app, sent

    async def test_notifies_once_the_turn_ends_not_at_an_interim_reply(self):
        now = time.time()
        rows = [{"role": "user", "content": "check the logs", "timestamp": now},
                {"role": "assistant", "content": "Let me look.", "tool_calls": [{"id": "a"}], "timestamp": now + 1}]
        app, sent = self.watching(rows, now)
        await server.check_replies(app)
        self.assertEqual(sent, [])
        rows += [{"role": "tool", "content": "ok", "timestamp": now + 2},
                 {"role": "assistant", "content": "All clear: ```no errors``` in the last hour.", "timestamp": now + 3}]
        await server.check_replies(app)
        self.assertEqual(sent, [{"title": "Kestrel", "body": "All clear: no errors in the last hour.",
                                 "url": "/?c=chat%3Aone", "tag": "reply-chat:one"}])
        self.assertEqual(app["watches"], {})

    async def test_a_reply_from_before_the_message_is_not_its_answer(self):
        now = time.time()
        app, sent = self.watching([{"role": "assistant", "content": "Earlier answer.", "timestamp": now - 60}], now)
        await server.check_replies(app)
        self.assertEqual(sent, [])
        self.assertIn("chat:one", app["watches"])

    async def test_a_turn_that_ends_without_a_reply_stops_being_watched_silently(self):
        now = time.time()
        rows = [{"role": "user", "content": "check", "timestamp": now + 1},
                {"role": "assistant", "content": "", "finish_reason": "agent_error", "timestamp": now + 2}]
        app, sent = self.watching(rows, now)
        await server.check_replies(app)
        self.assertEqual((sent, app["watches"]), ([], {}))

    async def test_a_conversation_on_screen_is_not_notified_unless_that_report_is_stale(self):
        now = time.time()
        rows = [{"role": "assistant", "content": "Done.", "timestamp": now + 1}]
        app, sent = self.watching(rows, now)
        app["presence"]["phone"] = (time.monotonic(), "chat:one")
        await server.check_replies(app)
        self.assertEqual((sent, app["watches"]), ([], {}))
        app, sent = self.watching(rows, now)
        app["presence"]["phone"] = (time.monotonic() - server.PRESENCE_SECONDS - 1, "chat:one")
        await server.check_replies(app)
        self.assertEqual(len(sent), 1)


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
        self.app["hermes"] = fake_hermes()
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
            "subscription": {"endpoint": "https://skynet-hermes:9119/x", "keys": {}}, "name": "iPhone"}, cookies=cookies)
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

    async def test_a_reply_being_written_streams_to_a_signed_in_browser(self):
        response = await self.client.get("/api/conversations/chat:one/stream")
        self.assertEqual(response.status, 401)
        response = await self.client.post("/api/login", headers=self.headers, json={"password": "example-demo-password"})
        cookies = {server.COOKIE: response.cookies[server.COOKIE].value}
        response = await self.client.get("/api/conversations/chat:one/stream", cookies=cookies)
        self.assertEqual(response.status, 404)
        release = asyncio.Event()
        async def events():
            yield ("message.delta", {"text": "Hi"})
            await release.wait()
        live.follow(self.app["live"], "chat:one", "hello", events())
        response = await self.client.get("/api/conversations/chat:one", cookies=cookies)
        self.assertIs((await response.json())["live"], True)
        response = await self.client.get("/api/conversations/chat:one/stream", cookies=cookies)
        self.assertEqual((response.status, response.content_type), (200, "text/event-stream"))
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(json.loads((await response.content.readline())[6:])["reset"], True)
        release.set()
        body = await response.text()
        self.assertIn('"done": true', body)
        response = await self.client.get("/api/conversations/chat:one", cookies=cookies)
        self.assertIs((await response.json())["live"], False)

    async def test_conversation_settings_export_and_deletion(self):
        response = await self.client.post("/api/login", headers=self.headers, json={"password": "example-demo-password"})
        cookies = {server.COOKIE: response.cookies[server.COOKIE].value}
        url = "/api/conversations/chat:one"
        for body in ({"instructions": 1}, {"instructions": "x" * 4001}, {"instructions": "ok", "extra": 1}):
            response = await self.client.put(url + "/settings", headers=self.headers, json=body, cookies=cookies)
            self.assertEqual(response.status, 400, body)
        response = await self.client.put(url + "/settings", headers=self.headers, json={"instructions": " Be brief. "}, cookies=cookies)
        self.assertEqual(await response.json(), {"instructions": "Be brief.", "reasoning": "default"})
        response = await self.client.get(url + "/settings", cookies=cookies)
        self.assertEqual(await response.json(), {"instructions": "Be brief.", "reasoning": "default"})
        # Hermes pages 500 rows at a time; the export reads until a short page.
        pages = [[{"role": "user", "content": str(i)} for i in range(500)], [{"role": "assistant", "content": "end"}]]
        self.app["hermes"].history.side_effect = pages
        response = await self.client.get(url + "/export", cookies=cookies)
        self.assertEqual(response.headers["Content-Disposition"], 'attachment; filename="conversation-chat_one.json"')
        exported = await response.json()
        self.assertEqual((len(exported["messages"]), exported["instructions"], exported["conversation"]), (501, "Be brief.", "chat:one"))
        self.assertEqual(self.app["hermes"].history.await_args_list[1].kwargs, {"offset": 500, "limit": 500})
        self.app["hermes"].history.side_effect = None
        self.app["hermes"].history.return_value = [{"role": "user"}] * 500
        response = await self.client.get(url + "/export", cookies=cookies)
        self.assertEqual(response.status, 400)
        response = await self.client.delete(url, headers=self.headers, json={"confirm": True}, cookies=cookies)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.app["chats"].instructions("chat:one"), "")

    async def test_working_and_where_the_turn_begins_come_from_the_server(self):
        await self.signed_in()
        now = time.time()
        earlier = [{"id": 1, "role": "user", "content": "hi", "timestamp": now - 60},
                   {"id": 2, "role": "assistant", "content": "Hello.", "timestamp": now - 50}]
        self.app["hermes"].messages.return_value = {"messages": earlier}
        data = await (await self.client.get("/api/conversations/chat:one")).json()
        self.assertEqual((data["working"], data["after"], data["live"]), (False, None, False))
        # Sent from Roost and accepted by Hermes, not saved yet: working, and the turn begins after row 2.
        server.watch_reply(self.app, "chat:one", now - 1)
        data = await (await self.client.get("/api/conversations/chat:one")).json()
        self.assertEqual((data["working"], data["after"]), (True, 2))
        self.app["hermes"].sessions.return_value = {"sessions": [{"id": "chat:one"}, {"id": "chat:two"}]}
        page = await (await self.client.get("/api/conversations")).json()
        self.assertEqual([item["working"] for item in page["sessions"]], [True, False])
        self.app["hermes"].messages.return_value = {"messages": earlier + [
            {"id": 3, "role": "user", "content": "again", "timestamp": now},
            {"id": 4, "role": "assistant", "content": "Done.", "timestamp": now + 1}]}
        data = await (await self.client.get("/api/conversations/chat:one")).json()
        self.assertEqual((data["working"], data["after"]), (False, 2))

    async def test_origin_and_auth_guards_and_error_headers(self):
        for headers in ({}, {**self.headers, "Origin": "https://other.example.invalid"}):
            response = await self.client.post("/api/login", headers=headers, json={"password": "example-demo-password"})
            self.assertEqual(response.status, 403)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        response = await self.client.get("/api/jobs")
        self.assertEqual(response.status, 401)
        self.app["hermes"].jobs.assert_not_awaited()

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
        for body in ({"agent_name": "x" * 41}, {"agent_name": "line\nbreak"}, {"accent": "neon"}, {"accent": "#12345g"}, {"accent": "#fff"}, {"accent": 5}, {"theme": "dark"}, {},
                     {"time_zone": "../etc"}, {"time_zone": "America/" + "x" * 70}, {"time_zone": 5}):
            response = await self.client.patch("/api/settings", headers=self.headers, json=body)
            self.assertEqual(response.status, 400, body)
        (self.state / "settings.json").write_text("{not json")
        self.assertEqual(server.load_settings(self.app), {"agent_name": "", "accent": "forest", "time_zone": ""})
        response = await self.client.patch("/api/settings", headers=self.headers, json={"agent_name": "  Robin  ", "accent": "rose"})
        self.assertEqual(response.status, 200)
        self.assertEqual(server.load_settings(self.app), {"agent_name": "Robin", "accent": "rose", "time_zone": ""})
        self.assertEqual(oct((self.state / "settings.json").stat().st_mode & 0o777), "0o600")
        response = await self.client.patch("/api/settings", headers=self.headers, json={"accent": "#A1B2C3"})
        self.assertEqual(response.status, 200)
        self.assertEqual(server.load_settings(self.app)["accent"], "#a1b2c3")

    async def test_delete_all_needs_the_typed_word_and_stops_on_rows_hermes_keeps(self):
        await self.signed_in()
        for confirm in (True, "delete", None):
            response = await self.client.delete("/api/conversations", headers=self.headers, json={"confirm": confirm})
            self.assertEqual(response.status, 400)
        self.app["hermes"].sessions.assert_not_awaited()
        # Hermes acknowledges every delete but keeps listing the same row: Roost must not loop.
        self.app["hermes"].sessions.return_value = {"sessions": [{"id": "stuck"}]}
        response = await self.client.delete("/api/conversations", headers=self.headers, json={"confirm": "DELETE"})
        self.assertEqual((response.status, await response.json()), (200, {"deleted": 1}))

    async def test_agent_name_is_asked_of_hermes_right_after_boot(self):
        await self.signed_in()

        self.app["hermes"].profile.return_value = {"name": "default", "display_name": "Kestrel"}
        # A host that has just booted reports a small monotonic clock.
        with patch.object(server.time, "monotonic", return_value=5.0):
            response = await self.client.get("/api/me")
        self.assertEqual((await response.json())["agent"], "Kestrel")

    async def test_about_reports_service_stats_and_only_public_hermes_fields(self):
        await self.signed_in()
        self.app["hermes"].status.return_value = {"version": "2026.9.24", "gateway_running": True,
                                                  "hermes_home": "/private/path", "gateway_pid": 42}
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
        self.app["hermes"].status.side_effect = HermesError(503, "Hermes is unavailable.")
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
