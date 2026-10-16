"""Whether the agent is working on a conversation: one rule for every device and for notifications.

A turn is one message from the owner and everything the agent does to answer it. Roost knows more
about turns it sent (it follows them live and remembers when each was sent) than about turns started
elsewhere, which only the transcript shows. Hermes and Roost share the host's clock, so a row's
timestamp says whether it was written after a message was sent.
"""

import time

# A transcript that ends mid-turn but has been quiet this long is treated as over.
QUIET_SECONDS = 600
ENDED = {"error", "agent_error", "content_filter"}


def message_text(row: dict) -> str:
    value = row.get("display_content") or row.get("content")
    if isinstance(value, list):
        value = " ".join(part if isinstance(part, str) else str((part or {}).get("text") or "") for part in value)
    return value if isinstance(value, str) else ""


def _written(row) -> float | None:
    try:
        return float(row.get("timestamp")) if isinstance(row, dict) else None
    except (TypeError, ValueError):
        return None


def mid_turn(rows: list, now: float) -> bool:
    """The newest row says a turn is still going: the owner's message or a tool result is waiting on
    the agent, or a reply calls tools and has more to come. An error, or ten quiet minutes, ends it."""
    last = rows[-1] if rows and isinstance(rows[-1], dict) else None
    written = _written(last)
    if last is None or last.get("finish_reason") in ENDED or written is None or now - written >= QUIET_SECONDS:
        return False
    return last.get("role") in {"user", "tool"} or (last.get("role") == "assistant" and bool(last.get("tool_calls")))


def finished_reply(rows: list, since: float) -> str | None:
    """The reply text once the turn has ended: the transcript ends with an assistant message
    written after `since` that has text and no tool calls. None while the agent is still working."""
    last = rows[-1] if rows and isinstance(rows[-1], dict) else None
    if last is None or last.get("role") != "assistant" or last.get("tool_calls"):
        return None
    text = message_text(last).strip()
    written = _written(last)
    return text if text and written is not None and written >= since else None


def working(rows: list, *, turn=None, since: float | None = None, now: float | None = None) -> bool:
    """Whether a turn has started and its final reply is not saved yet.

    `turn` is the live turn Roost is following, if any; `since` is when Roost sent the message it is
    waiting on, if any. Hermes may accept a message before it saves it, so nothing written since
    then means the turn has not even been saved yet."""
    now = time.time() if now is None else now
    if turn is not None and not turn.done:
        return True
    if since is not None and not any((written := _written(row)) is not None and written >= since for row in rows):
        return True
    return mid_turn(rows, now)


def after(rows: list, since: float) -> int:
    """The newest saved row id from before `since`: the turn Roost sent then is everything after it."""
    return max((row["id"] for row in rows if isinstance(row, dict) and isinstance(row.get("id"), int)
                and not isinstance(row.get("id"), bool) and (_written(row) or 0) < since), default=0)
