"""Deterministic speech HTTP fixture shared by tests and smoke."""

import asyncio

from aiohttp import web
from aiohttp.test_utils import TestServer


class SpeechFake:
    def __init__(self):
        self.calls, self.status, self.plain = [], 200, False
        self.audio = b'example-mp3'
        self.transcript = 'Hello'
        app = web.Application()
        app.router.add_post('/v1/audio/transcriptions', self.transcribe)
        app.router.add_post('/v1/audio/speech', self.speak)
        self.server = TestServer(app)

    @property
    def url(self):
        return str(self.server.make_url('/v1'))

    async def start(self):
        await self.server.start_server()
        return self

    async def close(self):
        await self.server.close()

    async def transcribe(self, request):
        form, fields = await request.multipart(), {}
        while part := await form.next():
            fields[part.name] = (part.filename, part.headers.get('Content-Type'), bytes(await part.read()))
        self.calls.append((request.headers.get('Authorization'), fields))
        if self.status != 200:
            return web.Response(status=self.status, text='cpu_busy synthetic upstream details')
        return web.Response(text=self.transcript) if self.plain else web.json_response({'text': self.transcript})

    async def speak(self, request):
        self.calls.append((request.headers.get('Authorization'), await request.json()))
        await asyncio.sleep(.03)
        return web.Response(status=self.status, body=self.audio, content_type='audio/mpeg')
