"""Application-owned native turns. HTTP streams observe committed state; they never own work."""

import asyncio
import time
from contextlib import suppress

from .provider import Provider, ProviderError
from .store import StoreError


class Runner:
    def __init__(self, store, servers, session, *, completed=None, limit=1):
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError("Generation admission limit must be 1 to 16.")
        self.store, self.servers, self.session = store, servers, session
        self.completed, self.limit = completed, limit
        self.active, self.titles, self.title_attempted = {}, [], set()
        self.completions = set()
        self.changed, self.wake, self.lock = {}, asyncio.Event(), asyncio.Lock()
        self.closing = False

    def start(self):
        self.store.recover()
        self.dispatcher = asyncio.create_task(self.dispatch())
        self.wake.set()

    def event(self, cid):
        return self.changed.setdefault(cid, asyncio.Event())

    def notify(self, cid):
        self.event(cid).set()
        self.changed[cid] = asyncio.Event()
        self.wake.set()

    def selected(self, model=None):
        server = self.servers.get('primary')
        model = model or server['default_model']
        for item in server['models']:
            if item['enabled'] and item['id'] == model and server['base_url']:
                return server, item
        raise StoreError("Choose an enabled model on the primary server in Settings.")

    def snapshot(self, model=None):
        server, item = self.selected(model)
        return item['id'], {'thinking_mode': item['thinking_mode'], 'context_tokens': item.get('context_tokens')}

    def client(self):
        server = self.servers.get('primary')
        return Provider(self.session, server['base_url'], self.servers.key('primary'))

    async def dispatch(self):
        while not self.closing:
            await self.wake.wait()
            self.wake.clear()
            async with self.lock:
                while not self.closing and len(self.active) < self.limit:
                    queued = self.store.queued()
                    # Sorting by ordinal is necessary after Send now changes execution order.
                    heads = {}
                    for turn in queued:
                        if turn.conversation_id not in heads or turn.ordinal < heads[turn.conversation_id].ordinal:
                            heads[turn.conversation_id] = turn
                    ready = sorted((t for cid, t in heads.items() if cid not in self.active), key=lambda t: (t.created_at, t.id))
                    if ready:
                        turn = ready[0]
                        self.store.set_turn_state(turn.id, 'running')
                        self.notify(turn.conversation_id)
                        self.launch(turn.conversation_id, turn.id, self.generate(turn.id))
                    else:
                        title = next((item for item in self.titles if item[0] not in self.active), None)
                        if title is None:
                            break
                        self.titles.remove(title)
                        self.launch(title[0], None, self.title(*title))

    def launch(self, cid, tid, coroutine):
        task = asyncio.create_task(coroutine)
        self.active[cid] = (tid, task)
        def finished(done):
            if self.active.get(cid, (None, None))[1] is done:
                self.active.pop(cid, None)
            # Retrieve exceptions even when shutdown or a control already awaited this task.
            if not done.cancelled():
                done.exception()
            self.notify(cid)
        task.add_done_callback(finished)

    def prompt(self, turn):
        messages = []
        if turn.settings['instructions']:
            messages.append({'role': 'system', 'content': turn.settings['instructions']})
        for prior in self.store.list_turns(turn.conversation_id):
            if prior.ordinal >= turn.ordinal:
                break
            if prior.state in ('completed', 'interrupted'):
                messages.extend({'role': row.role, 'content': row.content} for row in self.messages(prior.id)
                                if row.role != 'assistant' or row.content)
        user = self.messages(turn.id)[0]
        messages.append({'role': 'user', 'content': user.content})
        return messages

    def messages(self, tid):
        return self.store.messages_for_turn(tid)

    async def generate(self, tid):
        turn = self.store.get_turn(tid)
        text = reasoning = ''
        usage, first = {}, None
        began = checkpoint = time.monotonic()
        saved_size = 0
        def save():
            nonlocal checkpoint, saved_size
            current = self.store.get_turn(tid)
            if current and current.state == 'running' and len(text) + len(reasoning) != saved_size:
                self.store.checkpoint_assistant(tid, text, reasoning)
                checkpoint, saved_size = time.monotonic(), len(text) + len(reasoning)
                self.notify(turn.conversation_id)
        async def flush():
            while True:
                await asyncio.sleep(.25)
                save()
        flusher = asyncio.create_task(flush())
        try:
            async for event in self.client().stream(turn.model, self.prompt(turn), thinking=turn.settings['thinking'],
                                                    mode=turn.settings.get('thinking_mode', 'unsupported')):
                if event.kind in ('text', 'reasoning'):
                    if first is None and event.text:
                        first = time.monotonic()
                    if event.kind == 'text':
                        text += event.text
                    else:
                        reasoning += event.text
                    now = time.monotonic()
                    if now - checkpoint >= .25 or len(text) + len(reasoning) - saved_size >= 1024:
                        save()
                elif event.kind == 'usage':
                    usage = {key: value for key, value in (event.usage or {}).items()
                             if key in ('prompt_tokens', 'completion_tokens', 'total_tokens') and type(value) is int and value >= 0}
                elif event.kind == 'tool_call':
                    raise ProviderError("This phase does not support model tool calls.")
            self.store.checkpoint_assistant(tid, text, reasoning)
            ended = time.monotonic()
            duration = ended - began
            generation = ended - first if first is not None else 0
            count = usage.get('completion_tokens')
            self.store.finish(tid, {'usage': usage, 'model_seconds': duration,
                                   'time_to_first_token': first - began if first is not None else None,
                                   'tokens_per_second': count / generation if type(count) is int and generation > 0 else None})
            self.notify(turn.conversation_id)
            ordered = self.store.list_turns(turn.conversation_id)
            first_reply = next((t for t in ordered if t.state == 'completed'), None)
            if first_reply and first_reply.id == tid and turn.conversation_id not in self.title_attempted:
                self.title_attempted.add(turn.conversation_id)
                self.titles.append((turn.conversation_id, tid))
            if self.completed:
                task = asyncio.create_task(self.complete(turn.conversation_id, tid, text))
                self.completions.add(task)
                task.add_done_callback(self.completions.discard)
        except asyncio.CancelledError:
            current = self.store.get_turn(tid)
            if current and current.state == 'running':
                self.store.checkpoint_assistant(tid, text, reasoning)
                self.store.set_turn_state(tid, 'interrupted')
                self.notify(turn.conversation_id)
            raise
        except Exception as exc:
            current = self.store.get_turn(tid)
            if current and current.state == 'running':
                self.store.checkpoint_assistant(tid, text, reasoning)
                self.store.set_turn_state(tid, 'failed', error=str(exc) if isinstance(exc, ProviderError) else 'Reply could not be completed.')
                self.notify(turn.conversation_id)

        finally:
            flusher.cancel()
            with suppress(asyncio.CancelledError):
                await flusher

    async def complete(self, cid, tid, text):
        with suppress(Exception):
            await self.completed(cid, tid, text)

    async def title(self, cid, tid):
        conversation, turn = self.store.get_conversation(cid), self.store.get_turn(tid)
        if not conversation or not turn or conversation.title_source != 'auto':
            return
        try:
            rows = self.messages(tid)
            result = await self.client().complete(turn.model, [
                {'role': 'system', 'content': 'Write only a short chat title, one line, at most 60 characters.'},
                {'role': 'user', 'content': rows[0].content[:2000] + '\n' + rows[1].content[:2000]}],
                thinking='off', mode=turn.settings.get('thinking_mode', 'unsupported'), max_tokens=32)
            cleaned = result.content.strip().splitlines()
            title = cleaned[0].strip(' #*`"\'')[:60] if cleaned else ''
            title = ''.join(char for char in title if char.isprintable())
            if title and self.store.get_turn(tid) and self.store.get_conversation(cid):
                self.store.update_conversation(cid, {'title': title}, auto_title=True)
                self.notify(cid)
        except (ProviderError, StoreError):
            pass  # The first user line remains a useful title when inference is unavailable.

    async def interrupt(self, cid):
        active = self.active.get(cid)
        if active:
            active[1].cancel()
            with suppress(asyncio.CancelledError):
                await active[1]
            turn = self.store.get_turn(active[0]) if active[0] else None
            if turn and turn.state == 'running':
                self.store.set_turn_state(turn.id, 'interrupted')
            if self.active.get(cid) == active:
                self.active.pop(cid, None)

    async def control(self, cid, action, tid=None, **kwargs):
        async with self.lock:
            if action == 'edit':
                accepted = self.store.request(kwargs['request_id'], kwargs['identity'])
                if accepted is not None:
                    return accepted
            turn = self.store.get_turn(tid) if tid else None
            if tid and (turn is None or turn.conversation_id != cid):
                raise StoreError("Turn does not belong to this conversation.")
            if action == 'stop':
                # A title call is not a running reply.
                if turn.state == 'running':
                    await self.interrupt(cid)
                elif turn.state == 'queued':
                    raise StoreError("Only a running turn can be stopped.")
            elif action == 'cancel':
                self.store.cancel_queued(tid)
            elif action == 'send-now':
                if turn.state != 'queued':
                    raise StoreError("Only a queued turn can be sent now.")
                await self.interrupt(cid)
                self.store.send_now(tid)
            elif action == 'edit':
                if turn.state not in ('completed', 'failed', 'interrupted'):
                    raise StoreError("Only settled messages can be edited.")
                model, snapshot = self.snapshot(self.store.get_conversation(cid).model)
                await self.interrupt(cid)
                result = self.store.edit_resend(tid, model=model, snapshot=snapshot, **kwargs)
                if turn.ordinal == 1:
                    self.title_attempted.discard(cid)
                    self.titles = [item for item in self.titles if item[0] != cid]
                    self.store.update_conversation(cid, {'title': kwargs['text'].splitlines()[0][:60]}, auto_title=True)
                self.notify(cid)
                return result
            elif action == 'regenerate':
                self.store.regenerate(tid)
            elif action == 'delete':
                await self.interrupt(cid)
                self.store.delete_conversation(cid)
                self.titles = [item for item in self.titles if item[0] != cid]
            self.notify(cid)

    async def close(self):
        self.closing = True
        self.dispatcher.cancel()
        with suppress(asyncio.CancelledError):
            await self.dispatcher
        for cid in list(self.active):
            await self.interrupt(cid)
        completions = list(self.completions)
        for task in completions:
            task.cancel()
        await asyncio.gather(*completions, return_exceptions=True)
