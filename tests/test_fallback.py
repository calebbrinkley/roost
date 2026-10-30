"""Fallback model server: when it takes over, when it must not, and how replies are labelled."""

import asyncio
import json
import unittest
from unittest.mock import patch

from aiohttp import web

from roost import settings
from roost.provider import Provider, ProviderConnectionError, ProviderError, ProviderHTTPError, ProviderTimeout
from roost.runner import Runner, starting_failure
from roost.tools import REMEMBER_GUIDANCE
from openai_fake import OpenAIFake, frame
import test_chat
from test_chat import wait_for

PRIMARY_KEY, FALLBACK_KEY = 'primary-secret-key', 'fallback-secret-key'
BACKUP_MODEL = {'id': 'backup-model', 'display_name': 'Backup', 'enabled': True, 'thinking_mode': 'reasoning_effort', 'context_tokens': 2048}


class DroppingFake(OpenAIFake):
    """Starts a reply, then cuts the connection."""

    async def chat(self, request):
        self.requests.append((request.method, dict(request.headers), await request.json()))
        stream = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
        await stream.prepare(request)
        await stream.write(frame({'content': 'Partial'}))
        await asyncio.sleep(.05)
        request.transport.close()
        return stream


class ToolDeltaDrop(OpenAIFake):
    """Starts a tool call, then cuts the connection."""

    async def chat(self, request):
        self.requests.append((request.method, dict(request.headers), await request.json()))
        stream = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
        await stream.prepare(request)
        await stream.write(frame({'tool_calls': [{'index': 0, 'id': 'c1', 'type': 'function',
                                                  'function': {'name': 'remember', 'arguments': '{"fa'}}]}))
        await asyncio.sleep(.05)
        request.transport.close()
        return stream


class ToolChoiceThenBusy(OpenAIFake):
    """Rejects a forced tool choice with 400, then answers every retry with 503."""

    async def chat(self, request):
        payload = await request.json()
        self.requests.append((request.method, dict(request.headers), payload))
        if payload['stream'] and payload.get('tool_choice'):
            return web.json_response({'error': 'tool_choice unsupported'}, status=400)
        if payload['stream']:
            return web.Response(status=503, text='busy')
        return await super().chat(request)


class OverflowThenBusy(OpenAIFake):
    """Reports a context overflow on replies and 503 on summaries."""

    async def chat(self, request):
        payload = await request.json()
        self.requests.append((request.method, dict(request.headers), payload))
        if payload['stream']:
            return web.json_response({'error': {'message': 'context_length_exceeded'}}, status=400)
        return web.Response(status=503, text='busy')


class StartingFailureTests(unittest.TestCase):
    def test_only_failures_to_start_count(self):
        timeout = ProviderTimeout('x')
        connect = ProviderTimeout('x')
        connect.connect = True
        started = ProviderConnectionError('x')
        started.started = True
        cases = [(ProviderConnectionError('x'), True), (connect, True), (timeout, False), (started, False),
                 *((ProviderHTTPError(status), True) for status in (408, 429, 500, 502, 503, 504)),
                 *((ProviderHTTPError(status), False) for status in (400, 401, 403, 404, 422)),
                 (ProviderHTTPError(400, '', 'context_overflow'), False), (ProviderHTTPError(503, '', 'context_overflow'), False),
                 (ProviderError('x'), False)]
        for exc, expected in cases:
            with self.subTest(exc=repr(exc), status=getattr(exc, 'status', None)):
                self.assertEqual(starting_failure(exc) is not None, expected)


class FallbackTests(unittest.IsolatedAsyncioTestCase):
    """The chat harness with a second fake server behind the fallback role."""

    web_fixture = test_chat.ChatTests.web_fixture
    request, create, send, settled, control = (test_chat.ChatTests.request, test_chat.ChatTests.create, test_chat.ChatTests.send,
                                                test_chat.ChatTests.settled, test_chat.ChatTests.control)

    async def asyncSetUp(self):
        await test_chat.ChatTests.asyncSetUp(self)
        self.fake.delay = 0
        self.fake.frames = frame({'content': 'Primary reply'}) + frame({}, finish='stop', usage={'prompt_tokens': 12, 'completion_tokens': 3})
        self.backup = await OpenAIFake().start()
        self.backup.frames = frame({'content': 'Fallback reply'}) + frame({}, finish='stop', usage={'prompt_tokens': 14, 'completion_tokens': 4})
        self.backup.completion['choices'][0]['message']['content'] = 'Backup summary'
        self.app['servers'].update('primary', {'key': PRIMARY_KEY})
        self.configure()

    async def asyncTearDown(self):
        await self.backup.close()
        await test_chat.ChatTests.asyncTearDown(self)

    def configure(self, *, use=True, tools=True, models=True):
        servers = self.app['servers']
        servers.update('fallback', {'base_url': self.backup.url, 'key': FALLBACK_KEY})
        if models:
            servers.set_models('fallback', {'models': [{**BACKUP_MODEL, 'tools': tools}], 'default_model': 'backup-model'})
        settings.update(self.state / 'settings.json', {'use_fallback': use})

    def streams(self, fake):
        return [r[2] for r in fake.requests if r[2] and r[2]['stream']]

    def calls(self, fake):
        return [r[2] for r in fake.requests if r[2]]

    def summary_models(self, cid):
        return {row[0] for row in self.store.db.execute('SELECT model FROM summaries WHERE conversation_id=?', (cid,))}

    async def ask(self, text='Hello', cid=None):
        sent = await (self.create(text) if cid is None else self.send(cid, text))
        await self.settled(sent['turn_id'])
        return sent['session_id'] if cid is None else cid, sent['turn_id']

    async def last(self, cid):
        return (await self.request('GET', f'/api/conversations/{cid}'))['messages'][-1]

    async def test_every_start_failure_class_falls_back_with_the_fallback_models_settings(self):
        for status in (408, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.fake.status, self.fake.requests, self.backup.requests = status, [], []
                cid, tid = await self.ask()
                turn = self.store.get_turn(tid)
                self.assertEqual(turn.state, 'completed')
                row = await self.last(cid)
                self.assertEqual(row['content'], 'Fallback reply')
                self.assertEqual(row['served_by'], {'server': 'fallback', 'model': 'backup-model', 'display_name': 'Backup'})
                self.assertEqual(len(self.streams(self.fake)), 1)
                request = self.streams(self.backup)[0]
                self.assertEqual(request['model'], 'backup-model')
                self.assertIn('reasoning_effort', request)  # the fallback model's thinking_mode, not the primary's template flag
                self.assertNotIn('chat_template_kwargs', request)
                self.assertEqual(self.backup.requests[0][1]['Authorization'], f'Bearer {FALLBACK_KEY}')
                self.assertEqual(self.fake.requests[0][1]['Authorization'], f'Bearer {PRIMARY_KEY}')
                self.assertEqual(row['trace'][-1], {'kind': 'tool', 'id': 'fallback-1', 'name': 'fallback', 'arguments': '{}',
                    'status': 'completed', 'result': f"Primary couldn't start (HTTP {status}); answered with Backup on the fallback server"})
                self.assertEqual(self.store.get_turn(tid).stats['context_tokens'], 2048)
                self.assertEqual((await self.request('GET', f'/api/conversations/{cid}'))['context']['context_tokens'], 2048)

    async def test_refused_connection_connect_timeout_and_dns_failure_fall_back(self):
        dead = await OpenAIFake().start()
        dead_url = dead.url
        await dead.close()
        self.app['servers'].update('primary', {'base_url': dead_url})
        cid, tid = await self.ask()
        self.assertEqual(self.store.get_turn(tid).state, 'completed')
        self.assertEqual((await self.last(cid))['served_by']['server'], 'fallback')
        self.assertEqual((await self.last(cid))['trace'][-1]['result'],
                         "Primary couldn't start (connection refused or reset); answered with Backup on the fallback server")
        original = Provider.stream
        connect = ProviderTimeout('Model server timed out.')
        connect.connect = True
        for label, error in (('connect timeout', connect), ('dns', ProviderConnectionError('Model server connection failed or ended early.'))):
            async def stream(provider, *args, **kwargs):
                if provider.url != self.backup.url:
                    raise error
                async for event in original(provider, *args, **kwargs):
                    yield event
            with self.subTest(label), patch.object(Provider, 'stream', stream):
                cid, tid = await self.ask()
                self.assertEqual((await self.last(cid))['served_by']['server'], 'fallback')

    async def test_client_errors_overflow_read_timeout_and_cancel_never_fall_back(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                self.fake.status, self.backup.requests = status, []
                cid, tid = await self.ask()
                self.assertEqual(self.store.get_turn(tid).state, 'failed')
                self.assertEqual(self.backup.requests, [])
        self.fake.status, self.fake.error_body = 400, 'context_length_exceeded'
        cid, tid = await self.ask()
        self.assertEqual(self.store.get_turn(tid).state, 'failed')
        self.assertEqual(self.backup.requests, [])
        original = Provider.stream
        async def silent(provider, *args, **kwargs):
            if provider.url != self.backup.url:
                raise ProviderTimeout('Model server timed out.')  # a read deadline, not a connection one
            async for event in original(provider, *args, **kwargs):
                yield event
        with patch.object(Provider, 'stream', silent):
            cid, tid = await self.ask()
        self.assertEqual(self.store.get_turn(tid).state, 'failed')
        self.assertEqual(self.backup.requests, [])
        self.fake.status, self.fake.delay = 200, 30
        sent = await self.create()
        await wait_for(lambda: self.store.get_turn(sent['turn_id']).state == 'running')
        await asyncio.sleep(.1)
        await self.control(sent['session_id'], 'stop', sent['turn_id'])
        self.assertEqual(self.store.get_turn(sent['turn_id']).state, 'interrupted')
        self.assertEqual(self.backup.requests, [])

    async def test_a_failure_after_the_reply_started_is_never_rerun_on_the_fallback(self):
        dropping = await DroppingFake().start()
        try:
            self.app['servers'].update('primary', {'base_url': dropping.url})
            cid, tid = await self.ask()
            turn = self.store.get_turn(tid)
            self.assertEqual(turn.state, 'failed')
            self.assertEqual((await self.last(cid))['content'], 'Partial')
            self.assertEqual(self.backup.requests, [])
            self.assertEqual((await self.last(cid))['served_by']['server'], 'primary')
            # A stream that ends early, with text but no finish reason, is also mid-reply.
            self.fake.frames = frame({'content': 'Cut'})
            self.app['servers'].update('primary', {'base_url': self.fake.url})
            cid, tid = await self.ask()
            self.assertEqual(self.store.get_turn(tid).state, 'failed')
            self.assertEqual(self.backup.requests, [])
        finally:
            await dropping.close()

    async def test_switch_off_local_chat_unconfigured_and_job_flag_mean_no_fallback(self):
        self.fake.status = 503
        async def refused(cid, tid):
            turn = self.store.get_turn(tid)
            self.assertEqual(turn.state, 'failed')
            self.assertEqual(turn.error, 'Model server returned HTTP 503.')
            self.assertEqual(self.backup.requests, [])
            self.assertEqual((await self.last(cid))['served_by']['server'], 'primary')
        settings.update(self.state / 'settings.json', {'use_fallback': False})
        await refused(*await self.ask())
        settings.update(self.state / 'settings.json', {'use_fallback': True})
        sent = await self.create()
        await self.settled(sent['turn_id'])
        self.assertEqual(self.store.get_turn(sent['turn_id']).state, 'completed')
        self.backup.requests = []
        cid = sent['session_id']
        await self.request('PUT', f'/api/conversations/{cid}/settings', {'keep_local': True})
        local = await self.send(cid, 'Stay here')
        await self.settled(local['turn_id'])
        await refused(cid, local['turn_id'])
        # Only the chat's current setting counts: untick it, regenerate, and the fallback is tried.
        await self.request('PUT', f'/api/conversations/{cid}/settings', {'keep_local': False})
        await self.control(cid, 'regenerate', local['turn_id'])
        await self.settled(local['turn_id'])
        self.assertEqual(self.store.get_turn(local['turn_id']).state, 'completed')
        self.assertEqual((await self.last(cid))['served_by']['server'], 'fallback')
        self.backup.requests = []
        original = Runner.generate
        async def jobs(runner, tid, **kwargs):
            return await original(runner, tid, fallback=False)
        with patch.object(Runner, 'generate', jobs):
            await refused(*await self.ask())
        # Unconfigured: the switch is on but there is no server, no models, or no default model.
        for models in ([], [{**BACKUP_MODEL, 'enabled': False}]):
            self.app['servers'].set_models('fallback', {'models': models, 'default_model': None})
            await refused(*await self.ask())
        path = self.app['servers'].path
        stored = json.loads(path.read_text())
        del stored['servers']['fallback']
        path.write_text(json.dumps(stored))
        await refused(*await self.ask())

    async def test_trace_wording_follows_the_turn_state(self):
        cid = self.store.create_conversation(model='example-model').id
        tid = self.store.accept_turn(cid, 'seed-state', 'Hi', model='example-model').turn_id
        self.store.set_turn_state(tid, 'running')
        self.store.append_step(tid, 'fallback_call', {'id': 'fallback-1', 'reason': 'HTTP 503', 'model': 'backup-model', 'display_name': 'Backup'})
        item = lambda: {k: v for k, v in self.store.trace(tid)[0].items() if k in ('status', 'result')}
        self.assertEqual(item(), {'status': 'running', 'result': "Primary couldn't start (HTTP 503); answering with Backup on the fallback server"})
        self.store.set_turn_state(tid, 'interrupted')
        self.assertEqual(item(), {'status': 'interrupted', 'result': "Primary couldn't start (HTTP 503); switched to Backup on the fallback server"})

    async def test_a_tool_call_delta_then_a_drop_is_mid_reply_and_never_falls_back(self):
        dropping = await ToolDeltaDrop().start()
        try:
            self.app['servers'].update('primary', {'base_url': dropping.url})
            cid, tid = await self.ask()
            self.assertEqual(self.store.get_turn(tid).state, 'failed')
            self.assertEqual(self.backup.requests, [])
        finally:
            await dropping.close()

    async def test_a_forced_search_the_fallback_cannot_run_is_dropped_not_failed(self):
        await self.web_fixture()
        self.configure(tools=False)
        flip = await ToolChoiceThenBusy().start()
        try:
            self.app['servers'].update('primary', {'base_url': flip.url})
            sent = await self.create('Look it up', web='force')
            await self.settled(sent['turn_id'])
            self.assertEqual(self.store.get_turn(sent['turn_id']).state, 'completed')
            request = self.streams(self.backup)[0]
            self.assertNotIn('tools', request)
            self.assertNotIn('Search the web first', ' '.join(m['content'] for m in request['messages'] if m['role'] == 'system'))
            row = await self.last(sent['session_id'])
            self.assertEqual(row['content'], 'Fallback reply')
            self.assertEqual(row['trace'], [{'kind': 'tool', 'id': 'fallback-1', 'name': 'fallback', 'arguments': '{}', 'status': 'completed',
                'result': "Primary couldn't start (HTTP 503); answered with Backup on the fallback server; the forced web search wasn't available there"}])
        finally:
            await flip.close()

    async def test_compaction_that_moves_to_the_fallback_after_an_overflow_drops_the_primarys_tools(self):
        primary = self.app['servers'].get('primary')
        self.app['servers'].set_models('primary', {'models': [{k: v for k, v in primary['models'][0].items() if k != 'context_tokens'}],
                                                   'default_model': 'example-model'})
        self.configure(tools=False)
        cid = self.store.create_conversation(model='example-model').id
        for number in range(1, 9):
            accepted = self.store.accept_turn(cid, f'seed-{number}', f'q{number} ' + 'u' * 90, model='example-model')
            self.store.set_turn_state(accepted.turn_id, 'running')
            self.store.checkpoint_assistant(accepted.turn_id, f'a{number} ' + 'r' * 90)
            self.store.finish(accepted.turn_id, {'usage': {'prompt_tokens': 10}})
        busy = await OverflowThenBusy().start()
        try:
            self.app['servers'].update('primary', {'base_url': busy.url})
            _, tid = await self.ask('Next', cid)
            self.assertEqual(self.store.get_turn(tid).state, 'completed')
            request = self.streams(self.backup)[0]
            self.assertNotIn('tools', request)
            self.assertNotIn(REMEMBER_GUIDANCE, request['messages'][0]['content'])
        finally:
            await busy.close()

    async def test_a_compaction_divert_reruns_the_same_chunk_without_using_up_a_call(self):
        primary = self.app['servers'].get('primary')
        self.app['servers'].set_models('primary', {'models': [{**primary['models'][0], 'context_tokens': 400}], 'default_model': 'example-model'})
        cid = self.store.create_conversation(model='example-model').id
        for number in range(1, 7):
            accepted = self.store.accept_turn(cid, f'seed-{number}', f'q{number} ' + 'u' * 90, model='example-model')
            self.store.set_turn_state(accepted.turn_id, 'running')
            self.store.checkpoint_assistant(accepted.turn_id, f'a{number} ' + 'r' * 90)
            self.store.finish(accepted.turn_id, {'usage': {'prompt_tokens': 300}})
        self.fake.status = 503
        with patch('roost.compaction.MAX_CALLS', 1):  # one summary call allowed, so a diverted attempt must not count
            _, tid = await self.ask('Next', cid)
        self.assertEqual(self.summary_models(cid), {'backup-model'})
        self.assertEqual([bool(r['stream']) for r in self.calls(self.backup)], [False, True])

    async def test_both_servers_failing_names_both_roles_and_leaks_nothing(self):
        self.fake.status = 503
        self.backup.status = 500
        cid, tid = await self.ask()
        turn = self.store.get_turn(tid)
        self.assertEqual(turn.state, 'failed')
        self.assertEqual(turn.error, 'The primary server could not start this reply (HTTP 503), and the fallback server failed too: Model server returned HTTP 500.')
        row = await self.last(cid)
        self.assertEqual(row['trace'][-1]['status'], 'error')
        self.assertEqual(row['trace'][-1]['result'], "Primary couldn't start (HTTP 503); Backup on the fallback server failed too")
        self.assertEqual(row['served_by']['server'], 'fallback')
        exported = await self.request('GET', f'/api/conversations/{cid}/export')
        settings_view = json.dumps([await self.request('GET', '/api/settings/servers'), await self.request('GET', '/api/settings')])
        shown = json.dumps([row, exported, turn.error])
        for key in (PRIMARY_KEY, FALLBACK_KEY):
            self.assertNotIn(key, shown + settings_view)
        for address in (self.fake.url, self.backup.url, str(self.fake.server.port), str(self.backup.server.port)):
            self.assertNotIn(address, shown)

    async def test_tool_rounds_stay_on_the_fallback_and_use_its_tool_flags(self):
        self.fake.status = 503
        self.backup.scenario = 'remember'
        cid, tid = await self.ask('Keep my writing preference')
        self.assertEqual(self.store.get_turn(tid).state, 'completed')
        self.assertEqual(len(self.streams(self.fake)), 1)  # the only other primary call is the title
        rounds = self.streams(self.backup)
        self.assertEqual(len(rounds), 2)
        self.assertTrue(any(m['role'] == 'tool' for m in rounds[1]['messages']))
        self.assertTrue(all(REMEMBER_GUIDANCE in r['messages'][0]['content'] for r in rounds))
        row = await self.last(cid)
        self.assertEqual([t['name'] for t in row['trace'] if t['kind'] == 'tool'], ['fallback', 'remember'])
        self.assertEqual(row['served_by']['server'], 'fallback')
        # A fallback model without tools gets none, even though the primary was offered them.
        self.configure(tools=False)
        self.fake.requests, self.backup.requests = [], []
        self.backup.scenario = None
        await self.ask()
        self.assertIn(REMEMBER_GUIDANCE, self.streams(self.fake)[0]['messages'][0]['content'])
        self.assertIn('tools', self.streams(self.fake)[0])
        request = self.streams(self.backup)[0]
        self.assertNotIn('tools', request)
        self.assertNotIn(REMEMBER_GUIDANCE, request['messages'][0]['content'])

    async def test_compaction_before_the_first_call_moves_to_the_fallback_and_stays(self):
        primary = self.app['servers'].get('primary')
        self.app['servers'].set_models('primary', {'models': [{**primary['models'][0], 'context_tokens': 400}], 'default_model': 'example-model'})
        cid = self.store.create_conversation(model='example-model').id
        for number in range(1, 7):
            accepted = self.store.accept_turn(cid, f'seed-{number}', f'q{number} ' + 'u' * 90, model='example-model')
            self.store.set_turn_state(accepted.turn_id, 'running')
            self.store.checkpoint_assistant(accepted.turn_id, f'a{number} ' + 'r' * 90)
            self.store.finish(accepted.turn_id, {'usage': {'prompt_tokens': 300}})
        self.fake.status = 503
        _, tid = await self.ask('Next', cid)
        self.assertEqual(self.store.get_turn(tid).state, 'completed')
        self.assertEqual(len(self.calls(self.fake)), 1)  # only the summary attempt
        self.assertEqual([bool(r['stream']) for r in self.calls(self.backup)], [False, True])
        summary = self.store.summaries(cid)[0]
        self.assertEqual((summary['text'], self.summary_models(cid)), ('Backup summary', {'backup-model'}))
        self.assertEqual([t['name'] for t in self.store.trace(tid)], ['summarize', 'fallback'])

    async def test_overflow_compaction_after_a_switch_runs_on_the_fallback(self):
        primary = self.app['servers'].get('primary')
        self.app['servers'].set_models('primary', {'models': [{k: v for k, v in primary['models'][0].items() if k != 'context_tokens'}],
                                                   'default_model': 'example-model'})
        cid = self.store.create_conversation(model='example-model').id
        for number in range(1, 9):
            accepted = self.store.accept_turn(cid, f'seed-{number}', f'q{number} ' + 'u' * 90, model='example-model')
            self.store.set_turn_state(accepted.turn_id, 'running')
            self.store.checkpoint_assistant(accepted.turn_id, f'a{number} ' + 'r' * 90)
            self.store.finish(accepted.turn_id, {'usage': {'prompt_tokens': 10}})
        self.fake.status = 503
        self.backup.max_chars = 1500
        _, tid = await self.ask('Next', cid)
        self.assertEqual(self.store.get_turn(tid).state, 'completed')
        self.assertEqual(len(self.calls(self.fake)), 1)
        self.assertGreater(len([r for r in self.calls(self.backup) if not r['stream']]), 1)
        self.assertEqual(self.summary_models(cid), {'backup-model'})
        self.assertEqual((await self.last(cid))['served_by']['server'], 'fallback')

    async def test_history_and_streams_stay_readable_with_an_unreadable_servers_file(self):
        cid, tid = await self.ask()
        self.app['servers'].path.write_text('{broken')
        view = await self.request('GET', f'/api/conversations/{cid}')
        self.assertEqual(view['messages'][-1]['served_by'], {'server': 'primary', 'model': 'example-model', 'display_name': 'example-model'})
        exported = await self.request('GET', f'/api/conversations/{cid}/export')
        self.assertEqual(exported['messages'][-1]['served_by']['display_name'], 'example-model')
        async with self.client.get(f'/api/conversations/{cid}/stream') as response:
            event = json.loads((await response.content.readline())[6:])
        self.assertEqual(event['served_by']['server'], 'primary')

    async def test_the_next_turn_tries_the_primary_again_and_labels_per_turn(self):
        self.fake.status = 503
        cid, first = await self.ask('One')
        self.fake.status = 200
        _, second = await self.ask('Two', cid)
        rows = (await self.request('GET', f'/api/conversations/{cid}'))['messages']
        self.assertEqual([r['served_by']['server'] for r in rows if r['role'] == 'assistant'], ['fallback', 'primary'])
        self.assertEqual(rows[-1]['served_by'], {'server': 'primary', 'model': 'example-model', 'display_name': 'Example'})
        self.assertEqual(rows[-1]['content'], 'Primary reply')
        self.assertNotIn('fallback', [t.get('name') for t in rows[-1]['trace']])
        self.assertNotIn('context_tokens', self.store.get_turn(second).stats)
        # The fallback's answer is part of the history the primary now sees.
        self.assertIn({'role': 'assistant', 'content': 'Fallback reply'}, self.streams(self.fake)[-1]['messages'])

    async def test_served_by_rides_on_stream_frames_and_export(self):
        self.fake.status = 503
        cid, tid = await self.ask()
        async with self.client.get(f'/api/conversations/{cid}/stream') as response:
            event = json.loads((await response.content.readline())[6:])
        self.assertEqual(event['served_by'], {'server': 'fallback', 'model': 'backup-model', 'display_name': 'Backup'})
        exported = await self.request('GET', f'/api/conversations/{cid}/export')
        self.assertEqual(exported['messages'][-1]['served_by']['server'], 'fallback')
        self.assertNotIn('served_by', exported['messages'][0])

    async def test_regenerate_forgets_the_fallback_until_it_is_needed_again(self):
        self.fake.status = 503
        cid, tid = await self.ask()
        self.fake.status = 200
        await self.control(cid, 'regenerate', tid)
        await self.settled(tid)
        self.assertEqual((await self.last(cid))['served_by']['server'], 'primary')

    async def test_settings_switch_is_gated_on_a_configured_fallback_and_keys_stay_private(self):
        settings.update(self.state / 'settings.json', {'use_fallback': False})
        servers = self.app['servers']
        path = servers.path
        stored = json.loads(path.read_text())
        saved_fallback = stored['servers'].pop('fallback')
        path.write_text(json.dumps(stored))
        await self.request('PATCH', '/api/settings', {'use_fallback': True}, 400)
        stored['servers']['fallback'] = {**saved_fallback, 'models': [], 'default_model': None}
        path.write_text(json.dumps(stored))
        refused = await self.request('PATCH', '/api/settings', {'use_fallback': True}, 400)
        self.assertIn('fallback server and default model', refused['error'])
        self.assertFalse(settings.load(self.state / 'settings.json')['use_fallback'])
        saved = await self.request('PUT', '/api/settings/servers/fallback', {'base_url': self.backup.url, 'key': FALLBACK_KEY})
        self.assertTrue(saved['has_key'])
        self.assertNotIn(FALLBACK_KEY, json.dumps(saved))
        await self.request('PATCH', '/api/settings', {'use_fallback': True}, 400)
        await self.request('PUT', '/api/settings/servers/fallback/models', {'models': [BACKUP_MODEL], 'default_model': 'backup-model'})
        for bad in ('yes', 1, None):
            await self.request('PATCH', '/api/settings', {'use_fallback': bad}, 400)
        self.assertTrue((await self.request('PATCH', '/api/settings', {'use_fallback': True}))['use_fallback'])
        listing = await self.request('GET', '/api/settings/servers')
        self.assertEqual([s['id'] for s in listing['servers']], ['primary', 'fallback'])
        self.assertTrue(all(s['has_key'] for s in listing['servers']))
        self.assertNotIn('secret', json.dumps(listing) + json.dumps(await self.request('GET', '/api/settings')))
        # Invalid fallback configuration is refused like the primary's.
        await self.request('PUT', '/api/settings/servers/fallback', {'base_url': 'ftp://example.invalid'}, 400)
        await self.request('PUT', '/api/settings/servers/fallback/models', {'models': [BACKUP_MODEL], 'default_model': 'missing'}, 400)
        self.assertFalse((await self.request('PATCH', '/api/settings', {'use_fallback': False}))['use_fallback'])

    async def test_keep_local_is_a_chat_setting_with_a_boolean_contract(self):
        cid, _ = await self.ask()
        self.assertFalse((await self.request('GET', f'/api/conversations/{cid}/settings'))['keep_local'])
        saved = await self.request('PUT', f'/api/conversations/{cid}/settings', {'keep_local': True})
        self.assertTrue(saved['keep_local'])
        self.assertTrue((await self.request('GET', f'/api/conversations/{cid}/export'))['keep_local'])
        for bad in ('true', 1, None):
            await self.request('PUT', f'/api/conversations/{cid}/settings', {'keep_local': bad}, 400)
        self.assertTrue(self.store.get_conversation(cid).keep_local)


if __name__ == '__main__':
    unittest.main()
