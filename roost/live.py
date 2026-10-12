"""Replies as they are written. Roost follows each turn it submits on Hermes's socket, and browsers
showing that conversation receive its thoughts and text as Server-Sent Events. The transcript stays
the record: a turn Roost did not submit, or one it stopped following, simply appears when saved."""

import asyncio
import json
import time

# A turn followed longer than this is left to the transcript; Hermes finishes it either way.
TURN_SECONDS = 1800
# Characters kept per turn for thoughts and for text; the transcript has the rest.
TEXT_LIMIT = 512 * 1024
# A comment line this often lets a browser (and any proxy) see that a quiet stream is still open.
HEARTBEAT_SECONDS = 15


class Turn:
    def __init__(self, user: str):
        self.user = user
        self.thoughts = ""
        self.text = ""
        self.done = False
        self.started = time.time()
        self.changed = asyncio.Event()
        # A tool call ends a step; what the next step writes starts on a new paragraph.
        self.new_step = set()

    def add(self, field: str, chunk) -> None:
        if not isinstance(chunk, str) or not chunk:
            return
        current = getattr(self, field)
        if field in self.new_step and current:
            chunk = "\n\n" + chunk.lstrip("\n")
        self.new_step.discard(field)
        if len(current) + len(chunk) > TEXT_LIMIT:
            return
        setattr(self, field, current + chunk)
        self.notify()

    def notify(self) -> None:
        self.changed.set()
        self.changed = asyncio.Event()


def follow(live: dict, sid: str, user: str, events) -> asyncio.Task:
    """Show a turn as live under `sid` now, and record its events until it completes, the socket
    closes or time runs out."""
    turn = Turn(user)
    live[sid] = turn
    # Held by the turn, which `live` holds until the task ends, so the task can't be collected early.
    turn.task = asyncio.create_task(record(live, sid, turn, events))
    return turn.task


async def record(live: dict, sid: str, turn: Turn, events) -> None:
    try:
        async with asyncio.timeout(TURN_SECONDS):
            async for kind, payload in events:
                if kind == "reasoning.delta":
                    turn.add("thoughts", payload.get("text"))
                elif kind == "message.delta":
                    turn.add("text", payload.get("text"))
                elif kind == "tool.start":
                    turn.new_step = {"thoughts", "text"}
                elif kind == "message.complete":
                    break
    except Exception:  # a broken stream ends the live view; the transcript still has the reply
        pass
    finally:
        await events.aclose()
        turn.done = True
        turn.notify()
        if live.get(sid) is turn:
            del live[sid]


async def send(response, turn: Turn) -> None:
    """Write `turn` to an open event stream: everything so far, then what is added, then the end."""
    sent_thoughts = sent_text = 0
    first = True
    while True:
        changed = turn.changed
        data = {}
        if first:
            data = {"reset": True, "user": turn.user, "started": turn.started}
            first = False
        if len(turn.thoughts) > sent_thoughts:
            data["thoughts"] = turn.thoughts[sent_thoughts:]
            sent_thoughts = len(turn.thoughts)
        if len(turn.text) > sent_text:
            data["text"] = turn.text[sent_text:]
            sent_text = len(turn.text)
        if turn.done:
            data["done"] = True
        if data:
            await response.write(b"data: " + json.dumps(data).encode() + b"\n\n")
        if turn.done:
            return
        try:
            await asyncio.wait_for(changed.wait(), HEARTBEAT_SECONDS)
        except TimeoutError:
            await response.write(b": still writing\n\n")
