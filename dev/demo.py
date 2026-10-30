"""Loopback-only demo: disposable Roost state and deterministic model, search and speech servers."""

import asyncio
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from uuid import uuid4

from aiohttp.test_utils import TestServer

from roost.auth import make_password_hash
from roost.server import create_app
from smoke import configure, OpenAIFake, SpeechFake, WebFake, frame

DEMO_PASSWORD = 'roost-demo-only'


async def run():
    with tempfile.TemporaryDirectory(prefix='roost-demo-') as directory:
        state = Path(directory)
        (state / 'password.hash').write_text(make_password_hash(DEMO_PASSWORD))
        fake = await OpenAIFake().start()
        search = await WebFake().start()
        speech = await SpeechFake().start()
        fake.frames = frame({'reasoning_content': 'A small plan leaves room for a quiet afternoon.'}) + frame({
            'content': 'Keep it simple:\n\n- Take a short walk.\n- Make time to sketch.\n- Leave the afternoon open.'}) + frame({}, finish='stop', usage={'prompt_tokens': 120, 'completion_tokens': 35})
        fake.completion['choices'][0]['message']['content'] = 'A quieter weekend'
        configure(state, fake.url)
        with patch.dict(os.environ, {'ROOST_STATE_DIR': str(state), 'ROOST_PUBLIC_ORIGIN': 'http://localhost', 'ROOST_DEMO': '1'}):
            app = create_app()
        app['voice_settings'].update({'same_as_model': False, 'base_url': speech.url})
        from roost import settings
        settings.update(state / 'settings.json', {'search_url': search.url})
        server = TestServer(app, port=int(os.environ.get('ROOST_DEMO_PORT', '8787')))
        try:
            with patch('roost.runner.WebTools', side_effect=search.tools):
                await server.start_server()
                app['origin'] = str(server.make_url('')).rstrip('/')
                store = app['store']
                guide = store.save_persona('Guide', 'Help with practical plans. Speak plainly in short paragraphs.')
                store.set_default_persona(guide['id'])
                store.save_memory('The user enjoys quiet weekends and sketching.')
                model, snapshot = app['runner'].snapshot(None)
                accepted = store.submit(str(uuid4()), 'Help me plan a quieter weekend.', conversation_id=None, model=model, changes={}, snapshot=snapshot, identity={'demo': True})
                app['runner'].notify(accepted.conversation_id)
                async with asyncio.timeout(10):
                    while store.get_turn(accepted.turn_id).state in ('queued', 'running'):
                        await asyncio.sleep(.01)
                store.update_conversation(accepted.conversation_id, {'title': 'A quieter weekend', 'title_source': 'user'})
                app['jobs'].save({'name': 'Weekly ideas', 'prompt': 'Suggest a small creative project.',
                                  'schedule': {'kind': 'cron', 'expr': '0 8 * * 0'}, 'enabled': False})
                print(json.dumps({'url': app['origin'], 'conversation': accepted.conversation_id}), flush=True)
                print('Demo password: ' + DEMO_PASSWORD, flush=True)
                await asyncio.Event().wait()
        finally:
            await server.close()
            await fake.close()
            await search.close()
            await speech.close()


if __name__ == '__main__':
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
