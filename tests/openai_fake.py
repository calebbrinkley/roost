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
        self.scenario = None
        self.reject_tool_choice = False
        self.status = 200
        self.error_body = "Synthetic upstream error"
        self.frames = frame({"content": "Hello"}) + frame({}, finish="stop") + b"data: [DONE]\n\n"
        self.chunk_sizes = (1, 2, 7, 3, 19)
        self.content_type = "text/event-stream"
        self.models = ["example-model", "another-model"]
        self.requests = []
        self.delay = 0
        self.chunk_delay = 0
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
        if payload.get("tool_choice") and self.reject_tool_choice:
            return web.json_response({"error": "tool_choice unsupported"}, status=400)
        if not payload["stream"]:
            return web.json_response(self.completion)
        stream = web.StreamResponse(headers={"Content-Type": self.content_type})
        await stream.prepare(request)
        frames = self.tool_frames(payload) if self.scenario else self.frames
        offset, index = 0, 0
        try:
            while offset < len(frames):
                size = self.chunk_sizes[index % len(self.chunk_sizes)]
                await stream.write(frames[offset:offset + size])
                offset, index = offset + size, index + 1
                await asyncio.sleep(self.chunk_delay)
            await stream.write_eof()
        except ConnectionResetError:
            pass
        return stream

    def tool_frames(self, payload):
        if self.scenario == 'remember':
            if not any(m['role'] == 'tool' for m in payload['messages']):
                return frame({'reasoning_content': 'I will keep this writing preference for future chats.'}) + frame({
                    'tool_calls': [{'index': 0, 'id': 'save-preference', 'type': 'function', 'function': {
                        'name': 'remember', 'arguments': json.dumps({'fact': 'The user prefers short paragraphs.'})}}]}, finish='tool_calls')
            return frame({'content': 'I will keep that in mind.'}) + frame({}, finish='stop')
        if self.scenario == 'capture' and not any('Search the web' in m.get('content', '') for m in payload['messages'] if m['role'] == 'user'):
            return self.frames
        results = [m for m in payload['messages'] if m['role'] == 'tool']
        if not any(t['function']['name'] == 'web_search' for t in payload.get('tools', [])) or (len(results) >= (1 if self.scenario == 'capture' else 2) and self.scenario != 'cap'):
            return frame({'reasoning_content': 'The sources agree.'}) + frame({'content': 'A useful answer [1].'}) + frame({}, finish='stop', usage={'prompt_tokens': 128, 'completion_tokens': 64})
        if self.scenario == 'invalid' and not results:
            return tool_frames([('unknown', '{}'), ('web_search', '{broken'), ('read_page', '{"url": 3}')])
        if results and self.scenario != 'cap':
            return tool_frames([('read_page', json.dumps({'url': 'http://example.org/page'}))])
        return tool_frames([('web_search', json.dumps({'query': 'example question'}))], prefix=str(len(results)))


def tool_frames(calls, prefix='0'):
    chunks = [frame({'reasoning_content': 'I will check a source.'}), frame({'content': 'Checking the web.'})]
    for index, (name, args) in enumerate(calls):
        chunks.append(frame({'tool_calls': [{'index': index, 'id': 'call-' + prefix + '-' + str(index),
                                           'type': 'function', 'function': {'name': name[:3], 'arguments': args[:4]}}]}))
        chunks.append(frame({'tool_calls': [{'index': index, 'function': {'name': name[3:], 'arguments': args[4:]}}]}))
    return b''.join(chunks) + frame({}, finish='tool_calls')
