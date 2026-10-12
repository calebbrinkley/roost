"""Voice: transcription, replies read aloud (with byte ranges for Safari) and the silence clip."""

import asyncio
import base64
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from roost import server
from roost.auth import make_password_hash

AUDIO = b"ID3-fake-mp3-bytes"


class SpeakableTests(unittest.TestCase):
    def test_code_links_tables_and_markdown_are_not_read(self):
        reply = "**Two** things:\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```py\nprint(1)\n```\nSee [the docs](https://example.invalid). `ok`"
        self.assertEqual(server.speakable(reply), "Two things: a b 1 2 See the docs. ok")
        self.assertEqual(len(server.speakable("word " * 5000)), server.SPEECH_CHARACTERS)


class VoiceRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        state = Path(self.directory.name)
        (state / "password.hash").write_text(make_password_hash("example-demo-password"))
        (state / "upstream-password").write_text("upstream")
        with patch.dict(os.environ, {
            "ROOST_PUBLIC_ORIGIN": "https://roost.example.invalid", "ROOST_STATE_DIR": str(state),
            "ROOST_HERMES_URL": "http://127.0.0.1:1", "ROOST_HERMES_USERNAME": "demo",
            "ROOST_HERMES_PASSWORD_FILE": str(state / "upstream-password"),
        }):
            self.app = server.create_app()
        self.calls = []
        self.speak_delay = 0
        async def request(method, path, body=None, *, timeout=45):
            self.calls.append((method, path, body, timeout))
            if path.startswith("/api/sessions/"):
                return {"messages": [{"id": 1, "role": "user", "content": "hi"}, {"id": 2, "role": "assistant", "content": "**Hello** there."}]}
            if path == "/api/audio/speak":
                await asyncio.sleep(self.speak_delay)
                return {"ok": True, "data_url": "data:audio/mpeg;base64," + base64.b64encode(AUDIO).decode()}
            if path == "/api/audio/transcribe":
                return {"ok": True, "transcript": "  what is on today  "}
            return {}
        self.app["hermes"] = SimpleNamespace(start=lambda: asyncio.sleep(0), close=lambda: asyncio.sleep(0), request=request)
        self.client = TestClient(TestServer(self.app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.headers = {"Origin": "https://roost.example.invalid", "X-Roost-Request": "1"}
        response = await self.client.post("/api/login", headers=self.headers, json={"password": "example-demo-password"})
        self.cookies = {server.COOKIE: response.cookies[server.COOKIE].value}

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()

    async def test_a_recording_is_transcribed_by_hermes(self):
        response = await self.client.post("/api/audio/transcribe", headers=self.headers, json={"data_url": "x"})
        self.assertEqual(response.status, 401)
        for body in ({"data_url": "data:audio/mp4;base64,AAAA", "mime_type": "text/html"},
                     {"data_url": "https://example.invalid/a.mp4", "mime_type": "audio/mp4"}, {}):
            response = await self.client.post("/api/audio/transcribe", headers=self.headers, json=body, cookies=self.cookies)
            self.assertEqual(response.status, 400, body)
        recording = "data:audio/webm;codecs=opus;base64," + "A" * 200000  # over the app-wide body limit
        response = await self.client.post("/api/audio/transcribe", headers=self.headers, cookies=self.cookies,
                                          json={"data_url": recording, "mime_type": "audio/webm;codecs=opus"})
        self.assertEqual(await response.json(), {"transcript": "what is on today"})
        self.assertEqual((self.calls[-1][1], self.calls[-1][3]), ("/api/audio/transcribe", 180))

    async def test_a_reply_is_read_aloud_once_with_byte_ranges(self):
        url = "/api/conversations/chat:one/speech/2"
        self.speak_delay = 0.05
        first, second = await asyncio.gather(self.client.get(url, cookies=self.cookies),
                                             self.client.get(url, headers={"Range": "bytes=0-1"}, cookies=self.cookies))
        self.assertEqual((first.status, first.content_type, await first.read()), (200, "audio/mpeg", AUDIO))
        self.assertEqual((second.status, second.headers["Content-Range"], await second.read()),
                         (206, f"bytes 0-1/{len(AUDIO)}", AUDIO[:2]))
        response = await self.client.get(url, headers={"Range": "bytes=-3"}, cookies=self.cookies)
        self.assertEqual(await response.read(), AUDIO[-3:])
        response = await self.client.get(url, headers={"Range": "bytes=999-"}, cookies=self.cookies)
        self.assertEqual(response.status, 416)
        spoken = [call for call in self.calls if call[1] == "/api/audio/speak"]
        self.assertEqual(spoken, [("POST", "/api/audio/speak", {"text": "Hello there."}, 180)], "one synthesis, then the cache")
        response = await self.client.get("/api/conversations/chat:one/speech/1", cookies=self.cookies)
        self.assertEqual(response.status, 404, "only the agent's replies are read")
        response = await self.client.get(url)
        self.assertEqual(response.status, 401)

    async def test_the_silence_clip_is_a_short_wav(self):
        response = await self.client.get("/api/audio/silence", cookies=self.cookies)
        body = await response.read()
        self.assertEqual((response.content_type, body[:4], body[8:12]), ("audio/wav", b"RIFF", b"WAVE"))
