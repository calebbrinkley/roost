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
                exported = await request('GET', path + '/export')
                assert exported['messages'][0]['content'] == 'Edited searchable message'
                await request('PATCH', path, {'title': 'My title'})
                assert (await request('GET', '/api/conversations'))['sessions'][0]['title'] == 'My title'
                assert (await request('GET', '/api/jobs')) == {'jobs': []}
                assert (await request('GET', '/api/settings/soul')) == {'content': ''}
                await request('DELETE', path, {'confirm': True})
                assert not (await request('GET', '/api/conversations'))['sessions']
                await request('DELETE', '/api/conversations', {'confirm': 'DELETE'})
                print('PASS: native send, committed SSE/reconnect snapshot, queue, cancel, Stop, send-now, edit, regenerate, title, search/escaping, history, model/context, rename, delete, delete-all and export; no Hermes server')
        finally:
            await roost.close()
            await fake.close()


if __name__ == '__main__':
    asyncio.run(run())
