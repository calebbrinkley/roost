"""Loopback search/page fixtures. Only tests opt into routing a public hostname here."""

import asyncio
import socket

from aiohttp import web
from aiohttp.abc import AbstractResolver
from aiohttp.test_utils import TestServer

from roost.tools import WebTools


class WebFake:
    def __init__(self):
        self.requests, self.started = [], asyncio.Event()
        self.delay = self.page_delay = 0
        self.transports, self.page_started = [], asyncio.Event()
        self.results = [{'title': 'Example source', 'url': 'http://example.org/page', 'content': 'A search snippet.'},
                        {'title': 'Example source again', 'url': 'http://example.org/page', 'content': 'Duplicate.'},
                        {'title': 'Another source', 'url': 'https://example.com/other', 'content': 'Another snippet.'}]
        app = web.Application()
        app.router.add_get('/search', self.search)
        app.router.add_get('/{tail:.*}', self.page)
        self.server = TestServer(app)

    async def start(self):
        await self.server.start_server()
        return self

    @property
    def url(self):
        return str(self.server.make_url('')).rstrip('/')

    async def close(self):
        await self.server.close()

    async def search(self, request):
        self.requests.append((request.path, dict(request.headers)))
        self.transports.append(request.transport)
        assert request.query['format'] == 'json' and request.query['q']
        self.started.set()
        await asyncio.sleep(self.delay)
        return web.json_response({'results': self.results})

    async def page(self, request):
        self.requests.append((request.path, dict(request.headers)))
        self.transports.append(request.transport)
        self.page_started.set()
        await asyncio.sleep(self.page_delay)
        if request.path == '/redirect-private':
            raise web.HTTPFound('http://127.0.0.1/page')
        if request.path == '/redirect-public':
            response = web.HTTPFound('http://example.com/page')
            response.set_cookie('untrusted', 'example-cookie')
            raise response
        if request.path == '/redirect-loop':
            raise web.HTTPFound('/redirect-loop')
        if request.path == '/large':
            return web.Response(text='x' * (2 * 1024 * 1024 + 1))
        if request.path == '/plain':
            return web.Response(text='word  ' * 10000, content_type='text/plain')
        if request.path == '/compressed':
            response = web.Response(text='x' * (2 * 1024 * 1024 + 1))
            response.enable_compression()
            return response
        if request.path == '/binary':
            return web.Response(body=b'fake image', content_type='image/png')
        return web.Response(text='<nav>Navigation</nav><style>hidden</style><script>unsafe()</script><h1>Example</h1><p>Readable page text.</p>', content_type='text/html')

    def tools(self, base):
        port = self.server.port
        class LocalPages(AbstractResolver):
            async def resolve(self, host, port=0, family=socket.AF_INET):
                return [{'hostname': host, 'host': '127.0.0.1', 'port': self.port, 'family': socket.AF_INET,
                         'proto': 0, 'flags': 0}]

            async def close(self):
                pass
        LocalPages.port = port
        return WebTools(base, resolver_factory=LocalPages)
