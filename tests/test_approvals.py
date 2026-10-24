"""Approval protocol and owner-boundary regressions against a loopback Hermes stand-in."""

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import CookieJar, WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from roost import approvals, live, server
from roost.auth import make_password_hash
from roost.hermes import Hermes, HermesError
from hermes_fake import fake_hermes


PAYLOAD = {"request_id": "request-one", "command": "printf 'example note' > /workspace/note.md",
           "description": "Write a test note", "tool_name": "terminal", "choices": ["once", "session", "always", "deny"]}


class ApprovalViewTests(unittest.TestCase):
    def test_only_exact_once_or_deny_choices_and_redacted_resume_fields_are_exposed(self):
        item = approvals.view({**PAYLOAD, "command": "safe\u202eevil\x1b", "private": "not exposed"})
        self.assertEqual(item["choices"], ["once", "deny"])
        self.assertEqual(item["command"], "safeevil")
        self.assertNotIn("private", item)
        self.assertIsNone(approvals.view({"command": "no request identity"}))
        self.assertIsNone(approvals.view({**PAYLOAD, "choices": None}))
        rows = approvals.snapshot({"open_requests": [{"method": "approval", "id": "srv-one", "params": PAYLOAD},
                                                     {"method": "sudo", "params": {"secret": "hidden"}}],
                                   "pending_approval": PAYLOAD})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["server_id"], "srv-one")


class ApprovalProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls, self.pending, self.resolved = [], [dict(PAYLOAD)], 1
        async def socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for message in ws:
                frame = message.json()
                self.calls.append(frame)
                method = frame.get("method")
                if method == "client.capabilities":
                    result = {}
                elif method == "session.resume":
                    result = {"session_id": "runtime-one", "stored_session_id": "stored-one",
                              "open_requests": [{"id": "srv-one", "method": "approval", "params": {**p, "session_id": "runtime-one"}} for p in self.pending]}
                elif method == "approval.respond":
                    result = {"resolved": self.resolved}
                else:
                    result = {}
                await ws.send_json({"jsonrpc": "2.0", "id": frame["id"], "result": result})
            return ws
        app = web.Application()
        app.router.add_get("/api/ws", socket)
        self.upstream = TestServer(app)
        await self.upstream.start_server()
        self.hermes = Hermes(str(self.upstream.make_url("/")), "demo", Path("unused"))
        await self.hermes.start()
        self.hermes._login = AsyncMock()
        self.hermes._request = AsyncMock(return_value={"ticket": "example-ticket"})

    async def asyncTearDown(self):
        await self.hermes.close()
        await self.upstream.close()

    async def test_resume_replays_redacted_pending_requests_and_advertises_support(self):
        rows = await self.hermes.approvals("stored-one")
        self.assertEqual(rows[0]["request_id"], "request-one")
        self.assertEqual(self.calls[0]["params"], {"server_requests": True})
        self.assertEqual(self.calls[1]["params"]["session_id"], "stored-one")

    async def test_exact_fresh_request_is_resolved_without_global_or_session_allowances(self):
        self.assertEqual(await self.hermes.approvals("stored-one", "request-one", "once"), {"resolved": True})
        self.assertEqual(self.calls[-1]["params"], {"session_id": "runtime-one", "request_id": "request-one", "choice": "once"})
        for request_id, choice in (("foreign-request", "once"), ("request-one", "always"), ("request-one", "session")):
            with self.assertRaises(HermesError) as caught:
                await self.hermes.approvals("stored-one", request_id, choice)
            self.assertEqual(caught.exception.status, 409)
        self.assertEqual(sum(call["method"] == "approval.respond" for call in self.calls), 1)

    async def test_expired_and_racing_duplicate_decisions_never_report_success(self):
        self.pending = []
        with self.assertRaises(HermesError) as caught:
            await self.hermes.approvals("stored-one", "request-one", "deny")
        self.assertEqual(caught.exception.status, 409)
        self.pending, self.resolved = [PAYLOAD], 0
        with self.assertRaises(HermesError) as caught:
            await self.hermes.approvals("stored-one", "request-one", "once")
        self.assertEqual(caught.exception.status, 409)

    async def test_approval_frames_before_rpc_ack_are_buffered_and_unknown_requests_rejected(self):
        frames = [
            {"jsonrpc": "2.0", "id": "srv-approval", "method": "approval", "params": {**PAYLOAD, "session_id": "runtime-one"}},
            {"jsonrpc": "2.0", "id": "srv-unsupported", "method": "sudo", "params": {"session_id": "runtime-one"}},
            {"jsonrpc": "2.0", "id": 2, "result": {"status": "accepted"}},
        ]
        ws = SimpleNamespace(send_json=AsyncMock(), receive=AsyncMock(side_effect=[
            SimpleNamespace(type=WSMsgType.TEXT, json=lambda frame=frame: frame) for frame in frames]))
        early = []
        await Hermes._rpc_on_socket(ws, 2, "prompt.submit", {"session_id": "runtime-one"}, early)
        self.assertEqual(early, [frames[0]])
        sent = [call.args[0] for call in ws.send_json.await_args_list]
        self.assertEqual(sent[-1]["id"], "srv-unsupported")
        self.assertEqual(sent[-1]["error"]["code"], -32601)
        self.assertFalse(any(frame.get("result", {}).get("choice") for frame in sent))

    async def test_live_approval_arriving_before_prompt_ack_is_preserved(self):
        frame = {"jsonrpc": "2.0", "id": "srv-one", "method": "approval", "params": {**PAYLOAD, "session_id": "runtime-one"}}
        self.assertIsNone(Hermes._event(frame, "another-session"))
        event = Hermes._event(frame, "runtime-one")
        gate = asyncio.Event()
        async def events():
            yield event
            await gate.wait()
            yield ("request.cancel", {"id": "srv-one", "reason": "timeout"})
            await asyncio.sleep(0)
        turns = {}
        task = live.follow(turns, "stored-one", "test", events())
        turn = turns["stored-one"]
        await asyncio.sleep(0)
        self.assertIn("request-one", turn.approvals)
        gate.set()
        await task
        self.assertEqual(turn.approvals, {})


class ApprovalHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        state = Path(self.directory.name)
        (state / "password.hash").write_text(make_password_hash("example-demo-password"))
        with patch.dict(os.environ, {"ROOST_STATE_DIR": str(state), "ROOST_PUBLIC_ORIGIN": "http://localhost:8787",
                                    "ROOST_HERMES_URL": "http://127.0.0.1:1", "ROOST_HERMES_USERNAME": "demo",
                                    "ROOST_HERMES_PASSWORD_FILE": str(state / "unused")}):
            self.app = server.create_app()
        self.hermes = fake_hermes()
        self.app["hermes"] = self.hermes
        self.client = TestClient(TestServer(self.app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.headers = {"Origin": "http://localhost:8787", "X-Roost-Request": "1"}

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()

    async def test_decisions_require_login_origin_and_exact_fields(self):
        path = "/api/conversations/stored-one/approvals"
        body = {"request_id": "request-one", "choice": "once"}
        self.assertEqual((await self.client.get(path)).status, 401)
        self.assertEqual((await self.client.post(path, headers=self.headers, json=body)).status, 401)
        cookie = {server.COOKIE: self.app["sessions"].issue()}
        self.assertEqual((await self.client.post(path, cookies=cookie, json=body)).status, 403)
        for bad_body in ({**body, "all": True}, {**body, "choice": "always"}, {**body, "choice": []}, {"choice": "once"}):
            response = await self.client.post(path, cookies=cookie, headers=self.headers, json=bad_body)
            self.assertEqual(response.status, 400)
        self.hermes.approvals.assert_not_awaited()
        self.hermes.approvals.return_value = {"resolved": True}
        response = await self.client.post(path, cookies=cookie, headers=self.headers, json=body)
        self.assertEqual(response.status, 200)
        self.hermes.approvals.assert_awaited_once_with("stored-one", "request-one", "once")
        self.hermes.approvals.side_effect = HermesError(409, "Expired")
        self.assertEqual((await self.client.post(path, cookies=cookie, headers=self.headers, json=body)).status, 409)

    async def test_pending_approval_notifies_once_with_summary_and_exact_identity(self):
        item = approvals.view(PAYLOAD, "srv-one")
        turn = live.Turn("test")
        turn.approvals[item["request_id"]] = item
        self.app["live"]["stored-one"] = turn
        server.watch_reply(self.app, "stored-one", time.time())
        with patch.object(server, "notify", new_callable=AsyncMock) as notify, patch.object(server, "agent_name", new_callable=AsyncMock, return_value="Kestrel"):
            await server.check_replies(self.app)
            await server.check_replies(self.app)
        self.assertEqual(notify.await_count, 1)
        message = notify.call_args.args[1]
        self.assertIn("Write a test note", message["body"])
        self.assertEqual(message["approval"], {"conversation": "stored-one", "request_id": "request-one", "choices": ["once", "deny"]})
        self.assertIn("&a=request-one", message["url"])
        del self.app["live"]["stored-one"]

    async def test_approval_first_seen_while_viewing_is_pushed_once_the_owner_leaves(self):
        item = approvals.view(PAYLOAD, "srv-one")
        turn = live.Turn("test")
        turn.approvals[item["request_id"]] = item
        self.app["live"]["stored-one"] = turn
        server.watch_reply(self.app, "stored-one", time.time())
        with patch.object(server, "notify", new_callable=AsyncMock) as notify, patch.object(server, "agent_name", new_callable=AsyncMock, return_value="Kestrel"):
            # The owner is looking at this conversation when the request first appears: no push, and no mark.
            self.app["presence"]["device"] = (time.monotonic(), "stored-one")
            await server.check_replies(self.app)
            self.assertEqual(notify.await_count, 0)
            # The owner leaves while the request is still pending: exactly one push.
            del self.app["presence"]["device"]
            await server.check_replies(self.app)
            self.assertEqual(notify.await_count, 1)
            # A later pass does not push again.
            await server.check_replies(self.app)
            self.assertEqual(notify.await_count, 1)
        del self.app["live"]["stored-one"]
