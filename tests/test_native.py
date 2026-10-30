"""Native route, persistence, runner and control contracts against a loopback provider."""

import asyncio
import json
import os
import tempfile
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
from roost.store import Store, StoreError
from roost.servers import Servers, ServerError
from openai_fake import OpenAIFake, frame


async def wait_for(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.01)


def without_clock(messages):
    return [m for m in messages if not (m['role'] == 'system' and m['content'].startswith('Current date and time:'))]


class NativeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = Path(self.directory.name)
        (self.state / 'password.hash').write_text(make_password_hash('example-password'))
        settings.update(self.state / 'settings.json', {'backend': 'native'})
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
        # Any accidental Hermes route call fails instead of being hidden by an unreachable service.
        self.app['hermes'] = AsyncMock()
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
            [{'role': 'system', 'content': 'Be brief.'}, {'role': 'user', 'content': 'First'}],
            [{'role': 'system', 'content': 'Be brief.'}, {'role': 'user', 'content': 'First'},
             {'role': 'assistant', 'content': 'Hello'}, {'role': 'user', 'content': 'Second'}]])
        self.assertTrue(all(r['chat_template_kwargs']['enable_thinking'] for r in streamed))
        await wait_for(lambda: self.store.get_conversation(cid).title == 'A short title')
        requests = [entry[2] for entry in self.fake.requests if entry[2]]
        self.assertEqual([p['stream'] for p in requests], [True, True, False])
        self.assertEqual(requests[-1]['max_tokens'], 32)
        self.assertFalse(requests[-1]['chat_template_kwargs']['enable_thinking'])
        view = await self.request('GET', f'/api/conversations/{cid}')
        self.assertEqual([m['role'] for m in view['messages']], ['user', 'assistant'] * 2)
        self.assertEqual(view['context'], {'prompt_tokens': 12, 'context_tokens': 4096})
        self.assertGreater(view['messages'][-1]['roost_usage']['tokens_per_second'], 0)
        for method in ('profile', 'messages', 'sessions', 'start'):
            getattr(self.app['hermes'], method).assert_not_called()

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

    async def test_shutdown_ends_native_stream_with_queued_work(self):
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
            self.assertFalse(self.app['native_streams'])
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
        for path, expected in (('/api/jobs', {'jobs': []}), ('/api/settings/soul', {'content': ''}),
                               (f'/api/conversations/{cid}/approvals', {'approvals': []})):
            self.assertEqual(await self.request('GET', path), expected)
        about = await self.request('GET', '/api/about')
        self.assertEqual(about['hermes'], {'reachable': False})
        self.assertEqual((await self.request('GET', '/api/me'))['backend'], 'native')
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
        second = await self.send(cid, 'Notify me')
        self.app['presence'].clear()
        await self.settled(second['turn_id'])
        await wait_for(lambda: self.app['push'].send.call_count == 1)
        message = self.app['push'].send.call_args.args[1]
        self.assertEqual(message['tag'], 'reply-' + second['turn_id'])

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
        self.assertNotIn('tools', calls[3])
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
        self.assertNotIn('tools', self.generations()[-1])
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
        self.assertNotIn('tools', self.generations()[-1])
        self.assertIn('Total tool time', self.store.trace(first['turn_id'])[1]['result'])
        clock = self.generations()[0]['messages'][0]['content']
        self.assertIn('-05:00', clock)


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
