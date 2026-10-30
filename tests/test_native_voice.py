"""Native speech uses real HTTP with a deterministic speech provider, never Hermes."""

import asyncio
import base64
import unittest
from unittest.mock import patch

from speech_fake import SpeechFake

import test_native
from test_native import wait_for


class NativeVoiceTests(unittest.IsolatedAsyncioTestCase):
    request = test_native.NativeTests.request
    create = test_native.NativeTests.create
    async def asyncSetUp(self):
        await test_native.NativeTests.asyncSetUp(self)
        self.speech_fake = await SpeechFake().start()
        self.app['servers'].update('primary', {'base_url': self.speech_fake.url, 'key': 'example-primary-key'})

    async def asyncTearDown(self):
        await self.speech_fake.close()
        await test_native.NativeTests.asyncTearDown(self)

    async def recording(self, mime='audio/mp4', data=b'clip', **extra):
        return await self.client.post('/api/audio/transcribe', headers=self.headers, json={
            'data_url': 'data:' + mime + ';base64,' + base64.b64encode(data).decode(), 'mime_type': mime, **extra})

    async def test_native_transcription_mapping(self):
        for mime, extension in [('audio/mp4;codecs=mp4a.40.2', 'mp4'), ('audio/webm;codecs=opus', 'webm'), ('audio/aac', 'aac')]:
            self.speech_fake.plain = extension == 'webm'
            response = await self.recording(mime)
            self.assertEqual(await response.json(), {'transcript': 'Hello'})
            key, fields = self.speech_fake.calls[-1]
            self.assertEqual(key, 'Bearer example-primary-key')
            self.assertEqual(fields['model'][2], b'whisper-1')
            self.assertEqual(fields['file'], ('recording.' + extension, mime.split(';')[0], b'clip'))
        response = await self.recording(data=b'clip' * 50000)
        self.assertEqual(response.status, 200, 'native upload accepts recordings beyond the global JSON limit')
        self.app['hermes'].transcribe.assert_not_awaited()

    async def test_native_errors_and_limits(self):
        for status in (429, 503, 401):
            self.speech_fake.status = status
            response = await self.recording()
            body = await response.json()
            self.assertEqual(response.status, 503 if status in (429, 503) else 502)
            self.assertEqual(body['error'], 'Speech is busy right now. Try again in a moment.' if status != 401 else 'Speech server returned HTTP 401.')
        response = await self.client.post('/api/settings/voice/test', headers=self.headers, json={})
        self.assertEqual((response.status, await response.json()), (502, {'error': 'Speech server returned HTTP 401.'}))
        response = await self.recording(duration_seconds=121)
        self.assertEqual(response.status, 400)
        with patch('roost.speech.MAX_RECORDING', 3):
            response = await self.recording(data=b'large')
            self.assertEqual(response.status, 413)
        self.speech_fake.status = 200
        with patch('roost.speech.MAX_TRANSCRIPT', 3):
            self.assertEqual((await self.recording()).status, 502)

    async def test_settings_keys_validation_and_test(self):
        url = '/api/settings/voice'
        saved = await self.request('GET', url)
        self.assertTrue(saved['same_as_model'])
        saved = await self.request('PUT', url, {'same_as_model': False, 'base_url': self.speech_fake.url,
                                              'key': 'example-speech-key', 'voice': 'nova'})
        self.assertTrue(saved['has_key'])
        from roost.speech import VoiceSettings
        self.assertEqual(VoiceSettings(self.state).public(), saved)
        for name in ('voice.json', 'speech-keys.json'):
            self.assertEqual((self.state / name).stat().st_mode & 0o777, 0o600)
        self.assertNotIn('key', saved)
        self.assertNotIn('example-speech-key', str(await self.request('GET', url)))
        response = await self.recording()
        self.assertEqual(response.status, 200)
        self.assertEqual(self.speech_fake.calls[-1][0], 'Bearer example-speech-key')
        response = await self.client.post(url + '/test', headers=self.headers, json={})
        self.assertEqual((response.status, await response.read()), (200, self.speech_fake.audio))
        self.assertEqual(self.speech_fake.calls[-1][1]['voice'], 'nova')
        for change in ({'same_as_model': 1}, {'base_url': 'https://user:pass@example.invalid'}, {'voice': ''},
                       {'transcription_model': []}, {'key': 'bad\nkey'}, {'unknown': True}):
            await self.request('PUT', url, change, 400)
        await self.request('PUT', url, {'clear_key': True})
        self.assertFalse((await self.request('GET', url))['has_key'])
        await self.request('PUT', url, {'same_as_model': True})
        await self.recording()
        self.assertEqual(self.speech_fake.calls[-1][0], 'Bearer example-primary-key')

    async def test_native_synthesis_cache_ranges_and_cleanup(self):
        # Use the model fake for a committed assistant row, then switch the speech target.
        self.app['servers'].update('primary', {'base_url': self.fake.url})
        sent = await self.create()
        await wait_for(lambda: self.store.get_turn(sent['turn_id']).state == 'completed')
        self.store.db.execute('UPDATE messages SET content=? WHERE id=?', ('**Hello** [3] [world](https://example.invalid). ```code```', sent['assistant_message_id']))
        self.app['servers'].update('primary', {'base_url': self.speech_fake.url})
        url = f"/api/conversations/{sent['session_id']}/speech/{sent['assistant_message_id']}"
        first, second = await asyncio.gather(self.client.get(url), self.client.get(url, headers={'Range': 'bytes=0-2'}))
        self.assertEqual((first.status, await first.read()), (200, self.speech_fake.audio))
        self.assertEqual((second.status, await second.read()), (206, self.speech_fake.audio[:3]))
        self.assertEqual(second.headers['Content-Range'], f'bytes 0-2/{len(self.speech_fake.audio)}')
        self.assertEqual((await self.client.get(url, headers={'Range': 'bytes=999-'})).status, 416)
        self.assertEqual(len(self.speech_fake.calls), 1)
        self.assertEqual(self.speech_fake.calls[0][1], {'model': 'tts-1', 'input': 'Hello world.', 'voice': 'alloy', 'response_format': 'mp3'})
        self.app['hermes'].speak.assert_not_awaited()
        with patch('roost.speech.MAX_AUDIO', 3):
            response = await self.client.post('/api/settings/voice/test', headers=self.headers, json={})
            self.assertEqual(response.status, 502)

    async def test_malformed_recordings_transcripts_and_native_only_settings(self):
        for body in ({'data_url': 'data:audio/mp4;base64,???', 'mime_type': 'audio/mp4'},
                     {'data_url': 'data:audio/mp4extra;base64,Y2xpcA==', 'mime_type': 'audio/mp4'},
                     {'data_url': 'data:audio/mp4;base64,', 'mime_type': 'audio/mp4'}):
            response = await self.client.post('/api/audio/transcribe', headers=self.headers, json=body)
            self.assertEqual(response.status, 400)
        self.speech_fake.transcript = {'unexpected': 'shape'}
        self.assertEqual((await self.recording()).status, 502)
        for code in (429, 503):
            self.speech_fake.status = code
            response = await self.client.post('/api/settings/voice/test', headers=self.headers, json={})
            self.assertEqual((response.status, (await response.json())['error']),
                             (503, 'Speech is busy right now. Try again in a moment.'))
        await self.request('PATCH', '/api/settings', {'backend': 'hermes'})
        await self.request('GET', '/api/settings/voice', status=404)

    async def test_transport_timeouts_are_public_safe(self):
        import aiohttp
        for error, status in ((asyncio.TimeoutError('synthetic private detail'), 504),
                              (aiohttp.ClientConnectionError('synthetic private detail'), 502)):
            with patch.object(self.app['provider_http'], 'post', side_effect=error):
                response = await self.recording()
            self.assertEqual(response.status, status)
            self.assertNotIn('private', str(await response.json()))
