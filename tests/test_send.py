"""Idempotent browser sends, including ambiguous Hermes acknowledgements."""

import asyncio
import sqlite3
import time
import unittest
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from aiohttp import ClientConnectionError, web
from aiohttp.test_utils import TestClient, TestServer

from roost import chats, server
from roost.hermes import Hermes, HermesError
from hermes_fake import fake_hermes


class SendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hermes = fake_hermes()
        self.hermes.send_message.return_value = "created"
        self.db = sqlite3.connect(":memory:")
        app = web.Application(middlewares=[server.guard])
        app.update(origin="https://roost.example.invalid", sessions=SimpleNamespace(valid=lambda token: True),
                   hermes=self.hermes, chats=chats.Chats(self.db), watches={}, presence={}, live={}, send_requests=OrderedDict())
        app.router.add_post("/api/conversations", server.create_conversation)
        app.router.add_post("/api/conversations/{sid}/messages", server.send_message)
        self.app = app
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.headers = {"Origin": app["origin"], "X-Roost-Request": "1"}

    async def asyncTearDown(self):
        await self.client.close()
        self.db.close()

    async def post(self, path, key, text="Hello"):
        response = await self.client.post(path, headers=self.headers, json={"text": text, "request_id": key})
        return response.status, await response.json()

    async def test_duplicate_done_keys_replay_the_original_response_for_both_routes(self):
        for path, status in (("/api/conversations", 201), ("/api/conversations/one/messages", 200)):
            with self.subTest(path=path):
                self.hermes.send_message.reset_mock()
                key = str(uuid4())
                first = await self.post(path, key)
                self.assertEqual(first, (status, {"session_id": "created"}))
                self.assertEqual(await self.post(path, key), first)
                self.hermes.send_message.assert_awaited_once()
                self.assertEqual(self.app["send_requests"][key]["state"], "done")

    async def test_pending_key_is_reserved_before_hermes_and_returns_409(self):
        for path in ("/api/conversations", "/api/conversations/one/messages"):
            started, finish = asyncio.Event(), asyncio.Event()
            async def send(*args, **kwargs):
                started.set()
                await finish.wait()
                return "created"
            self.hermes.send_message.side_effect = send
            self.hermes.send_message.reset_mock()
            key = str(uuid4())
            first = asyncio.create_task(self.post(path, key))
            await started.wait()
            try:
                status, body = await self.post(path, key)
                self.assertEqual(status, 409)
                self.assertIs(body["pending"], True)
                self.hermes.send_message.assert_awaited_once()
            finally:
                finish.set()
                await first

    async def test_definite_failures_release_keys_so_retry_can_submit(self):
        for path in ("/api/conversations", "/api/conversations/one/messages"):
            for error in (HermesError(400, "Rejected"), HermesError(503, "Not connected", not_accepted=True)):
                with self.subTest(path=path, error=error):
                    key = str(uuid4())
                    self.hermes.send_message.reset_mock()
                    self.hermes.send_message.side_effect = [error, "created"]
                    self.assertEqual((await self.post(path, key))[0], error.status)
                    self.assertNotIn(key, self.app["send_requests"])
                    self.assertIn((await self.post(path, key))[0], (200, 201))
                    self.assertEqual(self.hermes.send_message.await_count, 2)

    async def test_unknown_acceptance_stays_pending_even_when_transcript_is_queued_or_coalesced(self):
        for path in ("/api/conversations", "/api/conversations/one/messages"):
            key = str(uuid4())
            self.hermes.send_message.reset_mock()
            self.hermes.send_message.side_effect = HermesError(503, "Response lost after submit")
            self.assertEqual((await self.post(path, key))[0], 503)
            for transcript in ([], [{"role": "user", "content": "Hello\n\nAnother prompt"}]):
                self.hermes.messages.return_value = {"messages": transcript}
                status, body = await self.post(path, key)
                self.assertEqual(status, 409)
                self.assertIs(body["pending"], True)
            self.hermes.send_message.assert_awaited_once()
            self.hermes.messages.assert_not_awaited()

    async def test_identical_text_with_distinct_keys_is_two_deliberate_sends(self):
        await self.post("/api/conversations/one/messages", str(uuid4()))
        await self.post("/api/conversations/one/messages", str(uuid4()))
        self.assertEqual(self.hermes.send_message.await_count, 2)

    async def test_invalid_keys_are_rejected_before_hermes(self):
        for path in ("/api/conversations", "/api/conversations/one/messages"):
            for key in (None, "", "not-a-key", "a" * 1000, 12, [], str(uuid4()).upper()):
                with self.subTest(path=path, key=key):
                    self.assertEqual((await self.post(path, key))[0], 400)
        self.hermes.send_message.assert_not_awaited()

    async def test_cache_evicts_oldest_at_bound_and_expires_pending_entries(self):
        entries = self.app["send_requests"]
        for index in range(server.SEND_LIMIT):
            entries[str(uuid4())] = {"state": "pending", "session_id": None, "started": time.monotonic()}
        oldest = next(iter(entries))
        key = str(uuid4())
        await self.post("/api/conversations", key)
        self.assertEqual(len(entries), server.SEND_LIMIT)
        self.assertNotIn(oldest, entries)
        self.assertIn(key, entries)
        for entry in entries.values():
            entry["started"] -= server.SEND_TTL
        self.hermes.send_message.reset_mock()
        await self.post("/api/conversations", oldest)
        self.assertEqual(list(entries), [oldest])
        self.hermes.send_message.assert_awaited_once()


class SendAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_failures_before_prompt_submission_are_known_not_accepted(self):
        hermes = Hermes("http://localhost", "demo", Path("unused"))
        hermes._socket = AsyncMock(side_effect=HermesError(503, "Connection refused"))
        with self.assertRaises(HermesError) as caught:
            await hermes.send_message("Hello")
        self.assertTrue(caught.exception.not_accepted)
        socket = SimpleNamespace(close=AsyncMock())
        hermes._socket = AsyncMock(return_value=socket)
        hermes._rpc_on_socket = AsyncMock(side_effect=asyncio.TimeoutError())
        with self.assertRaises(HermesError) as caught:
            await hermes.send_message("Hello")
        self.assertTrue(caught.exception.not_accepted)

    async def test_failures_after_prompt_submission_remain_ambiguous(self):
        for error in (asyncio.TimeoutError(), ClientConnectionError(), HermesError(502, "Socket dropped")):
            hermes = Hermes("http://localhost", "demo", Path("unused"))
            hermes._socket = AsyncMock(return_value=SimpleNamespace(close=AsyncMock()))
            hermes._rpc_on_socket = AsyncMock(side_effect=[{"session_id": "one"}, error])
            with self.assertRaises(HermesError) as caught:
                await hermes.send_message("Hello")
            self.assertFalse(caught.exception.not_accepted)

    async def test_explicit_prompt_rejections_carry_non_acceptance(self):
        for code in (400, 403, 409, 4002):
            frame = {"id": 2, "error": {"code": code}}
            ws = SimpleNamespace(send_json=AsyncMock(), receive=AsyncMock(return_value=SimpleNamespace(type=1, json=lambda: frame)))
            with self.assertRaises(HermesError) as caught:
                await Hermes._rpc_on_socket(ws, 2, "prompt.submit", {})
            self.assertTrue(caught.exception.not_accepted)


if __name__ == "__main__":
    unittest.main()
