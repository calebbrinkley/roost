"""Bounded OpenAI-compatible speech transport and write-only native voice settings."""

import asyncio
import base64
import binascii
import json
import os
from uuid import uuid4

import aiohttp

from .provider import base_url
from .servers import ServerError, ServerStorageError, _key, _read, _save

MAX_RECORDING = 25 * 1024 * 1024
MAX_AUDIO = 25 * 1024 * 1024
MAX_TRANSCRIPT = 1024 * 1024
DEFAULTS = {'same_as_model': True, 'base_url': '', 'transcription_model': 'whisper-1',
            'speech_model': 'tts-1', 'voice': 'alloy'}


class SpeechError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


def validate(changes):
    if not changes or set(changes) - (set(DEFAULTS) | {'key', 'clear_key'}):
        raise ServerError('Unsupported voice setting.')
    result = dict(changes)
    for field in ('same_as_model', 'clear_key'):
        if field in changes and type(changes[field]) is not bool:
            raise ServerError(field + ' must be a boolean.')
    if 'base_url' in changes and changes['base_url'] != '':
        try:
            result['base_url'] = base_url(changes['base_url'])
        except ValueError:
            raise ServerError('Choose an HTTP(S) speech base URL without credentials, query or fragment.') from None
    for field in ('transcription_model', 'speech_model', 'voice'):
        if field in changes:
            value = changes[field]
            if not isinstance(value, str) or not value.strip() or len(value) > 256 or not value.isprintable():
                raise ServerError('Voice and model names must be 1 to 256 printable characters.')
            result[field] = value.strip()
    if 'key' in changes:
        result['key'] = _key(changes['key'])
    if result.get('key') and result.get('clear_key'):
        raise ServerError('Choose a new key or clear the old one, not both.')
    return result


class VoiceSettings:
    def __init__(self, state):
        self.path, self.keys_path = state / 'voice.json', state / 'speech-keys.json'

    def load(self):
        if not self.path.exists():
            return {**DEFAULTS, 'key_id': ''}, {}
        try:
            record, keys = _read(self.path), _read(self.keys_path)
            if set(record) != set(DEFAULTS) | {'key_id'}:
                raise ValueError
            validate({k: v for k, v in record.items() if k != 'key_id'})
            if not record['same_as_model'] and not record['base_url']:
                raise ValueError
            if not isinstance(record['key_id'], str) or (record['key_id'] and record['key_id'] not in keys):
                raise ValueError
            for value in keys.values():
                _key(value)
            os.chmod(self.path, 0o600)
            os.chmod(self.keys_path, 0o600)
            return record, keys
        except (ValueError, OSError):
            raise ServerStorageError('Cannot read speech configuration.') from None

    def public(self):
        record, keys = self.load()
        return {**{k: v for k, v in record.items() if k != 'key_id'}, 'has_key': bool(keys.get(record['key_id']))}

    def update(self, changes):
        changes = validate(changes)
        record, keys = self.load()
        record.update({k: v for k, v in changes.items() if k in DEFAULTS})
        if not record['same_as_model'] and not record['base_url']:
            raise ServerError('Set a custom speech base URL first.')
        if changes.get('clear_key'):
            record['key_id'] = ''
        elif changes.get('key'):
            record['key_id'] = uuid4().hex
            keys[record['key_id']] = changes['key']
        try:
            # Keys first: interrupted saves can leave an unused key, never a mismatched credential.
            _save(self.keys_path, keys)
            _save(self.path, record)
            _save(self.keys_path, {k: v for k, v in keys.items() if k == record['key_id']})
        except OSError:
            raise ServerStorageError('Cannot save speech configuration.') from None
        return self.public()

    def connection(self, servers):
        record, keys = self.load()
        if record['same_as_model']:
            return record, servers.get('primary')['base_url'], servers.key('primary')
        return record, record['base_url'], keys.get(record['key_id'], '')


def recording(data):
    mime, value = data.get('mime_type'), data.get('data_url')
    types = {'audio/mp4': 'mp4', 'audio/aac': 'aac', 'audio/webm': 'webm', 'video/webm': 'webm'}
    kind = mime.split(';', 1)[0] if isinstance(mime, str) else ''
    if kind not in types or not isinstance(value, str):
        raise SpeechError(400, 'Expected an MP4, AAC or WebM recording.')
    duration = data.get('duration_seconds', 0)
    if type(duration) not in (int, float) or not 0 <= duration <= 120:
        raise SpeechError(400, 'Recordings are limited to 120 seconds.')
    header, separator, encoded = value.partition(',')
    if (not separator or not header.startswith('data:') or header[5:].split(';', 1)[0] != kind
            or len(header) > 200 or not header.endswith(';base64')):
        raise SpeechError(400, 'Expected an audio recording.')
    if len(encoded) > ((MAX_RECORDING + 2) // 3) * 4:
        raise SpeechError(413, 'Recordings are limited to 25 MB.')
    try:
        clip = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise SpeechError(400, 'Expected an audio recording.') from None
    if len(clip) > MAX_RECORDING:
        raise SpeechError(413, 'Recordings are limited to 25 MB.')
    if not clip:
        raise SpeechError(400, 'No audio was recorded.')
    return clip, kind, 'recording.' + types[kind]


class Speech:
    def __init__(self, session, settings, servers):
        self.session, self.settings, self.servers = session, settings, servers

    async def request(self, path, limit, *, build):
        config, url, key = self.settings.connection(self.servers)
        if not url:
            raise SpeechError(400, 'Set a speech server in Settings › Voice or configure the model server.')
        try:
            async with self.session.post(url + '/audio/' + path, headers={'Authorization': 'Bearer ' + key} if key else {},
                                         timeout=aiohttp.ClientTimeout(total=150, connect=10, sock_read=60),
                                         allow_redirects=False, **build(config)) as response:
                if response.status in (429, 503):
                    raise SpeechError(503, 'Speech is busy right now. Try again in a moment.')
                if response.status != 200:
                    raise SpeechError(502, f'Speech server returned HTTP {response.status}.')
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > limit:
                        raise SpeechError(502, 'Speech server response is too large.')
                return bytes(body), response.content_type
        except asyncio.TimeoutError:
            raise SpeechError(504, 'Speech server timed out. Try again.') from None
        except aiohttp.ClientError:
            raise SpeechError(502, 'Could not reach the speech server.') from None

    async def transcribe(self, data):
        clip, mime, filename = recording(data)
        def build(config):
            form = aiohttp.FormData()
            form.add_field('file', clip, filename=filename, content_type=mime)
            form.add_field('model', config['transcription_model'])
            return {'data': form}
        body, mime = await self.request('transcriptions', MAX_TRANSCRIPT, build=build)
        try:
            text = body.decode('utf-8')
            text = text if mime == 'text/plain' else json.loads(text)['text']
            if not isinstance(text, str):
                raise ValueError
            return text
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise SpeechError(502, 'Speech server returned an invalid transcript.') from None

    async def speak(self, text):
        audio, _ = await self.request('speech', MAX_AUDIO, build=lambda config: {'json': {
            'model': config['speech_model'], 'input': text, 'voice': config['voice'], 'response_format': 'mp3'}})
        if not audio:
            raise SpeechError(502, 'Speech server returned no audio.')
        return 'audio/mpeg', audio
