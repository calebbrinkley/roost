"""Guard tests use fake DNS only; no request can leave the test process."""

import asyncio
import json
import ipaddress
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from roost.tools import PublicResolver, Readable, ToolError, WebTools, page_url, public_ip
from web_fake import WebFake


def ipv4(*octets):
    """Build synthetic policy-test addresses, never copied device configuration."""
    return str(ipaddress.IPv4Address(bytes(octets)))


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_address_and_url_policy(self):
        blocked = ('127.0.0.1', ipv4(10, 0, 0, 1), ipv4(172, 16, 0, 1), ipv4(192, 168, 0, 1), '169.254.1.1', '224.0.0.1',
                   '0.0.0.0', '240.0.0.1', ipv4(100, 64, 0, 1), ipv4(100, 127, 255, 254), '::', '::1', 'fc00::1',
                   'fe80::1', 'ff02::1', '::ffff:8.8.8.8', '::8.8.8.8', '2001:db8::1')
        for address in blocked:
            with self.subTest(address=address), self.assertRaises(ToolError):
                public_ip(address)
        for address in ('8.8.8.8', '2606:4700:4700::1111'):
            public_ip(address)
        for url in ('ftp://example.org', 'file:///etc/passwd', 'http://example.org:8080',
                    'https://REPLACE_USER:REPLACE_PASSWORD@example.org', 'http://[::ffff:8.8.8.8]/',
                    'http://[::8.8.8.8]/', 'http://' + ipv4(100, 64, 0, 1), 'http://[fe80::1%25en0]'):
            with self.subTest(url=url), self.assertRaises(ToolError):
                page_url(url)
        self.assertEqual(page_url('https://example.org:443/page'), 'https://example.org:443/page')

    async def test_every_exact_dns_record_is_checked_on_each_resolution(self):
        record = lambda ip: {'hostname': 'example.org', 'host': ip, 'port': 443, 'family': socket.AF_INET, 'proto': 0, 'flags': 0}
        dns = SimpleNamespace(resolve=AsyncMock(side_effect=[[record('8.8.8.8')], [record('8.8.8.8'), record(ipv4(10, 0, 0, 1))],
                                                            [record(ipv4(100, 64, 0, 1))], [record('::ffff:8.8.8.8')], []]), close=AsyncMock())
        resolver = PublicResolver(dns)
        self.assertEqual((await resolver.resolve('example.org', 443))[0]['host'], '8.8.8.8')
        for _ in range(4):
            with self.assertRaises(ToolError):
                await resolver.resolve('example.org', 443)
        self.assertEqual(dns.resolve.await_count, 5)
        await resolver.close()
        dns.close.assert_awaited_once()

    async def test_connector_blocks_dns_before_any_socket_connect(self):
        dns = SimpleNamespace(resolve=AsyncMock(return_value=[{'hostname': 'example.org', 'host': '127.0.0.1',
                              'port': 80, 'family': socket.AF_INET, 'proto': 0, 'flags': 0}]), close=AsyncMock())
        tools = WebTools('', resolver_factory=lambda: PublicResolver(dns))
        with patch('aiohappyeyeballs.start_connection', new_callable=AsyncMock) as connect:
            result = await tools.execute('read_page', json.dumps({'url': 'http://example.org/page'}))
        self.assertIn('error', result)
        connect.assert_not_awaited()
        dns.resolve.assert_awaited_once()
        for host in ('127.1', '2130706433', '0x7f000001'):
            with patch('aiohappyeyeballs.start_connection', new_callable=AsyncMock) as connect:
                result = await tools.execute('read_page', json.dumps({'url': 'http://' + host}))
            self.assertIn('error', result)
            connect.assert_not_awaited()

    async def test_connector_uses_only_the_exact_checked_dns_records(self):
        dns = SimpleNamespace(resolve=AsyncMock(return_value=[
            {'hostname': 'example.org', 'host': address, 'port': 80, 'family': socket.AF_INET, 'proto': 0, 'flags': 0}
            for address in ('8.8.8.8', '1.1.1.1')]), close=AsyncMock())
        tools = WebTools('', resolver_factory=lambda: PublicResolver(dns))
        connected = []
        async def refuse_socket(*, addr_infos, **kwargs):
            connected.extend(info[4][0] for info in addr_infos)
            raise OSError('Synthetic socket refusal')
        with patch('aiohappyeyeballs.start_connection', side_effect=refuse_socket):
            result = await tools.execute('read_page', '{"url":"http://example.org/page"}')
        self.assertIn('error', result)
        self.assertTrue(connected)
        self.assertEqual(set(connected), {'8.8.8.8', '1.1.1.1'})
        self.assertTrue(all(args.args[0] == 'example.org' for args in dns.resolve.await_args_list))

    async def test_cancel_page_fetch_closes_connection(self):
        fake = await WebFake().start()
        try:
            fake.page_delay = .5
            task = asyncio.create_task(fake.tools('').execute('read_page', '{"url":"http://example.org/page"}'))
            await asyncio.wait_for(fake.page_started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, .2)
            async with asyncio.timeout(.2):
                while not fake.transports[-1].is_closing():
                    await asyncio.sleep(.01)
        finally:
            await fake.close()

    async def test_extraction_redirects_limits_and_credential_isolation(self):
        fake = await WebFake().start()
        try:
            tools = fake.tools(fake.url)
            search = await tools.execute('web_search', '{"query":"example"}')
            self.assertTrue(search['untrusted_data'])
            page = await tools.execute('read_page', '{"url":"http://example.org/page"}')
            self.assertEqual(page['text'], 'Example Readable page text.')
            self.assertTrue(page['untrusted_data'])
            plain = await tools.execute('read_page', '{"url":"http://example.org/plain"}')
            self.assertEqual(len(plain['text']), 20000)
            for path in ('redirect-private', 'redirect-loop', 'large', 'compressed', 'binary'):
                result = await tools.execute('read_page', json.dumps({'url': 'http://example.org/' + path}))
                self.assertIn('error', result, path)
            before = len(fake.requests)
            page = await tools.execute('read_page', '{"url":"http://example.org/redirect-public"}')
            self.assertEqual(page['url'], 'http://example.com/page')
            self.assertEqual(len(fake.requests) - before, 2)
            for _, headers in fake.requests:
                self.assertNotIn('Cookie', headers)
                self.assertNotIn('Authorization', headers)
            fake.results *= 6
            self.assertEqual(len((await tools.web_search('example'))['results']), 6)
        finally:
            await fake.close()

    async def test_redirect_dns_revalidated_and_no_cached_connections(self):
        fake = await WebFake().start()
        try:
            tools = fake.tools(fake.url)
            local = tools.resolver_factory()
            original = local.resolve
            local.resolve = AsyncMock(side_effect=[await original('example.org', 80), ToolError('Page address is not public.')])
            tools.resolver_factory = lambda: local
            result = await tools.execute('read_page', '{"url":"http://example.org/redirect-public"}')
            self.assertIn('error', result)
            self.assertEqual([args.args[0] for args in local.resolve.await_args_list], ['example.org', 'example.com'])
            self.assertEqual(len(fake.requests), 1)
        finally:
            await fake.close()

    async def test_timeout_bad_arguments_and_text_cap(self):
        tools = WebTools('http://example.org')
        for name, args in (('unknown', '{}'), ('web_search', '{broken'), ('web_search', '[]'),
                           ('web_search', '{"query":true}'), ('read_page', '{"url":"x","other":1}')):
            self.assertIn('error', await tools.execute(name, args))
        async def slow(query):
            await asyncio.Event().wait()
        with patch.object(tools, 'web_search', slow), patch('roost.tools.CALL_SECONDS', .01):
            self.assertIn('error', await tools.execute('web_search', '{"query":"example"}'))
        parser = Readable()
        parser.feed('<p>' + 'word ' * 10000 + '</p>')
        self.assertEqual(len(parser.text()), 20000)


class RememberTests(unittest.TestCase):
    def test_save_duplicate_length_validation_and_budget(self):
        import tempfile
        from pathlib import Path
        from roost.store import Store
        from roost.tools import remember
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'native.sqlite3')
            try:
                cid = store.create_conversation().id
                turn = store.accept_turn(cid, 'remember', 'Remember this', model='example-model')
                def call(fact):
                    return remember(store, json.dumps({'fact': fact}), turn.assistant_message_id, cid)
                saved = call('  The user likes short answers.  ')
                self.assertEqual(saved['status'], 'saved')
                self.assertEqual(saved['fact'], 'The user likes short answers.')
                self.assertEqual(store.memory_fact(saved['id'])['origin'], 'model')
                self.assertIn('Duplicate', call('the USER likes SHORT answers.')['error'])
                self.assertEqual(call('x' * 501)['error'], 'Too long.')
                for arguments in ('{broken', '[]', '{}', '{"fact":3}', '{"fact":""}', '{"fact":"Fact","extra":true}'):
                    self.assertIn('error', remember(store, arguments, turn.assistant_message_id, cid))
                store.delete_memory(saved['id'])
                for n in range(16):
                    store.save_memory(str(n).zfill(3) + 'x' * 497)
                self.assertEqual(store.memory_view()['used_tokens'], 2000)
                self.assertEqual(call('New fact')['error'], 'Memory full.')
                # Manual adds may cross the limit; model saves may not.
                store.save_memory('Manual addition')
                self.assertGreater(store.memory_view()['used_tokens'], 2000)
                self.assertEqual(call('Another fact')['error'], 'Memory full.')
                self.assertEqual(store.memory_view()['budget_tokens'], 2000)
            finally:
                store.close()
