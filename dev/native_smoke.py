"""Native browser-contract smoke against the deterministic OpenAI-compatible fake."""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from aiohttp import ClientSession, CookieJar
from aiohttp.test_utils import TestServer

from roost.auth import make_password_hash
from roost.server import create_app

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from openai_fake import OpenAIFake, frame
from web_fake import WebFake
from speech_fake import SpeechFake


def configure(state, url):
    from roost import settings
    from roost.servers import Servers
    settings.update(state / 'settings.json', {'backend': 'native'})
    servers = Servers(state, environ={})
    servers.update('primary', {'base_url': url})
    servers.set_models('primary', {'models': [{'id': 'example-model', 'display_name': 'Example', 'enabled': True,
                                               'thinking_mode': 'template_flag', 'context_tokens': 4096}],
                                  'default_model': 'example-model'})


async def run():
    with tempfile.TemporaryDirectory(prefix='roost-native-smoke-') as directory:
        state = Path(directory)
        (state / 'password.hash').write_text(make_password_hash('example-password'))
        fake = await OpenAIFake().start()
        search = await WebFake().start()
        backup = await OpenAIFake().start()
        speech = await SpeechFake().start()
        fake.delay, fake.chunk_delay = .1, .3
        partial = frame({'content': 'Native partial reply. ' * 60})
        fake.frames = partial + frame({'content': 'Finished.'}) + frame({}, finish='stop', usage={'prompt_tokens': 20, 'completion_tokens': 8})
        fake.chunk_sizes = (len(partial),)
        fake.completion['choices'][0]['message']['content'] = 'Native smoke title'
        configure(state, fake.url)
        with patch.dict(os.environ, {'ROOST_STATE_DIR': str(state), 'ROOST_PUBLIC_ORIGIN': 'http://localhost'}):
            app = create_app()
        roost = TestServer(app)
        await roost.start_server()
        headers = {'Origin': 'http://localhost', 'X-Roost-Request': '1'}
        try:
            async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
                async def request(method, path, body=None, status=200):
                    async with client.request(method, roost.make_url(path), headers=headers, json=body) as response:
                        result = await response.json()
                        assert response.status == status, (path, response.status, result)
                        return result

                async def settled(tid):
                    async with asyncio.timeout(10):
                        while app['store'].get_turn(tid).state in ('queued', 'running'):
                            await asyncio.sleep(.01)

                async def control(cid, action, body):
                    return await request('POST', f'/api/conversations/{cid}/turns/{action}', body)

                await request('POST', '/api/login', {'password': 'example-password'})
                assert (await request('GET', '/api/me'))['backend'] == 'native'
                assert (await request('GET', '/api/models'))['default_model'] == 'example-model'
                first = await request('POST', '/api/conversations', {'text': 'Native searchable message', 'request_id': str(uuid4())}, 201)
                cid, tid = first['session_id'], first['turn_id']
                path = '/api/conversations/' + cid
                async with client.get(roost.make_url(path + '/stream')) as response:
                    assert response.status == 200
                    async with asyncio.timeout(5):
                        while True:
                            line = await response.content.readline()
                            if line.startswith(b'data: '):
                                event = json.loads(line[6:])
                                assert 'revision' in event and 'turn_id' in event
                                if event.get('text'):
                                    break
                # Closing the stream leaves the native work alive.
                assert app['store'].get_turn(tid).state == 'running'
                second = await request('POST', path + '/messages', {'text': 'Second queued message', 'request_id': str(uuid4())})
                third = await request('POST', path + '/messages', {'text': 'Third send now', 'request_id': str(uuid4())})
                cancelled = await request('POST', path + '/messages', {'text': 'Cancel this', 'request_id': str(uuid4())})
                assert (await request('GET', path))['queue']
                await control(cid, 'cancel', {'turn_id': cancelled['turn_id']})
                await control(cid, 'send-now', {'turn_id': third['turn_id']})
                assert app['store'].get_turn(tid).state == 'interrupted'
                async with asyncio.timeout(5):
                    while app['store'].get_turn(third['turn_id']).state != 'running':
                        await asyncio.sleep(.01)
                await control(cid, 'stop', {'turn_id': third['turn_id']})
                await settled(second['turn_id'])
                assert [t.id for t in app['store'].list_turns(cid)] == [tid, third['turn_id'], second['turn_id']]
                await control(cid, 'regenerate', {'turn_id': second['turn_id']})
                await settled(second['turn_id'])
                edited = await control(cid, 'edit', {'turn_id': tid, 'text': 'Edited searchable message', 'request_id': str(uuid4())})
                await settled(edited['turn_id'])
                async with asyncio.timeout(5):
                    while app['store'].get_conversation(cid).title != 'Native smoke title':
                        await asyncio.sleep(.01)
                assert (await request('GET', '/api/search?q=searchable'))['sessions'][0]['id'] == cid
                for query in ('%22', 'OR', 'content%3Asearchable'):
                    assert not (await request('GET', '/api/search?q=' + query))['sessions']
                view = await request('GET', path)
                assert len(view['messages']) == 2 and view['context']['prompt_tokens'] == 20
                history = await request('GET', path + '?before=' + str(view['messages'][-1]['id']) + '&limit=1')
                assert history['messages'][0]['role'] == 'user'
                # Native voice talks directly to its speech server; no Hermes service exists.
                await request('PUT', '/api/settings/voice', {'same_as_model': False, 'base_url': speech.url,
                                                            'key': 'example-speech-key'})
                for mime in ('audio/mp4', 'audio/webm;codecs=opus'):
                    recorded = await request('POST', '/api/audio/transcribe', {
                        'data_url': 'data:' + mime + ';base64,Y2xpcA==', 'mime_type': mime, 'duration_seconds': 1})
                    assert recorded == {'transcript': 'Hello'}
                mid = view['messages'][-1]['id']
                speech_url = roost.make_url(path + '/speech/' + str(mid))
                async with client.get(speech_url) as response:
                    assert response.status == 200 and await response.read() == speech.audio
                async with client.get(speech_url, headers={'Range': 'bytes=0-2'}) as response:
                    assert response.status == 206 and await response.read() == speech.audio[:3]
                assert len(speech.calls) == 3 and speech.calls[-1][0] == 'Bearer example-speech-key'
                exported = await request('GET', path + '/export')
                assert exported['messages'][0]['content'] == 'Edited searchable message'
                await request('PATCH', path, {'title': 'My title'})
                assert (await request('GET', '/api/conversations'))['sessions'][0]['title'] == 'My title'
                assert (await request('GET', '/api/jobs')) == {'jobs': []}
                assert (await request('GET', '/api/settings/soul')) == {'content': ''}
                # Persona and global memory use the same authenticated browser contracts.
                persona = await request('POST', '/api/personas', {'name': 'Guide', 'prompt': 'You explain warmly in short paragraphs.'}, 201)
                await request('PUT', path + '/settings', {'persona_id': persona['id'], 'no_web': True})
                manual = await request('POST', '/api/memory', {'text': 'The user likes concrete examples.',
                                       'source_message_id': view['messages'][0]['id'], 'conversation_id': cid}, 201)
                fake.scenario, fake.chunk_delay = 'remember', 0
                remembered = await request('POST', path + '/messages', {'text': 'Keep my writing preference', 'request_id': str(uuid4())})
                await settled(remembered['turn_id'])
                reply = (await request('GET', path))['messages'][-1]
                assert reply['persona_name'] == 'Guide'
                assert reply['memories'] == [{'id': manual['fact']['id'], 'text': 'The user likes concrete examples.'}]
                saved = next(item for item in reply['trace'] if item.get('name') == 'remember')
                assert saved['fact'] == 'The user prefers short paragraphs.' and not saved['forgotten']
                facts = (await request('GET', '/api/memory'))['facts']
                assert [fact['origin'] for fact in facts] == ['user', 'model']
                assert facts[-1]['source_message_id'] == remembered['assistant_message_id']
                await request('DELETE', '/api/memory/' + str(saved['fact_id']))
                assert next(item for item in (await request('GET', path))['messages'][-1]['trace'] if item.get('name') == 'remember')['forgotten']
                await request('PUT', path + '/settings', {'no_web': False})
                # Real local HTTP tools, with only the page resolver redirected to the test listener.
                await request('PATCH', '/api/settings', {'search_url': search.url})
                assert (await request('POST', '/api/settings/web-search/test', {'query': 'example'}))['results']
                fake.scenario, fake.chunk_delay = 'web', 0
                with patch('roost.runner.WebTools', side_effect=search.tools):
                    web_turn = await request('POST', path + '/messages', {'text': 'Search the web', 'web': 'force', 'request_id': str(uuid4())})
                    await settled(web_turn['turn_id'])
                reply = (await request('GET', path))['messages'][-1]
                assert reply['content'] == 'A useful answer [1].'
                assert [item['n'] for item in reply['sources']] == [1, 2]
                assert [item['name'] for item in reply['trace'] if item['kind'] == 'tool'] == ['web_search', 'read_page']
                async with client.get(roost.make_url(path + '/stream')) as response:
                    event = json.loads((await response.content.readline())[6:])
                    assert event['reset'] and event['sources'] == reply['sources'] and event['trace'] == reply['trace']
                await request('PUT', path + '/settings', {'no_web': True})
                no_web = await request('POST', path + '/messages', {'text': 'No web for this chat', 'request_id': str(uuid4())})
                await settled(no_web['turn_id'])
                assert not app['store'].get_turn(no_web['turn_id']).settings['tools_offered']
                # A small context makes the fourth message of a fresh chat fold its first turn into a summary.
                from roost.servers import Servers
                Servers(state, environ={}).set_models('primary', {'models': [{'id': 'example-model', 'display_name': 'Example', 'enabled': True,
                                                                             'thinking_mode': 'template_flag', 'context_tokens': 24}],
                                                                  'default_model': 'example-model'})
                fake.scenario = None
                long_chat = None
                for number in range(1, 5):
                    body = {'text': f'Compaction message {number}', 'request_id': str(uuid4())}
                    sent = await request('POST', '/api/conversations' if long_chat is None else f'/api/conversations/{long_chat}/messages', body, 201 if long_chat is None else 200)
                    long_chat = long_chat or sent['session_id']
                    await settled(sent['turn_id'])
                folded = await request('GET', '/api/conversations/' + long_chat)
                assert [(s['through_ordinal'], s['text']) for s in folded['summaries']] == [(1, 'Native smoke title')], folded['summaries']
                assert len(folded['messages']) == 8 and folded['messages'][-1]['trace'][0]['name'] == 'summarize'
                assert folded['messages'][-1]['trace'][0]['status'] == 'completed'
                async with client.get(roost.make_url('/api/conversations/' + long_chat + '/stream')) as response:
                    assert json.loads((await response.content.readline())[6:])['summaries'] == folded['summaries']
                last_prompt = [r[2] for r in fake.requests if r[2] and r[2]['stream']][-1]['messages']
                assert 'Summary of the earlier conversation:' in last_prompt[0]['content']
                assert not any('Compaction message 1' in m['content'] for m in last_prompt)
                # The primary refuses with 503: the reply comes from the second fake server and says so.
                from roost.servers import Servers
                spare = Servers(state, environ={})
                spare.update('fallback', {'base_url': backup.url})
                spare.set_models('fallback', {'models': [{'id': 'backup-model', 'display_name': 'Backup', 'enabled': True,
                                                          'thinking_mode': 'unsupported'}], 'default_model': 'backup-model'})
                backup.frames = frame({'content': 'Answered by the fallback.'}) + frame({}, finish='stop', usage={'prompt_tokens': 9, 'completion_tokens': 4})
                assert (await request('PATCH', '/api/settings', {'use_fallback': True}))['use_fallback']
                fake.status = 503
                spare_turn = await request('POST', '/api/conversations', {'text': 'Fallback please', 'request_id': str(uuid4())}, 201)
                await settled(spare_turn['turn_id'])
                fake.status = 200
                reply = (await request('GET', '/api/conversations/' + spare_turn['session_id']))['messages'][-1]
                assert reply['content'] == 'Answered by the fallback.', reply
                assert reply['served_by'] == {'server': 'fallback', 'model': 'backup-model', 'display_name': 'Backup'}
                assert reply['trace'][-1]['name'] == 'fallback' and reply['trace'][-1]['status'] == 'completed'
                await request('DELETE', '/api/conversations/' + spare_turn['session_id'], {'confirm': True})
                await request('DELETE', path, {'confirm': True})
                assert [c['id'] for c in (await request('GET', '/api/conversations'))['sessions']] == [long_chat]
                await request('DELETE', '/api/conversations', {'confirm': 'DELETE'})
                print('PASS: native send, committed SSE/reconnect snapshot, queue, cancel, Stop, send-now, edit, regenerate, title, search/escaping, history, model/context, persona, manual/model memory, memory snapshots/forget, web search/read/answer, forced search, compaction summary, citations, trace snapshots, fallback reply on a 503, native MP4/WebM transcription, speech/cache/ranges, No web, rename, delete, delete-all and export; no Hermes server')
        finally:
            await roost.close()
            await fake.close()
            await search.close()
            await backup.close()
            await speech.close()


if __name__ == '__main__':
    asyncio.run(run())
