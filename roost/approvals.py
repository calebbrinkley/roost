"""Display-only views of Hermes-owned, exact-request approvals."""

import unicodedata


def text(value, limit=16000):
    if not isinstance(value, str):
        return ""
    return "".join(char for char in value if char in "\n\t" or
                   not unicodedata.category(char).startswith("C"))[:limit]


def view(payload, server_id=None):
    if not isinstance(payload, dict):
        return None
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id or len(request_id) > 256:
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return None
    return {"request_id": request_id, "command": text(payload.get("command")),
            "description": text(payload.get("description"), 2000),
            "tool_name": text(payload.get("tool_name"), 100),
            "choices": [choice for choice in ("once", "deny") if choice in choices],
            "server_id": server_id}


def snapshot(result):
    """Resume snapshots use Hermes' redacted payloads, never the raw approval queue."""
    pending = {}
    frames = result.get("open_requests")
    for frame in (frames[:32] if isinstance(frames, list) else []):
        if isinstance(frame, dict) and frame.get("method") == "approval":
            params = frame.get("params")
            if result.get("session_id") and (not isinstance(params, dict) or params.get("session_id") != result["session_id"]):
                continue
            if item := view(frame.get("params"), frame.get("id")):
                pending[item["request_id"]] = item
    if item := view(result.get("pending_approval")):
        pending.setdefault(item["request_id"], item)
    return list(pending.values())
