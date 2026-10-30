"""Chat route, persistence, runner and control contracts against a loopback provider."""

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from aiohttp.test_utils import TestClient, TestServer

from roost import server, settings
from roost.auth import make_password_hash
from roost.provider import Event
from roost.runner import Runner
from roost.store import Store, StoreError, DEFAULT_PERSONA_PROMPT
from roost.servers import Servers, ServerError
from openai_fake import OpenAIFake, frame, tool_frames
from roost.tools import REMEMBER_GUIDANCE


async def wait_for(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.01)


def without_clock(messages):
    return [m for m in messages if m['role'] != 'system']


class ChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = Path(self.directory.name)
        (self.state / 'password.hash').write_text(make_password_hash('example-password'))
        self.fake = await OpenAIFake().start()
        self.fake.frames = frame({'reasoning_content': 'Thinking'}) + frame({'content': 'Hello'}) + frame({}, finish='stop', usage={'prompt_tokens': 12, 'completion_tokens': 3})
        self.fake.completion['choices'][0]['message']['content'] = 'A short title'
        self.fake.delay = .05
        models = Servers(self.state, environ={})
        models.update('primary', {'base_url': self.fake.url})
        models.set_models('primary', {'models': [{'id': 'example-model', 'display_name': 'Example', 'enabled': True,
                                                  'thinking_mode': 'template_flag', 'context_tokens': 4096}],
                                      'default_model': 'example-model'})
        with patch.dict(os.environ, {'ROOST_PUBLIC_ORIGIN': 'http://localhost', 'ROOST_STATE_DIR': str(self.state)}):
            self.app = server.create_app()
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.client.session.cookie_jar.update_cookies({server.COOKIE: self.app['sessions'].issue()})
        self.headers = {'Origin': 'http://localhost', 'X-Roost-Request': '1'}
        self.store = self.app['store']

    async def asyncTearDown(self):
        await self.client.close()
        await self.fake.close()
        self.directory.cleanup()

    async def request(self, method, path, data=None, status=200):
        async with self.client.request(method, path, headers=self.headers, json=data) as response:
            result = await response.json()
            self.assertEqual(response.status, status, (path, result))
            return result

    async def create(self, text='First', **kwargs):
        return await self.request('POST', '/api/conversations', {'text': text, 'request_id': str(uuid4()), **kwargs}, 201)

    async def send(self, cid, text, key=None):
        return await self.request('POST', f'/api/conversations/{cid}/messages', {'text': text, 'request_id': key or str(uuid4())})

    async def settled(self, tid):
        await wait_for(lambda: self.store.get_turn(tid).state not in ('running', 'queued'))

    async def control(self, cid, action, tid=None, **kwargs):
        return await self.request('POST', f'/api/conversations/{cid}/turns/{action}', {**({'turn_id': tid} if tid else {}), **kwargs})

    async def test_two_quick_sends_are_fifo_and_prompt_excludes_queued(self):
        first = await self.create('First', instructions='Be brief.', reasoning='high')
        cid = first['session_id']
        second = await self.send(cid, 'Second')
        await self.settled(second['turn_id'])
        streamed = [entry[2] for entry in self.fake.requests if entry[2] and entry[2]['stream']]
        self.assertEqual([without_clock(r['messages']) for r in streamed], [
            [{'role': 'user', 'content': 'First'}],
            [{'role': 'user', 'content': 'First'},
             {'role': 'assistant', 'content': 'Hello'}, {'role': 'user', 'content': 'Second'}]])
        self.assertTrue(all(r['chat_template_kwargs']['enable_thinking'] for r in streamed))
        self.assertTrue(all([m['role'] for m in r['messages']].count('system') == 1 and r['messages'][0]['role'] == 'system' for r in streamed))
        self.assertIn('Be brief.', streamed[0]['messages'][0]['content'])
        await wait_for(lambda: self.store.get_conversation(cid).title == 'A short title')
        requests = [entry[2] for entry in self.fake.requests if entry[2]]
        self.assertEqual([p['stream'] for p in requests], [True, True, False])
        self.assertEqual(requests[-1]['max_tokens'], 32)
        self.assertFalse(requests[-1]['chat_template_kwargs']['enable_thinking'])
        view = await self.request('GET', f'/api/conversations/{cid}')
        self.assertEqual([m['role'] for m in view['messages']], ['user', 'assistant'] * 2)
        self.assertEqual(view['context'], {'prompt_tokens': 12, 'context_tokens': 4096})
        self.assertGreater(view['messages'][-1]['roost_usage']['tokens_per_second'], 0)

    async def test_logout_or_expiry_ends_only_that_sessions_stream(self):
        from aiohttp import ClientSession
        for revoke in ('logout', 'expiry'):
            with self.subTest(revoke=revoke), patch('roost.server.HEARTBEAT_SECONDS', .05):
                self.fake.delay = 5
                first = await self.create()
                token, other = self.app['sessions'].issue(), self.app['sessions'].issue()
                url = self.client.make_url(f"/api/conversations/{first['session_id']}/stream")
                async with ClientSession() as client:
                    async with client.get(url, cookies={server.COOKIE: token}) as response:
                        self.assertTrue((await response.content.readuntil(b'\n\n')).startswith(b'data: '))
                        if revoke == 'logout':
                            async with client.post(self.client.make_url('/api/logout'), headers=self.headers,
                                                   cookies={server.COOKIE: token}) as logged_out:
                                self.assertEqual(logged_out.status, 200)
                        else:
                            with self.app['sessions'].db:
                                self.app['sessions'].db.execute('UPDATE sessions SET expires=0 WHERE token_hash=?',
                                                               (self.app['sessions']._hash(token),))
                        async with asyncio.timeout(1):
                            self.assertEqual(await response.content.read(), b'')
                    async with client.get(url, cookies={server.COOKIE: other}) as response:
                        self.assertTrue((await response.content.readuntil(b'\n\n')).startswith(b'data: '))
                self.assertTrue(self.app['sessions'].valid(other))
                self.assertIn(self.store.get_turn(first['turn_id']).state, ('queued', 'running'))

    async def test_short_checkpoint_is_committed_while_provider_is_quiet(self):
        self.fake.chunk_delay = 1
        partial = frame({'content': 'Small partial'})
        self.fake.chunk_sizes = (len(partial),)
        self.fake.frames = partial + frame({}, finish='stop')
        accepted = await self.create()
        await wait_for(lambda: self.store.get_message(accepted['assistant_message_id']).content == 'Small partial', timeout=.8)
        self.assertEqual(self.store.get_turn(accepted['turn_id']).state, 'running')

    async def test_edit_discards_running_and_queued_and_stop_retry_targets_original(self):
        first = await self.create()
        cid = first['session_id']
        await self.settled(first['turn_id'])
        self.fake.delay = .3
        second = await self.send(cid, 'Second')
        third = await self.send(cid, 'Third')
        await wait_for(lambda: self.store.get_turn(second['turn_id']).state == 'running')
        await self.control(cid, 'stop', second['turn_id'])
        await wait_for(lambda: self.store.get_turn(third['turn_id']).state == 'running')
        await self.control(cid, 'stop', second['turn_id'])
        self.assertEqual(self.store.get_turn(third['turn_id']).state, 'running')
        fourth = await self.send(cid, 'Fourth')
        edited = await self.control(cid, 'edit', first['turn_id'], text='Changed', request_id=str(uuid4()))
        self.assertIsNone(self.store.get_turn(second['turn_id']))
        self.assertIsNone(self.store.get_turn(third['turn_id']))
        self.assertIsNone(self.store.get_turn(fourth['turn_id']))
        await self.settled(edited['turn_id'])
        self.assertEqual([t.id for t in self.store.list_turns(cid)], [edited['turn_id']])

    async def test_global_admission_prioritizes_another_chat_over_title(self):
        one = await self.create('One')
        two = await self.create('Two')
        await self.settled(two['turn_id'])
        payloads = [r[2] for r in self.fake.requests if r[2]]
        self.assertTrue(all(p['stream'] for p in payloads[:2]))
        self.assertEqual([p['messages'][-1]['content'] for p in payloads[:2]], ['One', 'Two'])
        self.assertEqual(self.store.get_turn(one['turn_id']).state, 'completed')

    async def test_concurrent_edit_retries_return_one_acceptance(self):
        first = await self.create()
        cid = first['session_id']
        await self.settled(first['turn_id'])
        await wait_for(lambda: cid not in self.app['runner'].active)
        key = str(uuid4())
        async with self.app['runner'].lock:
            retries = [asyncio.create_task(self.control(cid, 'edit', first['turn_id'],
                                                       text='Changed', request_id=key)) for _ in range(2)]
            await asyncio.sleep(.05)
        one, two = await asyncio.gather(*retries)
        self.assertEqual(one, two)
        self.assertEqual([t.id for t in self.store.list_turns(cid)], [one['turn_id']])

    async def test_shutdown_ends_chat_stream_with_queued_work(self):
        self.fake.delay = 2
        first = await self.create()
        cid = first['session_id']
        second = await self.send(cid, 'Second')
        await wait_for(lambda: self.store.get_turn(first['turn_id']).state == 'running')
        response = await self.client.get(f'/api/conversations/{cid}/stream')
        await response.content.readline()
        try:
            async with asyncio.timeout(1):
                await self.app.shutdown()
                await response.read()
            self.assertFalse(self.app['streams'])
            self.assertEqual(self.store.get_turn(first['turn_id']).state, 'interrupted')
            self.assertEqual(self.store.get_turn(second['turn_id']).state, 'queued')
        finally:
            response.close()

    async def test_slow_completion_does_not_hold_queue_and_is_cancelled_on_close(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def completed(cid, tid, text):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        runner = self.app['runner']
        runner.completed = completed
        first = await self.create()
        second = await self.send(first['session_id'], 'Second')
        await asyncio.wait_for(started.wait(), 1)
        await self.settled(second['turn_id'])
        self.assertEqual(self.store.get_turn(first['turn_id']).state, 'completed')
        self.assertTrue(runner.completions)
        await runner.close()
        self.assertTrue(cancelled.is_set())
        self.assertFalse(runner.completions)

    async def test_system_message_is_identical_across_turns_on_the_same_day(self):
        first = await self.create('First')
        cid = first['session_id']
        await self.settled(first['turn_id'])
        await asyncio.sleep(.02)
        second = await self.send(cid, 'Second')
        await self.settled(second['turn_id'])
        one, two = (r['messages'][0] for r in self.generations()[:2])
        self.assertEqual(one, two)
        self.assertNotRegex(one['content'], r'\d{2}:\d{2}')
        self.assertEqual(self.store.get_turn(first['turn_id']).settings['current_date'],
                         self.store.get_turn(second['turn_id']).settings['current_date'])

    async def test_empty_interrupted_assistant_is_omitted_from_prompt(self):
        self.fake.delay = 2
        first = await self.create()
        cid = first['session_id']
        await wait_for(lambda: self.store.get_turn(first['turn_id']).state == 'running')
        await self.control(cid, 'stop', first['turn_id'])
        self.assertEqual(self.store.get_message(first['assistant_message_id']).content, '')
        self.fake.delay = 0
        second = await self.send(cid, 'Second')
        await self.settled(second['turn_id'])
        payloads = [r[2] for r in self.fake.requests if r[2] and r[2]['stream']]
        self.assertEqual(without_clock(payloads[-1]['messages']), [{'role': 'user', 'content': 'First'},
                                                 {'role': 'user', 'content': 'Second'}])

    async def test_idempotent_retry_and_conflict_even_after_settings_change(self):
        key = str(uuid4())
        data = {'text': 'First', 'request_id': key}
        first = await self.request('POST', '/api/conversations', data, 201)
        cid = first['session_id']
        await self.request('PUT', f'/api/conversations/{cid}/settings', {'reasoning': 'high'})
        retry = await self.request('POST', '/api/conversations', data, 201)
        self.assertEqual(first['turn_id'], retry['turn_id'])
        await self.request('POST', '/api/conversations', {**data, 'text': 'Different'}, 409)
        await self.settled(first['turn_id'])
        self.assertEqual(len(self.store.list_conversations()), 1)
        self.assertEqual(len([r for r in self.fake.requests if r[2] and r[2]['stream']]), 1)
        key = str(uuid4())
        accepted = await self.send(cid, 'Second', key)
        await self.request('PUT', f'/api/conversations/{cid}/settings', {'instructions': 'Changed'})
        retry = await self.send(cid, 'Second', key)
        self.assertEqual(accepted['turn_id'], retry['turn_id'])

    async def test_request_ids_pagination_and_deletion_validate_before_mutating(self):
        first = await self.create()
        await self.settled(first['turn_id'])
        cid = first['session_id']
        for offset in ('9' * 5000, '١', '-1', '100001'):
            await self.request('GET', '/api/conversations?offset=' + offset, status=400)
        for query in ('before=0', 'before=-1', 'before=x', 'before=١', 'before=' + '9' * 100,
                      'before=1&limit=0', 'before=1&limit=501', 'before=1&limit=x', 'limit=2',
                      'before=1&before=2', 'before=1&limit=2&limit=3'):
            await self.request('GET', f'/api/conversations/{cid}?' + query, status=400)
        await wait_for(lambda: self.store.get_conversation(cid).title == 'A short title')
        calls = len(self.fake.requests)
        for path in ('/api/conversations', f'/api/conversations/{cid}/messages'):
            for value in ('bad', '0' * 36, None, [], 1):
                await self.request('POST', path, {'text': 'Invalid request', 'request_id': value}, 400)
        self.assertEqual(len(self.fake.requests), calls)
        self.assertEqual(len(self.store.list_turns(cid)), 1)
        job = await self.request('POST', '/api/jobs', {'name': 'Test report', 'prompt': 'Say hello',
                                 'schedule': {'kind': 'every', 'value': 1, 'unit': 'hours'}}, 201)
        for path in (f'/api/conversations/{cid}', '/api/jobs/' + job['id']):
            for value in (1, 'true', False, None):
                await self.request('DELETE', path, {'confirm': value}, 400)
        self.assertIsNotNone(self.store.get_conversation(cid))
        self.assertEqual((await self.request('GET', '/api/jobs'))['jobs'][0]['id'], job['id'])

    async def test_send_now_stop_cancel_edit_regenerate_and_export(self):
        self.fake.chunk_delay = .04
        self.fake.chunk_sizes = (len(frame({'content': 'Partial' * 200})),)
        self.fake.frames = frame({'content': 'Partial' * 200}) + frame({'content': ' answer'}) + frame({}, finish='stop')
        first = await self.create('First')
        cid, tid = first['session_id'], first['turn_id']
        second = await self.send(cid, 'Second')
        third = await self.send(cid, 'Third')
        cancel = await self.send(cid, 'Cancelled')
        await self.control(cid, 'cancel', cancel['turn_id'])
        self.assertIsNone(self.store.get_turn(cancel['turn_id']))
        await wait_for(lambda: self.store.get_message(first['assistant_message_id']).content)
        await self.control(cid, 'send-now', third['turn_id'])
        self.assertEqual(self.store.get_turn(tid).state, 'interrupted')
        self.assertTrue(self.store.get_message(first['assistant_message_id']).content)
        self.assertEqual([t.id for t in self.store.list_turns(cid)], [tid, third['turn_id'], second['turn_id']])
        await wait_for(lambda: self.store.get_turn(third['turn_id']).state == 'running')
        await self.control(cid, 'stop', third['turn_id'])
        self.assertEqual(self.store.get_turn(third['turn_id']).state, 'interrupted')
        await self.settled(second['turn_id'])
        view = await self.request('GET', f'/api/conversations/{cid}')
        self.assertEqual([m['content'] for m in view['messages'] if m['role'] == 'user'], ['First', 'Third', 'Second'])
        cursor = third['message_id']
        older = await self.request('GET', f'/api/conversations/{cid}?before={cursor}&limit=2')
        self.assertEqual([m['turn_id'] for m in older['messages']], [tid, tid])
        latest = self.store.list_turns(cid)[-1]
        await self.control(cid, 'regenerate', latest.id)
        self.assertEqual(self.store.get_message(second['assistant_message_id']).content, '')
        await self.settled(latest.id)
        edited = await self.control(cid, 'edit', tid, text='Edited', request_id=str(uuid4()))
        await self.settled(edited['turn_id'])
        self.assertEqual(len(self.store.list_turns(cid)), 1)
        exported = await self.request('GET', f'/api/conversations/{cid}/export')
        self.assertEqual([m['content'] for m in exported['messages'] if m['role'] == 'user'], ['Edited'])

    async def test_delete_during_generation_and_stream_disconnect_do_not_recreate_rows(self):
        self.fake.chunk_delay = .1
        self.fake.chunk_sizes = (len(frame({'content': 'Partial' * 200})),)
        self.fake.frames = frame({'content': 'Partial' * 200}) + frame({'content': 'Later'}) + frame({}, finish='stop')
        first = await self.create()
        cid = first['session_id']
        await wait_for(lambda: self.store.get_message(first['assistant_message_id']).content)
        response = await self.client.get(f'/api/conversations/{cid}/stream')
        event = json.loads((await response.content.readline()).decode()[6:])
        self.assertEqual(event['text'], self.store.get_message(first['assistant_message_id']).content)
        response.close()
        queued = await self.send(cid, 'Later')
        await self.request('DELETE', f'/api/conversations/{cid}', {'confirm': True})
        await asyncio.sleep(.15)
        self.assertIsNone(self.store.get_conversation(cid))
        self.assertIsNone(self.store.get_turn(queued['turn_id']))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM messages').fetchone()[0], 0)
        self.assertEqual(await self.request('GET', '/api/search?q=Partial'), {'sessions': []})

    async def test_model_validation_neutral_routes_and_owner_guard(self):
        result = await self.request('GET', '/api/models')
        self.assertEqual(result['default_model'], 'example-model')
        first = await self.create()
        cid = first['session_id']
        await self.request('PUT', f'/api/conversations/{cid}/settings', {'model': 'disabled'}, 400)
        await self.request('POST', f'/api/conversations/{cid}/turns/send-now', {'turn_id': first['turn_id']}, 400)
        self.assertEqual(await self.request('GET', '/api/jobs'), {'jobs': []})
        about = await self.request('GET', '/api/about')
        self.assertEqual(set(about), {'roost', 'runtime', 'stats'})
        self.assertNotIn('backend', await self.request('GET', '/api/me'))
        await self.request('GET', '/api/search?q=Hello%00world', status=400)
        async with self.client.post(f'/api/conversations/{cid}/turns/stop', json={}) as response:
            self.assertEqual(response.status, 403)
        self.client.session.cookie_jar.clear()
        await self.request('POST', f'/api/conversations/{cid}/turns/stop', {}, 401)

    async def test_rename_is_final_and_push_uses_presence(self):
        self.app['push'].send = AsyncMock(return_value=1)
        first = await self.create()
        cid = first['session_id']
        await self.request('PATCH', f'/api/conversations/{cid}', {'title': 'Mine'})
        await self.settled(first['turn_id'])
        await wait_for(lambda: cid not in self.app['runner'].active)
        self.assertEqual(self.store.get_conversation(cid).title, 'Mine')
        self.app['push'].send.assert_not_called()
        self.assertFalse(self.store.db.execute('SELECT 1 FROM push_outbox').fetchone())
        self.app['push'].list = lambda: [{'id': 'test-device'}]
        second = await self.send(cid, 'Notify me')
        self.app['presence'].clear()
        await self.settled(second['turn_id'])
        await wait_for(lambda: self.store.db.execute('SELECT 1 FROM push_outbox').fetchone())
        await self.app['outbox'].tick()
        self.assertEqual(self.app['push'].send.call_count, 1)
        message = self.app['push'].send.call_args.args[1]
        self.assertEqual(message['tag'], 'reply-' + second['turn_id'])
        third = await self.send(cid, 'Notify me again')
        self.app['presence'].clear()
        self.app['presence']['test-device'] = (time.monotonic() - server.PRESENCE_SECONDS - 1, cid)
        await self.settled(third['turn_id'])
        await wait_for(lambda: self.store.db.execute('SELECT 1 FROM push_outbox').fetchone())
        await self.app['outbox'].tick()
        self.assertEqual(self.app['push'].send.call_count, 2)
        self.assertEqual(self.app['push'].send.call_args.args[1]['tag'], 'reply-' + third['turn_id'])

    async def web_fixture(self, scenario='web'):
        from web_fake import WebFake
        fake = await WebFake().start()
        self.addAsyncCleanup(fake.close)
        self.enterContext(patch('roost.runner.WebTools', side_effect=fake.tools))
        await self.request('PATCH', '/api/settings', {'search_url': fake.url})
        self.fake.scenario = scenario
        self.fake.delay = 0
        return fake

    def generations(self):
        return [r[2] for r in self.fake.requests if r[2] and r[2]['stream']]

    async def test_web_search_read_answer_fragmented_protocol_and_reload(self):
        await self.web_fixture()
        first = await self.create('Search for an example', reasoning='high')
        tid, cid = first['turn_id'], first['session_id']
        await self.settled(tid)
        self.assertEqual(self.store.get_turn(tid).state, 'completed')
        calls = self.generations()
        self.assertEqual(len(calls), 3)
        assistant = calls[1]['messages'][-2]
        self.assertEqual(assistant['tool_calls'][0]['function'], {'name': 'web_search', 'arguments': '{"query": "example question"}'})
        self.assertEqual(assistant['reasoning_content'], 'I will check a source.')
        self.assertEqual(calls[1]['messages'][-1]['tool_call_id'], assistant['tool_calls'][0]['id'])
        search = json.loads(calls[1]['messages'][-1]['content'])
        page = json.loads(calls[2]['messages'][-1]['content'])
        self.assertEqual([source['n'] for source in search['results']], [1, 1, 2])
        self.assertEqual(page['n'], 1)
        self.assertTrue(search['untrusted_data'] and page['untrusted_data'])
        view = await self.request('GET', f'/api/conversations/{cid}')
        reply = view['messages'][-1]
        self.assertEqual(reply['content'], 'A useful answer [1].')
        self.assertEqual([item['kind'] for item in reply['trace']], ['reasoning', 'tool', 'reasoning', 'tool', 'reasoning'])
        self.assertEqual([item['status'] for item in reply['trace'] if item['kind'] == 'tool'], ['completed', 'completed'])
        self.assertTrue(all(len(item.get('result', '')) <= 16000 for item in reply['trace']))
        reloaded = Store(self.state / 'native.sqlite3')
        try:
            self.assertEqual(reloaded.sources(tid), reply['sources'])
            self.assertEqual(reloaded.trace(tid), reply['trace'])
        finally:
            reloaded.close()
        async with self.client.get(f'/api/conversations/{cid}/stream') as response:
            snapshot = json.loads((await response.content.readline())[6:])
        self.assertTrue(snapshot['reset'])
        self.assertEqual(snapshot['sources'], reply['sources'])
        self.assertEqual(snapshot['trace'], reply['trace'])
        self.fake.scenario = None
        second = await self.send(cid, 'Follow up')
        await self.settled(second['turn_id'])
        history = self.generations()[-1]['messages']
        self.assertTrue(all(m['role'] != 'tool' and 'tool_calls' not in m and 'reasoning_content' not in m for m in history))
        self.assertIn({'role': 'assistant', 'content': 'A useful answer [1].'}, history)

    async def test_unknown_tool_bad_json_and_bad_argument_types_return_errors(self):
        await self.web_fixture('invalid')
        first = await self.create()
        await self.settled(first['turn_id'])
        self.assertEqual(self.store.get_turn(first['turn_id']).state, 'completed')
        results = [s for s in self.store.list_steps(first['turn_id']) if s.kind == 'tool_result']
        self.assertEqual(len(results), 3)
        self.assertTrue(all('error' in json.loads(s.payload['content']) for s in results))
        self.assertTrue(all(s.payload['status'] == 'error' for s in results))

    async def test_three_tool_round_cap_and_stable_sources(self):
        await self.web_fixture('cap')
        first = await self.create()
        await self.settled(first['turn_id'])
        calls = self.generations()
        self.assertEqual(len(calls), 4)
        self.assertTrue(all('tools' in call for call in calls[:3]))
        self.assertEqual([t['function']['name'] for t in calls[3]['tools']], ['remember'])
        self.assertEqual([s['n'] for s in self.store.sources(first['turn_id'])], [1, 2])
        self.assertEqual(len([s for s in self.store.list_steps(first['turn_id']) if s.kind == 'assistant_call']), 3)

    async def test_force_search_rejection_fallback_and_regenerate_snapshot(self):
        await self.web_fixture()
        self.fake.reject_tool_choice = True
        first = await self.create(web='force')
        await self.settled(first['turn_id'])
        calls = self.generations()
        self.assertEqual(calls[0]['tool_choice'], {'type': 'function', 'function': {'name': 'web_search'}})
        self.assertNotIn('tool_choice', calls[1])
        self.assertIn('Search the web first', calls[1]['messages'][-1]['content'])
        searches = [item for item in self.store.trace(first['turn_id']) if item.get('name') == 'web_search']
        self.assertEqual(len(searches), 1)
        self.assertEqual(json.loads(searches[0]['arguments']), {'query': 'example question'})
        self.assertEqual(self.store.get_turn(first['turn_id']).state, 'completed')
        before = len(calls)
        await self.request('PATCH', '/api/settings', {'search_url': ''})
        await self.request('PUT', f"/api/conversations/{first['session_id']}/settings", {'no_web': True})
        await self.control(first['session_id'], 'regenerate', first['turn_id'])
        await self.settled(first['turn_id'])
        self.assertIn('tool_choice', self.generations()[before])
        self.assertTrue(self.store.get_turn(first['turn_id']).settings['tools_offered'])
        self.assertEqual([s['n'] for s in self.store.sources(first['turn_id'])], [1, 2])

    async def test_force_search_rejection_runs_synthetic_search_for_plain_answer(self):
        web = await self.web_fixture(None)
        self.fake.reject_tool_choice = True
        first = await self.create('  ' + 'query ' * 500 + '  ', web='force')
        tid = first['turn_id']
        await self.settled(tid)
        self.assertEqual(self.store.get_turn(tid).state, 'completed')
        self.assertEqual([path for path, _ in web.requests], ['/search'])
        calls = self.generations()
        self.assertEqual(len(calls), 3)
        assistant, result = calls[2]['messages'][-2:]
        call = assistant['tool_calls'][0]
        self.assertEqual(call['function']['name'], 'web_search')
        self.assertEqual(json.loads(call['function']['arguments']), {'query': ('query ' * 500).strip()[:2000]})
        self.assertEqual(result['tool_call_id'], call['id'])
        self.assertTrue(json.loads(result['content'])['results'])
        self.assertEqual([s.kind for s in self.store.list_steps(tid) if s.kind != 'reasoning'],
                         ['assistant_call', 'tool_result'])
        tools = [item for item in self.store.trace(tid) if item['kind'] == 'tool']
        self.assertEqual([(item['name'], item['status']) for item in tools], [('web_search', 'completed')])
        self.assertEqual(self.store.messages_for_turn(tid)[-1].content, 'Hello')

    async def test_equal_length_tool_prose_replacement_saves_reasoning_trace(self):
        await self.web_fixture()
        frames = [frame({'content': 'Checking..'}) + frame({'tool_calls': [{'index': 0, 'id': 'check', 'type': 'function',
                  'function': {'name': 'web_search', 'arguments': '{"query":"example"}'}}]}, finish='tool_calls'),
                  frame({'reasoning_content': 'Think'}) + frame({'content': 'Done.'}) + frame({}, finish='stop')]
        with patch.object(self.fake, 'tool_frames', side_effect=frames):
            first = await self.create()
            await self.settled(first['turn_id'])
        tid = first['turn_id']
        self.assertEqual(self.store.get_turn(tid).state, 'completed')
        reply = self.store.messages_for_turn(tid)[-1]
        self.assertEqual((reply.content, reply.reasoning), ('Done.', 'Think'))
        self.assertEqual(self.store.trace(tid)[-1], {'kind': 'reasoning', 'text': 'Think'})

    async def test_force_search_and_disabled_availability_settings(self):
        await self.web_fixture()
        models = await self.request('GET', '/api/models')
        self.assertTrue(models['web_available'] and models['models'][0]['tools'])
        test = await self.request('POST', '/api/settings/web-search/test', {'query': 'example'})
        self.assertEqual(len(test['results']), 3)
        first = await self.create(web='force')
        await self.settled(first['turn_id'])
        self.assertIn('tool_choice', self.generations()[0])
        cid = first['session_id']
        await self.request('PUT', f'/api/conversations/{cid}/settings', {'no_web': 'yes'}, 400)
        saved = await self.request('PUT', f'/api/conversations/{cid}/settings', {'no_web': True})
        self.assertTrue(saved['no_web'])
        second = await self.send(cid, 'No web here')
        await self.settled(second['turn_id'])
        self.assertEqual([t['function']['name'] for t in self.generations()[-1]['tools']], ['remember'])
        await self.request('POST', f'/api/conversations/{cid}/messages', {'text': 'Forced', 'web': 'force'}, 400)
        self.assertFalse(self.store.get_turn(second['turn_id']).settings['tools_offered'])
        servers = self.app['servers']
        primary = servers.get('primary')
        await self.request('PUT', '/api/settings/servers/primary/models', {'models': [{**primary['models'][0], 'tools': False}], 'default_model': primary['default_model']})
        third = await self.create()
        await self.settled(third['turn_id'])
        self.assertNotIn('tools', self.generations()[-1])
        self.assertFalse(self.store.get_turn(third['turn_id']).settings['tools_offered'])
        await self.request('POST', '/api/conversations', {'text': 'Forced', 'web': 'force'}, 400)
        await self.request('PATCH', '/api/settings', {'search_url': ''})
        self.assertFalse((await self.request('GET', '/api/models'))['web_available'])
        await self.request('POST', '/api/settings/web-search/test', {'query': 'example'}, 400)

    async def test_stop_send_now_delete_during_fetch_and_live_trace(self):
        fake = await self.web_fixture()
        fake.delay = .5
        first = await self.create()
        cid, tid = first['session_id'], first['turn_id']
        await asyncio.wait_for(fake.started.wait(), 2)
        async with self.client.get(f'/api/conversations/{cid}/stream') as response:
            snapshot = json.loads((await response.content.readline())[6:])
        self.assertEqual(snapshot['trace'][-1]['status'], 'running')
        async with asyncio.timeout(.25):
            await self.control(cid, 'stop', tid)
        await wait_for(lambda: fake.transports[0].is_closing(), timeout=.25)
        self.assertEqual(self.store.trace(tid)[-1]['status'], 'interrupted')
        self.assertEqual(self.store.get_turn(tid).state, 'interrupted')
        self.assertFalse(self.store.sources(tid))
        fake.started.clear()
        second = await self.send(cid, 'Search again')
        await asyncio.wait_for(fake.started.wait(), 2)
        third = await self.send(cid, 'Run next')
        async with asyncio.timeout(.25):
            await self.control(cid, 'send-now', third['turn_id'])
        self.assertEqual(self.store.get_turn(second['turn_id']).state, 'interrupted')
        await wait_for(lambda: self.store.get_turn(third['turn_id']).state == 'running')
        async with asyncio.timeout(.25):
            await self.request('DELETE', f'/api/conversations/{cid}', {'confirm': True})
        await asyncio.sleep(.05)
        self.assertIsNone(self.store.get_turn(third['turn_id']))
        self.assertIsNone(self.store.get_conversation(cid))

    async def test_total_tool_time_and_current_owner_time_zone(self):
        fake = await self.web_fixture('cap')
        fake.delay = .2
        await self.request('PATCH', '/api/settings', {'time_zone': 'Etc/GMT+5'})
        with patch('roost.runner.TOTAL_SECONDS', .02):
            first = await self.create()
            await self.settled(first['turn_id'])
        self.assertEqual(self.store.get_turn(first['turn_id']).state, 'completed')
        self.assertEqual(len(self.generations()), 2)
        self.assertEqual([t['function']['name'] for t in self.generations()[-1]['tools']], ['remember'])
        self.assertIn('Total tool time', self.store.trace(first['turn_id'])[1]['result'])
        self.assertTrue(self.generations()[0]['messages'][0]['content'].endswith('(Etc/GMT+5)'))
        self.assertIn("\n\nToday's date: ", self.generations()[0]['messages'][0]['content'])


    async def test_persona_and_memory_prompt_order_queued_edits_and_deleted_snapshot(self):
        persona = await self.request('POST', '/api/personas', {'name': 'Guide', 'prompt': 'You are a patient guide.'}, 201)
        memory = await self.request('POST', '/api/memory', {'text': 'The user prefers examples.'}, 201)
        fid = memory['fact']['id']
        self.fake.delay = .3
        first = await self.create('First', instructions='Keep it concrete.')
        cid = first['session_id']
        await wait_for(lambda: self.store.get_turn(first['turn_id']).state == 'running')
        queued = await self.send(cid, 'Second')
        await self.request('PUT', f'/api/conversations/{cid}/settings', {'persona_id': persona['id']})
        await self.request('PUT', f'/api/memory/{fid}', {'text': 'The user prefers diagrams.'})
        await self.settled(queued['turn_id'])
        first_turn, second_turn = self.store.get_turn(first['turn_id']), self.store.get_turn(queued['turn_id'])
        self.assertEqual(first_turn.persona['name'], 'Assistant')
        self.assertEqual(second_turn.persona['name'], 'Guide')
        self.assertEqual(first_turn.memories, [{'id': fid, 'text': 'The user prefers examples.'}])
        self.assertEqual(second_turn.memories, [{'id': fid, 'text': 'The user prefers diagrams.'}])
        prompt = self.generations()[1]['messages']
        self.assertEqual([m['role'] for m in prompt].count('system'), 1)
        parts = prompt[0]['content'].split('\n\n')
        self.assertEqual(parts[0], 'You are a patient guide.')
        self.assertEqual(parts[1], f'Memory facts:\n[{fid}] The user prefers diagrams.')
        self.assertEqual(parts[2], REMEMBER_GUIDANCE)
        self.assertEqual(parts[3], 'Keep it concrete.')
        self.assertTrue(parts[4].startswith('Cite web sources'))
        self.assertTrue(parts[5].startswith("Today's date: "))
        self.assertEqual(len(parts), 6)
        await self.request('DELETE', f'/api/memory/{fid}')
        view = await self.request('GET', f'/api/conversations/{cid}')
        reply = view['messages'][-1]
        self.assertEqual(view['persona_name'], 'Guide')
        self.assertEqual(reply['persona_name'], 'Guide')
        self.assertEqual(reply['memories'], second_turn.memories)
        async with self.client.get(f'/api/conversations/{cid}/stream') as response:
            event = json.loads((await response.content.readline())[6:])
        self.assertEqual(event['memories'], second_turn.memories)
        self.assertEqual(event['persona_name'], 'Guide')
        await self.request('DELETE', f"/api/personas/{persona['id']}")
        self.assertIsNone(self.store.get_conversation(cid).persona_id)
        self.assertEqual((await self.request('GET', f'/api/conversations/{cid}'))['persona_name'], 'Assistant')

    async def test_memory_empty_prompt_and_tools_false_still_injects_facts(self):
        first = await self.create()
        await self.settled(first['turn_id'])
        prompt = self.generations()[0]['messages']
        self.assertEqual(prompt[0]['content'].index(REMEMBER_GUIDANCE), len(DEFAULT_PERSONA_PROMPT) + 2)
        self.assertTrue(prompt[0]['content'].startswith(DEFAULT_PERSONA_PROMPT + '\n\n' + REMEMBER_GUIDANCE + '\n\nCite web sources'))
        self.assertNotIn('Memory facts:', prompt[0]['content'])
        primary = self.app['servers'].get('primary')
        self.app['servers'].set_models('primary', {'models': [{**primary['models'][0], 'tools': False}], 'default_model': primary['default_model']})
        fact = self.store.save_memory('A durable preference.')
        second = await self.create()
        await self.settled(second['turn_id'])
        payload = self.generations()[-1]
        self.assertNotIn('tools', payload)
        self.assertTrue(payload['messages'][0]['content'].startswith(
            f"{DEFAULT_PERSONA_PROMPT}\n\nMemory facts:\n[{fact['id']}] A durable preference.\n\nCite web sources"))
        self.assertNotIn(REMEMBER_GUIDANCE, str(payload))

    async def test_empty_persona_prompt_adds_no_system_message(self):
        first = await self.create()
        await self.settled(first['turn_id'])
        turn = self.store.get_turn(first['turn_id'])
        runner = self.app['runner']
        self.assertTrue(runner.prompt(turn)[0]['content'].startswith(DEFAULT_PERSONA_PROMPT))
        blank = SimpleNamespace(**{**vars(turn), 'persona': {'name': 'Blank', 'prompt': ''}})
        system = runner.prompt(blank)[0]['content']
        self.assertTrue(system.startswith(REMEMBER_GUIDANCE))
        self.assertFalse(system.startswith('\n'))

    async def test_regenerate_clears_persona_and_memory_snapshot_while_queued(self):
        self.store.save_memory('A durable preference.')
        first = await self.create()
        await self.settled(first['turn_id'])
        self.assertTrue(self.store.get_turn(first['turn_id']).memories)
        self.store.regenerate(first['turn_id'])
        queued = self.store.get_turn(first['turn_id'])
        self.assertEqual((queued.persona, queued.memories), ({}, []))

    async def test_remember_save_duplicate_no_web_and_forget_trace(self):
        first = await self.create()
        await self.settled(first['turn_id'])
        cid = first['session_id']
        await self.request('PUT', f'/api/conversations/{cid}/settings', {'no_web': True})
        self.fake.scenario = 'remember'
        second = await self.send(cid, 'Keep my preference')
        await self.settled(second['turn_id'])
        turn = self.store.get_turn(second['turn_id'])
        self.assertEqual(turn.state, 'completed')
        fact = self.store.memories()[0]
        self.assertEqual(fact['origin'], 'model')
        self.assertEqual(fact['source_message_id'], second['assistant_message_id'])
        self.assertEqual(fact['conversation_id'], cid)
        self.assertEqual([t['function']['name'] for t in self.generations()[-1]['tools']], ['remember'])
        third = await self.send(cid, 'Again')
        await self.settled(third['turn_id'])
        self.assertEqual(len(self.store.memories()), 1)
        self.assertIn('Duplicate', self.store.trace(third['turn_id'])[1]['result'])
        edited = await self.request('PUT', f"/api/memory/{fact['id']}", {'text': 'An edited writing preference.'})
        self.assertEqual(edited['facts'][0]['origin'], 'model')
        self.assertEqual(edited['facts'][0]['source_message_id'], second['assistant_message_id'])
        self.assertEqual(self.store.trace(second['turn_id'])[1]['fact'], fact['text'])
        await self.request('DELETE', f"/api/memory/{fact['id']}")
        self.assertTrue(self.store.trace(second['turn_id'])[1]['forgotten'])
        self.assertEqual(self.store.get_turn(third['turn_id']).memories[0]['text'], fact['text'])

    async def test_remember_cap_three_and_web_rounds_are_independent(self):
        await self.web_fixture('scripted')
        scripted = [tool_frames([('remember', json.dumps({'fact': f'Durable fact {n}.'}))], prefix=str(n)) for n in range(3)]
        scripted += [tool_frames([('web_search', '{"query":"example"}')], prefix='web' + str(n)) for n in range(3)]
        scripted += [frame({'content': 'Finished.'}) + frame({}, finish='stop')]
        with patch.object(self.fake, 'tool_frames', side_effect=scripted):
            first = await self.create()
            await self.settled(first['turn_id'])
        self.assertEqual(self.store.get_turn(first['turn_id']).state, 'completed')
        self.assertEqual(len(self.store.memories()), 3)
        calls = self.generations()
        self.assertEqual(len(calls), 7)
        self.assertEqual([t['function']['name'] for t in calls[3]['tools']], ['web_search', 'read_page'])
        self.assertNotIn('tools', calls[-1])
        self.assertEqual(len([t for t in self.store.trace(first['turn_id']) if t.get('name') == 'web_search']), 3)
        # A fourth call in the same batch is refused even if the provider ignores the advertised limit.
        scripted = [tool_frames([('remember', json.dumps({'fact': f'Batch fact {n}.'})) for n in range(4)]),
                    frame({'content': 'Done'}) + frame({}, finish='stop')]
        with patch.object(self.fake, 'tool_frames', side_effect=scripted):
            second = await self.create()
            await self.settled(second['turn_id'])
        trace = [t for t in self.store.trace(second['turn_id']) if t.get('name') == 'remember']
        self.assertEqual(len(trace), 4)
        self.assertIn('limit reached', trace[-1]['result'])
        self.assertEqual(len(self.store.memories()), 6)

    async def test_remember_cannot_satisfy_forced_search(self):
        fake = await self.web_fixture('scripted')
        scripted = [tool_frames([('remember', '{"fact":"The user likes examples."}')]),
                    frame({'content': 'Searched and saved.'}) + frame({}, finish='stop')]
        with patch.object(self.fake, 'tool_frames', side_effect=scripted):
            first = await self.create('Look up an example', web='force')
            await self.settled(first['turn_id'])
        self.assertEqual(self.store.get_turn(first['turn_id']).state, 'completed')
        self.assertEqual([path for path, _ in fake.requests], ['/search'])
        names = [t['name'] for t in self.store.trace(first['turn_id']) if t['kind'] == 'tool']
        self.assertEqual(names, ['web_search', 'remember'])
        self.assertEqual(len(self.store.memories()), 1)

    async def test_default_persona_edit_and_new_chat_selection(self):
        persona = await self.request('POST', '/api/personas', {'name': 'Guide', 'prompt': 'You guide.'}, 201)
        await self.request('PUT', '/api/personas/default', {'default_persona_id': persona['id']})
        await self.request('PUT', f"/api/personas/{persona['id']}", {'name': 'Builder', 'prompt': 'You build carefully.'})
        first = await self.create()
        await self.settled(first['turn_id'])
        self.assertIsNone(self.store.get_conversation(first['session_id']).persona_id)
        turn = self.store.get_turn(first['turn_id'])
        self.assertEqual(turn.persona['name'], 'Builder')
        self.assertTrue(self.generations()[0]['messages'][0]['content'].startswith('You build carefully.\n\n'))
        self.assertEqual((await self.request('GET', '/api/settings'))['agent'], 'Builder')
        await self.request('DELETE', f"/api/personas/{persona['id']}", status=400)

    async def test_runner_returns_memory_full_while_manual_adds_remain_allowed(self):
        for n in range(16):
            await self.request('POST', '/api/memory', {'text': str(n).zfill(3) + 'x' * 497}, 201)
        result = await self.request('POST', '/api/memory', {'text': 'One extra manual fact.'}, 201)
        self.assertGreater(result['used_tokens'], result['budget_tokens'])
        self.fake.scenario = 'remember'
        first = await self.create()
        await self.settled(first['turn_id'])
        self.assertEqual(self.store.get_turn(first['turn_id']).state, 'completed')
        self.assertEqual(len(self.store.memories()), 17)
        trace = next(item for item in self.store.trace(first['turn_id']) if item.get('name') == 'remember')
        self.assertEqual(trace['status'], 'error')
        self.assertIn('Memory full.', trace['result'])
        self.assertNotIn('fact_id', trace)

    async def test_persona_memory_validation_origin_auth(self):
        personas = await self.request('GET', '/api/personas')
        pid = personas['default_persona_id']
        await self.request('DELETE', f'/api/personas/{pid}', status=400)
        for data in ({'name': '', 'prompt': ''}, {'name': 'x' * 61, 'prompt': ''}, {'name': 'Name', 'prompt': 'x' * 8001},
                     {'name': [], 'prompt': ''}, {'name': 'Name', 'prompt': None}, {'name': 'Name', 'prompt': '', 'extra': True}):
            await self.request('POST', '/api/personas', data, 400)
        for data in ({'text': ''}, {'text': 'x' * 501}, {'text': []}, {'text': 'Fact', 'origin': 'model'},
                     {'text': 'Fact', 'source_message_id': True}, {'text': 'Fact', 'conversation_id': []},
                     {'text': 'Fact', 'source_message_id': 999999}):
            await self.request('POST', '/api/memory', data, 400)
        await self.request('PUT', '/api/personas/default', {'default_persona_id': []}, 400)
        first, second = await self.create(), await self.create()
        await self.settled(second['turn_id'])
        await self.request('PUT', f"/api/conversations/{first['session_id']}/settings", {'persona_id': []}, 400)
        await self.request('PUT', f"/api/conversations/{first['session_id']}/settings", {'persona_id': 'missing'}, 400)
        await self.request('POST', '/api/memory', {'text': 'Fact', 'source_message_id': first['message_id'], 'conversation_id': second['session_id']}, 400)
        memory = await self.request('POST', '/api/memory', {'text': '  A preference.  ', 'source_message_id': first['message_id']}, 201)
        fid = memory['fact']['id']
        self.assertEqual(memory['fact']['origin'], 'user')
        await self.request('POST', '/api/memory', {'text': 'a PREFERENCE.'}, 400)
        await self.request('PUT', f'/api/memory/{fid}', {'text': ''}, 400)
        await self.request('DELETE', '/api/memory/nope', status=400)
        for path, payload in (('/api/memory', {'text': 'Fact'}), ('/api/personas', {'name': 'Name', 'prompt': ''})):
            async with self.client.post(path, json=payload) as response:
                self.assertEqual(response.status, 403)
        self.assertEqual(len(self.store.memories()), 1)
        self.client.session.cookie_jar.clear()
        for path in ('/api/memory', '/api/personas'):
            await self.request('GET', path, status=401)
            await self.request('POST', path, {}, 401)


class RecoveryAndSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_tokens_per_second_excludes_prefill_and_requires_token_span(self):
        for first, ended, expected in ((12, 15, 2), (None, 15, None), (12, 12, None)):
            with self.subTest(first=first, ended=ended), tempfile.TemporaryDirectory() as directory:
                store = Store(Path(directory) / 'native.sqlite3')
                try:
                    c = store.create_conversation()
                    accepted = store.accept_turn(c.id, 'first', 'First', model='example-model')
                    store.set_turn_state(accepted.turn_id, 'running')
                    runner = Runner(store, None, None)
                    async def stream(*args, **kwargs):
                        if first is not None:
                            yield Event('text', text='Hello')
                        yield Event('usage', usage={'completion_tokens': 6})
                    clock = [10, first, first, first, ended] if first is not None else [10, ended]
                    with patch.object(runner, 'client', return_value=SimpleNamespace(stream=stream)), \
                            patch('roost.runner.time', SimpleNamespace(monotonic=iter(clock).__next__)):
                        await runner.generate(accepted.turn_id)
                    turn = store.get_turn(accepted.turn_id)
                    self.assertEqual(turn.state, 'completed')
                    self.assertEqual(turn.stats['model_seconds'], ended - 10)
                    self.assertEqual(turn.stats['time_to_first_token'], first - 10 if first is not None else None)
                    self.assertEqual(turn.stats['tokens_per_second'], expected)
                finally:
                    store.close()

    async def test_restart_keeps_partial_and_resumes_only_queued(self):
        from roost.runner import Runner
        import aiohttp
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'native.sqlite3'
            store = Store(path)
            c = store.create_conversation()
            first = store.accept_turn(c.id, 'first', 'First', model='example-model')
            second = store.accept_turn(c.id, 'second', 'Second', model='example-model')
            store.set_turn_state(first.turn_id, 'running')
            store.checkpoint_assistant(first.turn_id, 'Partial')
            store.close()
            fake = await OpenAIFake().start()
            models = Servers(Path(directory), environ={})
            models.update('primary', {'base_url': fake.url})
            store = Store(path)
            async with aiohttp.ClientSession() as session:
                runner = Runner(store, models, session)
                runner.start()
                try:
                    await wait_for(lambda: store.get_turn(second.turn_id).state == 'completed')
                    self.assertEqual(store.get_turn(first.turn_id).state, 'interrupted')
                    self.assertEqual(store.get_message(first.assistant_message_id).content, 'Partial')
                    payload = fake.requests[0][2]
                    self.assertEqual(without_clock(payload['messages']), [{'role': 'user', 'content': 'First'},
                                     {'role': 'assistant', 'content': 'Partial'}, {'role': 'user', 'content': 'Second'}])
                    self.assertEqual(len([r for r in fake.requests if r[2] and r[2]['stream']]), 1)
                finally:
                    await runner.close()
                    store.close()
                    await fake.close()

    async def test_migration_backfills_fts_and_escapes_queries_and_reverses(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'native.sqlite3')
            store.migrate(1)
            c = store.create_conversation()
            a = store.accept_turn(c.id, 'first', 'Hello world', model='example-model')
            store.migrate()
            self.assertEqual(store.search('Hello')[0]['conversation_id'], c.id)
            for query in ('\0', 'Hello\0world'):
                with self.assertRaises(StoreError):
                    store.search(query)
            for query in ('"', 'OR', '*', 'NEAR(Hello)', 'content:Hello', 'Hello OR missing', '" OR 1=1 --'):
                self.assertEqual(store.search(query), [], query)
            store.checkpoint_assistant(a.turn_id, 'Updated')
            self.assertTrue(store.search('Updated'))
            store.checkpoint_assistant(a.turn_id, 'Changed')
            self.assertFalse(store.search('Updated'))
            store.migrate(1)
            self.assertEqual(store.get_message(a.assistant_message_id).content, 'Changed')
            store.migrate()
            self.assertTrue(store.search('Changed'))
            store.delete_conversation(c.id)
            self.assertFalse(store.search('Changed'))
            store.close()

    async def test_downgrade_preserves_populated_stats_and_search_on_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'native.sqlite3')
            try:
                c = store.create_conversation()
                accepted = store.accept_turn(c.id, 'first', 'Hello', model='example-model')
                store.set_turn_state(accepted.turn_id, 'running')
                store.finish(accepted.turn_id, {'usage': {'prompt_tokens': 3}})
                with self.assertRaises(StoreError):
                    store.migrate(1)
                self.assertEqual(store.get_turn(accepted.turn_id).stats['usage']['prompt_tokens'], 3)
                self.assertEqual(store.search('Hello')[0]['conversation_id'], c.id)
            finally:
                store.close()

    async def test_sources_migration_refuses_data_loss_and_cascades(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'native.sqlite3')
            try:
                c = store.create_conversation()
                accepted = store.accept_turn(c.id, 'source-test', 'Example', model='example-model')
                tid = accepted.turn_id
                self.assertEqual(store.add_source(tid, 'Example', 'https://example.org/page'), 1)
                self.assertEqual(store.add_source(tid, 'Duplicate', 'https://example.org/page'), 1)
                with self.assertRaises(StoreError):
                    store.migrate(2)
                self.assertEqual(store.sources(tid)[0]['n'], 1)
                store.delete_conversation(c.id)
                self.assertEqual(store.db.execute('SELECT count(*) FROM sources').fetchone()[0], 0)
                store.migrate(2)
                store.migrate()
                self.assertEqual(store.sources(tid), [])
            finally:
                store.close()

    async def test_context_tokens_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            servers = Servers(Path(directory), environ={})
            servers.update('primary', {'base_url': 'https://models.example.invalid/v1'})
            model = {'id': 'example-model', 'display_name': 'Example', 'enabled': True, 'thinking_mode': 'unsupported'}
            for value in (None, True, 0, -1, '4096', 1.5, 100000001):
                with self.subTest(value=value), self.assertRaises(ServerError):
                    servers.set_models('primary', {'models': [{**model, 'context_tokens': value}], 'default_model': model['id']})


class CompactionTests(unittest.IsolatedAsyncioTestCase):
    """Long-chat compaction through the real runner, store and loopback provider."""

    async def asyncSetUp(self):
        await ChatTests.asyncSetUp(self)
        self.fake.delay = 0
        self.fake.completion['choices'][0]['message']['content'] = 'Synthetic running summary.'
        self.set_context(400)

    asyncTearDown = ChatTests.asyncTearDown
    web_fixture = ChatTests.web_fixture
    request, create, send, settled, generations, control = (ChatTests.request, ChatTests.create, ChatTests.send,
                                                             ChatTests.settled, ChatTests.generations, ChatTests.control)

    def set_context(self, tokens):
        primary = self.app['servers'].get('primary')
        model = {key: value for key, value in primary['models'][0].items() if key != 'context_tokens'}
        if tokens:
            model['context_tokens'] = tokens
        self.app['servers'].set_models('primary', {'models': [model], 'default_model': 'example-model'})

    def seed(self, count, *, prompt_tokens=300, width=90):
        """Settled earlier turns, written straight to the store so nothing is queued or generated."""
        cid = self.store.create_conversation(model='example-model').id
        self.extend(cid, count, prompt_tokens=prompt_tokens, width=width)
        return cid

    def extend(self, cid, count, *, prompt_tokens=300, width=90, label='q'):
        start = len(self.store.list_turns(cid))
        for number in range(start + 1, start + count + 1):
            accepted = self.store.accept_turn(cid, f'seed-{cid}-{number}', f'{label}{number} ' + 'u' * width, model='example-model')
            self.store.set_turn_state(accepted.turn_id, 'running')
            self.store.checkpoint_assistant(accepted.turn_id, f'a{number} ' + 'r' * width)
            self.store.finish(accepted.turn_id, {'usage': {'prompt_tokens': prompt_tokens}})

    def summary_requests(self):
        return [r[2] for r in self.fake.requests if r[2] and not r[2]['stream'] and r[2].get('max_tokens') == 2000]

    async def run_turn(self, cid, text='Next'):
        sent = await self.send(cid, text)
        await self.settled(sent['turn_id'])
        return sent['turn_id']

    async def test_threshold_compacts_and_lays_out_the_prompt_with_the_summary(self):
        cid = self.seed(6)
        tid = await self.run_turn(cid)
        turn = self.store.get_turn(tid)
        self.assertEqual(turn.state, 'completed')
        summaries = self.store.summaries(cid)
        self.assertEqual([(s['through_ordinal'], s['text']) for s in summaries], [(4, 'Synthetic running summary.')])
        self.assertEqual(turn.summary_id, summaries[0]['id'])
        request = self.summary_requests()[0]
        self.assertEqual(request['max_tokens'], 2000)
        self.assertFalse(request['chat_template_kwargs']['enable_thinking'])
        self.assertNotIn('tools', request)
        self.assertNotIn('temperature', request)
        self.assertIn('q1 ', request['messages'][1]['content'])
        self.assertNotIn('q5 ', request['messages'][1]['content'])
        messages = self.generations()[0]['messages']
        self.assertEqual([m['role'] for m in messages].count('system'), 1)
        self.assertTrue(messages[0]['content'].startswith(DEFAULT_PERSONA_PROMPT))
        self.assertTrue(messages[0]['content'].endswith('\n\nSummary of the earlier conversation:\nSynthetic running summary.'))
        self.assertLess(messages[0]['content'].index("Today's date: "), messages[0]['content'].index('Summary of the earlier'))
        tail = [(m['role'], m['content'][:2]) for m in messages[1:]]
        self.assertEqual(tail, [('user', 'q5'), ('assistant', 'a5'), ('user', 'q6'), ('assistant', 'a6'), ('user', 'Ne')])
        trace = (await self.request('GET', f'/api/conversations/{cid}'))['messages'][-1]['trace']
        self.assertEqual(trace[0], {'kind': 'tool', 'id': 'compaction-1', 'name': 'summarize', 'arguments': '{}',
                                    'status': 'completed', 'result': 'Summarized 4 earlier turns.'})
        view = await self.request('GET', f'/api/conversations/{cid}')
        self.assertEqual(view['summaries'], summaries)
        self.assertEqual(set(summaries[0]), {'id', 'through_ordinal', 'text', 'created_at'})
        self.assertEqual(len(view['messages']), 14)
        self.assertEqual(self.store.search('q1')[0]['conversation_id'], cid)
        async with self.client.get(f'/api/conversations/{cid}/stream') as response:
            event = json.loads((await response.content.readline())[6:])
        self.assertEqual(event['summaries'], summaries)
        self.assertEqual(event['trace'][0]['name'], 'summarize')

    async def test_trigger_at_and_below_threshold_and_unknown_context(self):
        # Threshold is 300 tokens of 400; the new message adds 1 (4 characters / 4).
        below = self.seed(4, prompt_tokens=298)
        await self.run_turn(below)
        self.assertEqual(self.summary_requests(), [])
        self.assertEqual(self.store.summaries(below), [])
        at = self.seed(4, prompt_tokens=299)
        await self.run_turn(at)
        self.assertEqual(len(self.summary_requests()), 1)
        self.assertEqual(len(self.store.summaries(at)), 1)
        self.set_context(None)
        unknown = self.seed(4, prompt_tokens=10 ** 6)
        await self.run_turn(unknown)
        self.assertEqual(len(self.summary_requests()), 1)
        self.assertEqual(self.store.summaries(unknown), [])
        # A last completed turn without a reported prompt size never triggers.
        self.set_context(400)
        silent = self.store.create_conversation().id
        accepted = self.store.accept_turn(silent, 'silent', 'q ' + 'u' * 4000, model='example-model')
        self.store.set_turn_state(accepted.turn_id, 'running')
        self.store.finish(accepted.turn_id, {})
        await self.run_turn(silent)
        self.assertEqual(len(self.summary_requests()), 1)

    async def test_kept_turns_never_fewer_than_two_and_size_bounds_the_rest(self):
        from roost import compaction
        self.assertEqual(compaction.split([100] * 6, 400), (4, 2))
        self.assertEqual(compaction.split([10] * 6, 400), (0, 6))
        self.assertEqual(compaction.split([10] * 11, 400), (1, 10))
        self.assertEqual(compaction.split([500] * 3, 400), (1, 2))
        self.assertEqual(compaction.split([1], 400), (0, 1))
        self.assertEqual(compaction.split([100] * 6, 400, overflow=True), (5, 1))
        # With only two settled turns there is nothing to fold, so a due trigger changes nothing.
        cid = self.seed(2)
        await self.run_turn(cid)
        self.assertEqual((self.summary_requests(), self.store.summaries(cid)), ([], []))

    async def test_second_summary_extends_the_first_without_the_old_history(self):
        cid = self.seed(6)
        await self.run_turn(cid)
        self.fake.completion['choices'][0]['message']['content'] = 'Second synthetic summary.'
        self.extend(cid, 4, label='n')
        await self.run_turn(cid, 'Again')
        first, second = self.summary_requests()
        self.assertIn('Synthetic running summary.', second['messages'][1]['content'])
        self.assertIn('Previous summary:', second['messages'][1]['content'])
        for old in ('q1 ', 'q4 ', 'a3 '):
            self.assertNotIn(old, second['messages'][1]['content'])
        self.assertIn('Next', second['messages'][1]['content'])
        self.assertEqual([s['through_ordinal'] for s in self.store.summaries(cid)], [4, 9])
        self.assertEqual(self.store.get_turn(self.store.list_turns(cid)[-1].id).summary_id, self.store.summaries(cid)[-1]['id'])
        final = self.generations()[-1]['messages']
        self.assertEqual(final[0]['content'].count('Summary of the earlier'), 1)
        self.assertNotIn('Synthetic running summary.', str(final))

    async def test_overflow_retry_folds_in_chunks_that_fit_and_halves_when_a_chunk_overflows(self):
        self.set_context(None)
        cid = self.seed(8, prompt_tokens=10)
        # The fake rejects any request over the cap, summary calls included, like a real server.
        self.fake.max_chars = 1500
        tid = await self.run_turn(cid)
        self.assertEqual(self.store.get_turn(tid).state, 'completed')
        self.assertEqual(len(self.generations()), 2)
        retry = self.generations()[1]['messages']
        self.assertIn('Summary of the earlier', retry[0]['content'])
        self.assertEqual(sum(m['role'] == 'user' for m in retry), 2)
        through = [s['through_ordinal'] for s in self.store.summaries(cid)]
        self.assertEqual(through[-1], 7)
        self.assertEqual(len(through), 1)
        accepted = [r for r in self.summary_requests() if sum(len(m['content']) for m in r['messages']) <= 1500]
        self.assertGreater(len(accepted), 1)
        self.assertGreater(len(self.summary_requests()), len(accepted))
        self.assertLessEqual(len(self.summary_requests()), 8)
        self.assertIn('Previous summary:\nSynthetic running summary.', accepted[-1]['messages'][1]['content'])
        trace = self.store.trace(tid)
        self.assertEqual([(t['name'], t['status']) for t in trace if t.get('name') == 'summarize'], [('summarize', 'completed')])
        self.assertEqual(trace[0]['result'], 'Summarized 7 earlier turns.')

    async def test_overflow_that_persists_or_cannot_be_summarized_fails_clearly(self):
        from roost.compaction import TOO_LONG
        self.set_context(None)
        cid = self.seed(4, prompt_tokens=10)
        self.fake.max_chars = 10
        tid = await self.run_turn(cid)
        turn = self.store.get_turn(tid)
        self.assertEqual((turn.state, turn.error), ('failed', TOO_LONG))
        self.assertEqual(len(self.generations()), 1)
        self.assertEqual(self.store.summaries(cid), [])
        self.fake.max_chars = None
        broken = self.seed(4, prompt_tokens=10)
        self.fake.max_chars, self.fake.fail_summary = 10, True
        turn = self.store.get_turn(await self.run_turn(broken))
        self.assertEqual((turn.state, turn.error), ('failed', TOO_LONG))
        self.assertEqual(self.store.summaries(broken), [])
        self.assertEqual(self.store.trace(turn.id)[0]['status'], 'error')
        single = self.seed(1, prompt_tokens=10)
        self.fake.fail_summary = False
        turn = self.store.get_turn(await self.run_turn(single))
        self.assertEqual((turn.state, turn.error), ('failed', TOO_LONG))

    async def test_overflow_after_a_tool_round_blames_the_web_results_not_the_summary(self):
        from roost.compaction import TOOLS_TOO_LONG
        self.set_context(None)
        await self.web_fixture('web')
        self.fake.max_chars = 1200
        tid = await self.run_turn(self.seed(1, prompt_tokens=10), 'Search the web')
        turn = self.store.get_turn(tid)
        self.assertEqual((turn.state, turn.error), ('failed', TOOLS_TOO_LONG))
        self.assertEqual(self.summary_requests(), [])

    async def test_a_three_chunk_compaction_leaves_one_summary_row_that_the_prompt_uses(self):
        cid = self.seed(5, width=170)  # Three folded turns of 85 tokens against a 100 token budget: three chunks.
        with patch('roost.compaction.budget', return_value=100):
            tid = await self.run_turn(cid)
        self.assertGreaterEqual(len(self.summary_requests()), 3)
        rows = self.store.summaries(cid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.store.get_turn(tid).summary_id, rows[0]['id'])
        view = await self.request('GET', f'/api/conversations/{cid}')
        self.assertEqual(view['summaries'], rows)
        self.assertIn('Synthetic running summary.', self.generations()[0]['messages'][0]['content'])
        self.assertEqual(self.store.trace(tid)[0]['status'], 'completed')

    async def test_a_single_oversize_turn_is_cut_to_the_chunk_budget(self):
        cid = self.seed(3, width=6000)
        await self.run_turn(cid)
        sent = self.summary_requests()[0]['messages'][1]['content']
        self.assertIn('[…]', sent)
        self.assertLess(len(sent), 1200)
        self.assertEqual([s['through_ordinal'] for s in self.store.summaries(cid)], [1])

    async def test_summary_calls_are_bounded_and_partial_progress_is_kept(self):
        from roost import compaction
        cid = self.seed(14, width=20)
        with patch.object(compaction, 'MAX_CALLS', 1), patch.object(compaction, 'MIN_KEPT', 1), patch.object(compaction, 'budget', return_value=40):
            tid = await self.run_turn(cid)
        self.assertEqual(self.store.get_turn(tid).state, 'completed')
        self.assertEqual(len(self.summary_requests()), 1)
        through = self.store.summaries(cid)[0]['through_ordinal']
        self.assertTrue(0 < through < 13)
        prompt = self.generations()[0]['messages']
        self.assertEqual(sum(m['role'] == 'user' for m in prompt), 14 - through + 1)

    async def test_a_summary_cut_off_at_the_token_limit_is_a_failed_summary(self):
        self.fake.completion['choices'][0]['finish_reason'] = 'length'
        cid = self.seed(6)
        tid = await self.run_turn(cid)
        self.assertEqual(self.store.get_turn(tid).state, 'completed')
        self.assertEqual(self.store.summaries(cid), [])
        self.assertEqual(self.store.trace(tid)[0]['status'], 'error')

    async def test_stream_sends_summaries_on_the_first_frame_and_only_when_they_change(self):
        cid = self.seed(2)
        self.store.db.execute("INSERT INTO summaries (conversation_id,through_ordinal,text,model,created_at) VALUES (?,?,?,?,?)",
                              (cid, 1, 'Earlier.', 'example-model', 1))
        self.fake.delay = .3
        sent = await self.send(cid, 'Slow')
        frames = []
        async with self.client.get(f'/api/conversations/{cid}/stream') as response:
            async with asyncio.timeout(10):
                while True:
                    line = await response.content.readline()
                    if not line:
                        break
                    if line.startswith(b'data: '):
                        frames.append(json.loads(line[6:]))
                        if frames[-1]['done']:
                            break
        self.assertGreaterEqual(len(frames), 2)
        self.assertEqual([s['text'] for s in frames[0]['summaries']], ['Earlier.'])
        self.assertTrue(all('summaries' not in frame for frame in frames[1:]))

    async def test_summary_failure_on_the_threshold_path_continues_without_compacting(self):
        self.fake.fail_summary = True
        cid = self.seed(6)
        tid = await self.run_turn(cid)
        turn = self.store.get_turn(tid)
        self.assertEqual((turn.state, turn.error, turn.summary_id), ('completed', None, None))
        self.assertEqual(self.store.summaries(cid), [])
        messages = self.generations()[0]['messages']
        self.assertEqual(sum(m['role'] == 'user' for m in messages), 7)
        self.assertEqual(self.store.trace(tid)[0]['status'], 'error')
        self.assertEqual(self.store.trace(tid)[0]['name'], 'summarize')

    async def test_edit_and_regenerate_invalidate_covered_summaries_and_delete_removes_all(self):
        cid = self.seed(6)
        last = await self.run_turn(cid)
        self.assertEqual(len(self.store.summaries(cid)), 1)
        # Regenerating a turn after the folded range keeps the summary and snapshots it again.
        await self.control(cid, 'regenerate', last)
        await self.settled(last)
        self.assertEqual(self.store.get_turn(last).summary_id, self.store.summaries(cid)[0]['id'])
        edited = await self.control(cid, 'edit', last, text='Changed', request_id=str(uuid4()))
        self.assertEqual(len(self.store.summaries(cid)), 1)
        await self.settled(edited['turn_id'])
        turns = self.store.list_turns(cid)
        second = await self.control(cid, 'edit', turns[1].id, text='Rewritten', request_id=str(uuid4()))
        await self.settled(second['turn_id'])
        self.assertEqual(self.store.summaries(cid), [])
        self.assertEqual((await self.request('GET', f'/api/conversations/{cid}'))['summaries'], [])
        self.assertTrue(all(row[0] is None for row in self.store.db.execute('SELECT summary_id FROM turns')))
        # An earlier-turn regenerate also drops a summary that folded it.
        cid = self.seed(6)
        await self.run_turn(cid)
        accepted = self.store.list_turns(cid)[-1]
        self.store.db.execute("INSERT INTO summaries (conversation_id,through_ordinal,text,model,created_at) VALUES (?,?,?,?,?)",
                              (cid, accepted.ordinal, 'Covers the latest', 'example-model', 1))
        self.store.regenerate(accepted.id)
        self.assertEqual([s['through_ordinal'] for s in self.store.summaries(cid)], [4])
        self.assertIsNone(self.store.get_turn(accepted.id).summary_id)
        await self.request('DELETE', f'/api/conversations/{cid}', {'confirm': True})
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM summaries WHERE conversation_id=?', (cid,)).fetchone()[0], 0)
