"""Roost platform for Hermes: job reports and messages the agent sends reach Roost, which keeps them and
notifies the owner's devices. Outbound only; conversations happen in Roost itself.

Ships with Roost (Settings › Notifications offers it as a download) and is copied into
``$HERMES_HOME/plugins/roost``. It never runs inside Roost. Hermes's ``.env`` sets:
ROOST_URL (Roost's address as Hermes reaches it), ROOST_DELIVERY_TOKEN (from Roost's Settings) and
ROOST_HOME_CHANNEL=roost, which lets jobs deliver to Roost (``deliver: roost``).
"""

import asyncio
import json
import logging
import math
import threading
import time
from collections import OrderedDict
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import extra_or_secret, get_scoped_secret, send_error
from gateway.platforms.base import BasePlatformAdapter, SendResult

logger = logging.getLogger(__name__)

# Roost keeps up to 48,000 characters of each delivery.
MAX_MESSAGE_LENGTH = 48000
TIMEOUT_SECONDS = 15


def _settings(extra: Optional[dict]) -> tuple[str, str]:
    extra = extra or {}
    url = str(extra_or_secret(extra, "url", "ROOST_URL") or "").strip().rstrip("/")
    token = str(extra_or_secret(extra, "token", "ROOST_DELIVERY_TOKEN") or "").strip()
    return url, token


def _post(url: str, token: str, body: dict) -> tuple[int, bytes]:
    request = urllib.request.Request(url + "/api/hermes/deliveries", data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read(65536)
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.read(65536)


async def _deliver(url: str, token: str, text: str, job_id: Optional[str]) -> Dict[str, Any]:
    """POST one delivery to Roost: {"success": True, "message_id": ...} or {"error": ...}."""
    if not (url and token):
        return send_error("Roost delivery: set ROOST_URL and ROOST_DELIVERY_TOKEN")
    body = {"text": text[:MAX_MESSAGE_LENGTH], **({"job_id": str(job_id)} if job_id else {})}
    try:
        status, answer = await asyncio.to_thread(_post, url, token, body)
    except Exception as exc:  # unreachable, refused, timed out: the type only, never the URL or token
        return send_error(f"Roost delivery failed: {type(exc).__name__}")
    if status >= 300:
        # Roost's error text is safe to show; it never echoes the token.
        return send_error(f"Roost answered HTTP {status}: {answer[:200].decode(errors='replace')}")
    try:
        message_id = str(json.loads(answer).get("id") or "")
    except Exception:
        message_id = ""
    return {"success": True, "platform": "roost", "message_id": message_id}


def check_requirements() -> bool:
    return all(_settings(None))


def validate_config(config) -> bool:
    return all(_settings(getattr(config, "extra", None)))


class RoostAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform("roost"))
        self._url, self._token = _settings(config.extra)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not (self._url and self._token):
            logger.warning("[%s] Set ROOST_URL and ROOST_DELIVERY_TOKEN to deliver to Roost", self.name)
            return False
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        result = await _deliver(self._url, self._token, content, (metadata or {}).get("job_id"))
        if not result.get("success"):
            return SendResult(success=False, error=str(result.get("error")))
        return SendResult(success=True, message_id=result.get("message_id") or None)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": "Roost", "type": "dm"}


async def _standalone_send(pconfig, chat_id: str, message: str, *, thread_id: Optional[str] = None,
                           media_files: Optional[List[str]] = None, force_document: bool = False) -> Dict[str, Any]:
    """Cron delivery when no gateway adapter is running. Job reports carry their job id in Hermes's
    header line, which Roost reads; media files are not sent."""
    return await _deliver(*_settings(getattr(pconfig, "extra", None)), message, None)


def _env_enablement() -> dict | None:
    url = (get_scoped_secret("ROOST_URL", "") or "").strip()
    if not url:
        return None
    home = (get_scoped_secret("ROOST_HOME_CHANNEL", "") or "").strip() or "roost"
    return {"url": url.rstrip("/"), "home_channel": {"chat_id": home, "name": "Roost"}}


# Hooks run in concurrent Hermes turn threads. Keep only numeric measurements, never prompts or replies.
_usage_lock = threading.Lock()
_turn_usage = OrderedDict()
_usage_uploads = threading.BoundedSemaphore(2)


def _token_count(value):
    return value if type(value) is int and 0 <= value <= 2 ** 53 - 1 else None


def _model_seconds(value):
    return value if type(value) in (int, float) and 0 < value < 1e9 and math.isfinite(value) else None


def _record_usage(*, turn_id=None, usage=None, api_duration=None, started_at=None, api_call_count=None, **_):
    if not turn_id:
        return
    usage = usage if isinstance(usage, dict) else {}
    values = {"input_tokens": _token_count(usage.get("prompt_tokens")),
              "output_tokens": _token_count(usage.get("output_tokens")),
              "model_seconds": _model_seconds(api_duration)}
    with _usage_lock:
        now = time.monotonic()
        for key, old in list(_turn_usage.items()):
            if now - old["created"] > 3600:
                del _turn_usage[key]
        current = _turn_usage.get(turn_id)
        # Eviction, a missed callback or a plugin enabled mid-turn must not look like a whole reply.
        expected = current["calls"] + 1 if current and type(current["calls"]) is int else 1
        if type(api_call_count) is not int or api_call_count != expected:
            values = {key: None for key in values}
        if current is None:
            current = {"created": now, "started_at": started_at, "calls": api_call_count, **values}
            _turn_usage[turn_id] = current
        else:
            for key, value in values.items():
                current[key] = current[key] + value if current[key] is not None and value is not None else None
        current["calls"] = api_call_count
        while len(_turn_usage) > 128:
            _turn_usage.popitem(last=False)


def _upload_usage(url, token, body):
    try:
        request = urllib.request.Request(url + "/api/hermes/reply-usage", data=json.dumps(body).encode(), method="POST",
                                         headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=3) as response:
            response.read(4096)
    except Exception:  # telemetry must never fail a reply or print its configuration
        logger.warning("Roost reply measurements could not be delivered.")
    finally:
        _usage_uploads.release()


def _finish_usage(*, session_id=None, turn_id=None, conversation_history=None, **_):
    with _usage_lock:
        measured = _turn_usage.pop(turn_id, None)
    history = conversation_history if isinstance(conversation_history, list) else []
    last = history[-1] if history and isinstance(history[-1], dict) else {}
    row_id, timestamp = last.get("_row_id"), last.get("timestamp")
    started = measured.get("started_at") if measured else None
    # Only the structurally final, durably identified row created during THIS turn. Never match by text.
    if (not measured or not session_id or last.get("role") != "assistant" or last.get("tool_calls")
            or type(row_id) is not int or row_id <= 0 or type(timestamp) not in (int, float) or not math.isfinite(timestamp)
            or type(started) not in (int, float) or not math.isfinite(started) or timestamp < started):
        return
    body = {"session_id": session_id, "message_id": row_id,
            **{key: measured[key] for key in ("input_tokens", "output_tokens", "model_seconds")}}
    url, token = _settings(None)
    if url and token and _usage_uploads.acquire(blocking=False):
        # Bounded background uploads: a slow/offline Roost must not delay Hermes's completed reply.
        try:
            threading.Thread(target=_upload_usage, args=(url, token, body), daemon=True).start()
        except Exception:
            _usage_uploads.release()


def _discard_usage(*, turn_id=None, **_):
    with _usage_lock:
        _turn_usage.pop(turn_id, None)


def register(ctx) -> None:
    ctx.register_hook("post_api_request", _record_usage)
    ctx.register_hook("post_llm_call", _finish_usage)
    ctx.register_hook("on_session_end", _discard_usage)
    ctx.register_platform(
        name="roost", label="Roost", adapter_factory=lambda cfg: RoostAdapter(cfg),
        check_fn=check_requirements, validate_config=validate_config, is_connected=validate_config,
        required_env=["ROOST_URL", "ROOST_DELIVERY_TOKEN"], install_hint="Set ROOST_URL and ROOST_DELIVERY_TOKEN",
        env_enablement_fn=_env_enablement, cron_deliver_env_var="ROOST_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send, max_message_length=MAX_MESSAGE_LENGTH, pii_safe=True,
        allow_update_command=False,
        platform_hint=("Messages you send here reach the owner in Roost, the app they chat with you in, "
                       "as a notification. Markdown renders there."))
