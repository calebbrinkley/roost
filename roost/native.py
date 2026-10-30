"""Native implementations of the browser's existing conversation contracts."""

import asyncio
import json
import re
import time
from uuid import uuid4

from aiohttp import web

from . import server as http


def enabled(app):
    return 'settings_file' in app and http.settings.load(app['settings_file'])['backend'] == 'native'


def chat(request):
    cid = http.item_id(request, 'sid')
    result = request.app['store'].get_conversation(cid)
    if result is None:
        raise web.HTTPNotFound(text='Conversation does not exist.')
    return result


def row(app, message):
    turn = app['store'].get_turn(message.turn_id)
    stats, usage = turn.stats, turn.stats.get('usage', {})
    return {'id': message.id, 'role': message.role, 'content': message.content, 'reasoning': message.reasoning,
            'timestamp': message.created_at, 'turn_id': turn.id, 'ordinal': turn.ordinal, 'revision': turn.revision,
            'state': turn.state, 'client_request_id': turn.request_id, 'error': turn.error,
            **({'roost_usage': {'input_tokens': usage.get('prompt_tokens'), 'output_tokens': usage.get('completion_tokens'),
                                'model_seconds': stats.get('model_seconds'),
                                'time_to_first_token': stats.get('time_to_first_token'),
                                'tokens_per_second': stats.get('tokens_per_second')}, 'stats': stats,
                'trace': app['store'].trace(turn.id), 'sources': app['store'].sources(turn.id)}
               if message.role == 'assistant' else {})}


def listing(app, conversation):
    store = app['store']
    last = store.history(conversation.id, limit=1)[0]
    return {'id': conversation.id, 'title': conversation.title, 'title_source': conversation.title_source,
            'preview': http.preview(last[0].content) if last else '', 'source': 'roost',
            'last_active_at': conversation.updated_at,
            'working': any(t.state in ('queued', 'running') for t in store.list_turns(conversation.id))}


async def conversations(request):
    offset = request.query.get('offset', '0')
    if not re.fullmatch(r'[0-9]{1,6}', offset) or int(offset) > 100000:
        raise http.bad('Invalid page offset.')
    store = request.app['store']
    return web.json_response({'sessions': [listing(request.app, c) for c in store.list_conversations(offset=int(offset))],
                              'total': store.count_conversations()})


async def conversation(request):
    c, store = chat(request), request.app['store']
    before, limit = request.query.get('before'), request.query.get('limit', '500')
    if (len(request.query.getall('before', [])) > 1 or len(request.query.getall('limit', [])) > 1
            or not re.fullmatch(r'[0-9]{1,3}', limit) or not 1 <= int(limit) <= 500
            or (before is None and 'limit' in request.query)
            or (before is not None and (not re.fullmatch(r'[0-9]{1,16}', before) or not 1 <= int(before) <= 9007199254740991))):
        raise http.bad('Invalid history page.')
    messages, older = store.history(c.id, before=int(before) if before else None, limit=int(limit))
    ordered = store.list_turns(c.id)
    running = next((t for t in ordered if t.state == 'running'), None)
    working = any(t.state in ('queued', 'running') for t in ordered)
    last = next((t for t in reversed(ordered) if t.stats), None)
    context_tokens = last.settings.get('context_tokens') if last else None
    result = {'session_id': c.id, 'messages': [row(request.app, m) for m in messages], 'has_older': older,
              'live': running is not None, 'working': working,
              'after': request.app['runner'].messages(running.id)[0].id - 1 if running else None,
              'started': running.created_at if running else None, 'turn_id': running.id if running else None,
              'revision': running.revision if running else None,
              'queue': [{'turn_id': t.id, 'position': position} for position, t in enumerate((t for t in ordered if t.state == 'queued'), 1)],
              'context': {'prompt_tokens': last.stats.get('usage', {}).get('prompt_tokens') if last else None,
                          'context_tokens': context_tokens}}
    return web.json_response(result)


def fields(data):
    changes = {}
    if 'instructions' in data:
        changes['instructions'] = http.body_text(data['instructions'], 'Instructions', 4000, required=False)
    if 'reasoning' in data:
        level = data['reasoning']
        if level not in ('default', 'none', 'off', 'low', 'medium', 'high'):
            raise http.bad('Unknown reasoning level.')
        changes['thinking'] = 'off' if level in ('default', 'none') else level
    if 'model' in data:
        if data['model'] is not None and (not isinstance(data['model'], str) or not data['model'] or len(data['model']) > 256):
            raise http.bad('Invalid model override.')
        changes['model'] = data['model']
    if 'no_web' in data:
        if type(data['no_web']) is not bool:
            raise http.bad('no_web must be a boolean.')
        changes['no_web'] = data['no_web']
    return changes


def send_fields(data):
    if set(data) - {'text', 'request_id', 'reasoning', 'instructions', 'model', 'web'}:
        raise http.bad('Unsupported send field.')
    if 'web' in data and data['web'] != 'force':
        raise http.bad('web must be force when provided.')
    text = http.body_text(data.get('text'), 'Message', 16000)
    request_id = data.get('request_id', str(uuid4()))
    if not isinstance(request_id, str) or not http.SEND_ID.fullmatch(request_id):
        raise http.bad('Invalid send request identifier.')
    return text, request_id, fields(data)


def acceptance(app, accepted):
    turn = app['store'].get_turn(accepted.turn_id)
    queued = [t.id for t in app['store'].list_turns(turn.conversation_id) if t.state == 'queued']
    return {'session_id': accepted.conversation_id, 'turn_id': accepted.turn_id,
            'message_id': accepted.message_id, 'assistant_message_id': accepted.assistant_message_id,
            'state': turn.state, 'revision': turn.revision,
            'queue_position': queued.index(turn.id) + 1 if turn.id in queued else None}


async def submit(request, *, new=False):
    data = await http.json_body(request)
    text, request_id, changes = send_fields(data)
    cid = None if new else chat(request).id
    store, runner = request.app['store'], request.app['runner']
    identity = {'conversation_id': cid, 'text': text, 'changes': changes, **({'web': data['web']} if 'web' in data else {})}
    accepted = store.request(request_id, identity)
    if accepted is None:
        if not new and changes:
            raise http.bad('Change conversation settings before sending a message.')
        if len(store.queued()) >= 500:
            raise web.HTTPTooManyRequests(text='The native turn queue is full.')
        override = changes.get('model') if new else store.get_conversation(cid).model
        model, snapshot = runner.snapshot(override, no_web=store.get_conversation(cid).no_web if cid else False, web=data.get('web'))
        accepted = store.submit(request_id, text, conversation_id=cid, model=model,
                                changes=changes, snapshot=snapshot, identity=identity)
        runner.notify(accepted.conversation_id)
    http.sent_from_here(request, accepted.conversation_id)
    return web.json_response(acceptance(request.app, accepted), status=201 if new else 200)


async def create_conversation(request):
    return await submit(request, new=True)


async def send_message(request):
    return await submit(request)


async def rename_conversation(request):
    c, data = chat(request), await http.json_body(request)
    if set(data) != {'title'}:
        raise http.bad('Send only a title.')
    title = http.body_text(data['title'], 'Title', 200, required=False)
    request.app['store'].update_conversation(c.id, {'title': title, 'title_source': 'user'})
    return web.json_response({'ok': True})


async def delete_conversation(request):
    c, data = chat(request), await http.json_body(request)
    if set(data) != {'confirm'} or data['confirm'] is not True:
        raise http.bad('Confirm conversation deletion.')
    await request.app['runner'].control(c.id, 'delete')
    return web.json_response({'ok': True})


async def delete_all_conversations(request):
    http.confirmed_everything(await http.json_body(request))
    ids = request.app['store'].conversation_ids()
    for cid in ids:
        await request.app['runner'].control(cid, 'delete')
    return web.json_response({'deleted': len(ids)})


def chat_settings(c):
    return {'instructions': c.instructions, 'reasoning': 'none' if c.thinking == 'off' else c.thinking, 'model': c.model, 'no_web': c.no_web}


async def get_chat_settings(request):
    return web.json_response(chat_settings(chat(request)))


async def update_chat_settings(request):
    c, data = chat(request), await http.json_body(request)
    if not data or set(data) - {'instructions', 'reasoning', 'model', 'no_web'}:
        raise http.bad('Only instructions, reasoning, model and no_web can be changed.')
    changes = fields(data)
    if changes.get('model') is not None:
        request.app['runner'].selected(changes['model'])
    request.app['store'].update_conversation(c.id, changes)
    return web.json_response(chat_settings(request.app['store'].get_conversation(c.id)))


async def export_conversation(request):
    c, store = chat(request), request.app['store']
    messages = [row(request.app, m) for t in store.list_turns(c.id) for m in request.app['runner'].messages(t.id)]
    body = {'conversation': c.id, 'exported': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            **chat_settings(c), 'messages': messages}
    return web.Response(body=json.dumps(body, ensure_ascii=False, indent=2).encode(), content_type='application/json',
                        headers={'Content-Disposition': f'attachment; filename="conversation-{c.id}.json"'})


async def stream(request):
    c, store, runner = chat(request), request.app['store'], request.app['runner']
    response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
    http.harden(request, response)
    task = asyncio.current_task()
    request.app['native_streams'].add(task)
    previous = None
    try:
        await response.prepare(request)
        while request.app['sessions'].valid(request.cookies.get(http.COOKIE, '')):
            changed = runner.event(c.id)
            ordered = store.list_turns(c.id)
            turn = next((t for t in ordered if t.state == 'running'), None)
            turn = turn or next((t for t in ordered if t.state == 'queued'), None) or (ordered[-1] if ordered else None)
            if turn is None:
                await response.write(b'data: {"done":true,"state":"deleted"}\n\n')
                break
            user, assistant = runner.messages(turn.id)
            marker = (turn.id, turn.revision)
            if marker != previous:
                # Each revision is a full committed snapshot, so reconnects and resets are identical.
                payload = {'reset': True, 'user': user.content, 'started': turn.created_at,
                           'thoughts': assistant.reasoning, 'text': assistant.content, 'approvals': [],
                           'turn_id': turn.id, 'revision': turn.revision, 'state': turn.state,
                           'done': turn.state not in ('running', 'queued'),
                           'trace': store.trace(turn.id), 'sources': store.sources(turn.id)}
                await response.write(b'data: ' + json.dumps(payload).encode() + b'\n\n')
                previous = marker
            if turn.state not in ('running', 'queued'):
                break
            try:
                await asyncio.wait_for(changed.wait(), http.live.HEARTBEAT_SECONDS)
            except TimeoutError:
                await response.write(b': still writing\n\n')
    except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
        pass
    finally:
        request.app['native_streams'].discard(task)
    return response


async def control(request):
    if not enabled(request.app):
        raise web.HTTPNotFound(text='Native controls require Native backend.')
    c, action = chat(request), request.match_info['action']
    if action not in ('stop', 'cancel', 'send-now', 'edit', 'regenerate'):
        raise web.HTTPNotFound(text='Unknown turn control.')
    data = await http.json_body(request)
    tid = data.get('turn_id')
    runner = request.app['runner']
    if action == 'stop':
        if set(data) != {'turn_id'} or not isinstance(tid, str) or not http.plain_id(tid):
            raise http.bad('Stop needs a turn_id.')
        await runner.control(c.id, action, tid)
    elif action == 'edit':
        if set(data) != {'turn_id', 'text', 'request_id'} or not isinstance(tid, str):
            raise http.bad('Edit needs turn_id, text and request_id.')
        text, request_id, _ = send_fields({key: value for key, value in data.items() if key != 'turn_id'})
        identity = {'edit': tid, 'conversation_id': c.id, 'text': text}
        accepted = await runner.control(c.id, 'edit', tid, request_id=request_id, text=text,
                                        identity=identity)
        http.sent_from_here(request, c.id)
        return web.json_response(acceptance(request.app, accepted))
    else:
        if set(data) != {'turn_id'} or not isinstance(tid, str) or not http.plain_id(tid):
            raise http.bad('Send a turn_id.')
        await runner.control(c.id, action, tid)
    return web.json_response({'ok': True})


async def models(request):
    primary = request.app['servers'].get('primary')
    return web.json_response({'models': [m for m in primary['models'] if m['enabled']], 'default_model': primary['default_model'],
                              'web_available': bool(http.settings.load(request.app['settings_file'])['search_url'])})


async def test_web_search(request):
    if not enabled(request.app):
        raise web.HTTPNotFound(text='Web search requires Native backend.')
    data = await http.json_body(request)
    if set(data) != {'query'} or not isinstance(data['query'], str) or not 0 < len(data['query'].strip()) <= 2000:
        raise http.bad('Provide a search query of 1 to 2000 characters.')
    base = http.settings.load(request.app['settings_file'])['search_url']
    if not base:
        raise http.bad('Configure a search URL first.')
    from .tools import WebTools
    result = await WebTools(base).execute('web_search', json.dumps(data))
    if 'error' in result:
        raise http.bad(result['error'])
    return web.json_response(result)


async def search(request):
    if not enabled(request.app):
        return web.json_response({'sessions': []})
    results = []
    for match in request.app['store'].search(request.query.get('q', '')):
        c = request.app['store'].get_conversation(match['conversation_id'])
        results.append({**listing(request.app, c), 'snippet': match['snippet']})
    return web.json_response({'sessions': results})


async def neutral(request):
    name = request.match_info.route.handler.__name__
    if request.path in http.PLUGIN_PATHS:
        raise web.HTTPNotFound(text='Hermes integration is disabled in Native backend.')
    if name == 'jobs':
        result = {'jobs': []}
    elif name == 'job_runs':
        result = {'runs': []}
    elif name == 'pending_approvals':
        result = {'approvals': []}
    elif name == 'respond_approval':
        result = {'resolved': False}
    elif name == 'get_soul':
        result = {'content': ''}
    elif name == 'hermes_link':
        result = {'token': '', 'url': '', 'last_delivery': None}
    elif name == 'get_delivery':
        result = {}
    elif name == 'delete_all_jobs':
        http.confirmed_everything(await http.json_body(request))
        result = {'deleted': 0}
    else:
        result = {'ok': False, 'disabled': True}
    return web.json_response(result)


HANDLERS = {handler.__name__: handler for handler in (
    conversations, conversation, create_conversation, send_message, rename_conversation,
    delete_conversation, delete_all_conversations, get_chat_settings, update_chat_settings,
    export_conversation, stream)}
HANDLERS.update({name: neutral for name in (
    'jobs', 'create_job', 'get_job', 'job_runs', 'update_job', 'job_action', 'delete_job', 'delete_all_jobs',
    'pending_approvals', 'respond_approval', 'get_soul', 'update_soul', 'restart_agent',
    'receive_delivery', 'receive_reply_usage', 'get_delivery', 'hermes_link', 'rotate_delivery_token', 'hermes_plugin',
    'transcribe', 'speech')})
