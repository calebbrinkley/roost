"""A configurable local OpenAI-compatible server; no model or credential leaves the test process."""

import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import TestServer


def frame(delta=None, *, finish=None, usage=None):
    value = {"choices": [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage is not None:
        value["usage"] = usage
    return b"data: " + json.dumps(value, ensure_ascii=False).encode() + b"\n\n"


class OpenAIFake:
    def __init__(self):
        self.status = 200
        self.error_body = "Synthetic upstream error"
        self.frames = frame({"content": "Hello"}) + frame({}, finish="stop") + b"data: [DONE]\n\n"
        self.chunk_sizes = (1, 2, 7, 3, 19)
        self.content_type = "text/event-stream"
        self.models = ["example-model", "another-model"]
        self.requests = []
        self.delay = 0
        self.completion = {"choices": [{"message": {"role": "assistant", "content": "Hello"}, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 3, "completion_tokens": 1}}
        app = web.Application()
        app.router.add_get("/v1/models", self.list_models)
        app.router.add_post("/v1/chat/completions", self.chat)
        self.server = TestServer(app)

    async def start(self):
        await self.server.start_server()
        return self

    @property
    def url(self):
        return str(self.server.make_url("/v1"))

    async def close(self):
        await self.server.close()

    async def list_models(self, request):
        self.requests.append((request.method, dict(request.headers), None))
        if self.status != 200:
            return web.Response(status=self.status, text=self.error_body)
        return web.json_response({"data": [{"id": mid} for mid in self.models]})

    async def chat(self, request):
        payload = await request.json()
        self.requests.append((request.method, dict(request.headers), payload))
        await asyncio.sleep(self.delay)
        if self.status != 200:
            return web.Response(status=self.status, text=self.error_body)
        if not payload["stream"]:
            return web.json_response(self.completion)
        stream = web.StreamResponse(headers={"Content-Type": self.content_type})
        await stream.prepare(request)
        offset, index = 0, 0
        try:
            while offset < len(self.frames):
                size = self.chunk_sizes[index % len(self.chunk_sizes)]
                await stream.write(self.frames[offset:offset + size])
                offset, index = offset + size, index + 1
                await asyncio.sleep(0)
            await stream.write_eof()
        except ConnectionResetError:
            pass
        return stream
