"""OpenAI-compatible chat transport. The caller owns the HTTP session; requests are never retried."""

import asyncio
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal
from urllib.parse import urlsplit

import aiohttp
from yarl import URL

ThinkingMode = Literal["reasoning_effort", "template_flag", "unsupported"]
THINKING_MODES = ("reasoning_effort", "template_flag", "unsupported")
THINKING_LEVELS = ("off", "low", "medium", "high")
MAX_LINE = 1024 * 1024
MAX_FRAME = 1024 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
ErrorKind = Literal["context_overflow", "other"]


class ProviderError(Exception):
    """A safe public error message. Upstream details, when retained, are private."""


class ProviderConnectionError(ProviderError):
    """The connection failed or broke. A partial reply must not be replayed automatically."""


class ProviderTimeout(ProviderError):
    """The provider exceeded a connection, read or total deadline."""


class ProviderHTTPError(ProviderError):
    def __init__(self, status: int, excerpt: str = "", kind: ErrorKind = "other"):
        self.status, self._excerpt, self.kind = status, excerpt, kind
        super().__init__(f"Model server returned HTTP {status}.")


class ProviderBusy(ProviderHTTPError):
    """The provider returned 429 or 503."""


class MalformedFrame(ProviderError):
    """The response was invalid or exceeded a bound."""


class ProviderStreamError(MalformedFrame):
    def __init__(self, excerpt: str, kind: ErrorKind):
        self._excerpt, self.kind = excerpt, kind
        super().__init__("Model server reported an error inside the stream.")


def _error_details(value: bytes | dict, key: str) -> tuple[str, ErrorKind]:
    """Normalize provider JSON before redaction. Never put this excerpt in messages or logs."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
        try:
            value = json.loads(value)
        except ValueError:
            pass
    if isinstance(value, dict):
        value = value.get("error", value)
    if isinstance(value, dict):
        value = " ".join(value[field] for field in ("message", "code", "type") if isinstance(value.get(field), str))
    message = value if isinstance(value, str) else ""
    if key:
        message = message.replace(key, "[redacted]")
    message = " ".join(message.split())[:8192]
    lower = message.lower()
    overflow = (
        "context_length_exceeded" in lower or "exceed_context_size_error" in lower
        or re.search(r"\b(?:context|prompt|input)\b.{0,120}\b(?:too (?:long|large)|exceed\w*|overflow)\b", lower)
        or re.search(r"\b(?:exceed\w*|overflow)\b.{0,120}\bcontext\b", lower)
        or re.search(r"\bmaximum context (?:length|size)\b.{0,300}\b(?:however|requested|resulted)\b", lower)
    )
    return message[:300], "context_overflow" if overflow else "other"


@dataclass(frozen=True)
class ToolCall:
    index: int
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class Event:
    kind: Literal["text", "reasoning", "tool_call", "usage", "finish"]
    text: str = ""
    tool_call: ToolCall | None = None
    usage: dict | None = None
    finish_reason: str | None = None


@dataclass(frozen=True)
class Completion:
    content: str
    reasoning: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict | None = None
    finish_reason: str | None = None


def base_url(value: str) -> str:
    """A fixed HTTP(S) API base, normally ending in /v1. Credentials belong in the key file."""
    try:
        if not isinstance(value, str):
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or any(char.isspace() or ord(char) < 32 for char in value)):
            raise ValueError
        parsed.port
        return str(URL(value)).rstrip("/")
    except (ValueError, TypeError):
        raise ValueError("Choose an HTTP(S) model base URL without credentials, query or fragment.") from None


def thinking_fields(level: str, mode: str) -> dict:
    """Template flags have only on/off; effort models map Off to the protocol's `none`."""
    if level not in THINKING_LEVELS or mode not in THINKING_MODES:
        raise ValueError("Unknown thinking level or model mode.")
    if mode == "reasoning_effort":
        return {"reasoning_effort": "none" if level == "off" else level}
    if mode == "template_flag":
        return {"chat_template_kwargs": {"enable_thinking": level != "off"}}
    return {}


def _object(raw: bytes | str) -> dict:
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise MalformedFrame("Model server returned invalid JSON.") from None
    if not isinstance(value, dict):
        raise MalformedFrame("Model server returned a non-object frame.")
    return value


def _text(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise MalformedFrame("Model server returned invalid text.")
    return value


class _Thoughts:
    """Strip tags incrementally and stream only the first source that produces reasoning."""

    def __init__(self):
        self.pending = ""
        self.inside = False
        self.source = None

    def _reasoning(self, source: str, text: str) -> list[Event]:
        if not text:
            return []
        if self.source is None:
            self.source = source
        return [Event("reasoning", text=text)] if self.source == source else []

    def content(self, text: str, *, final: bool = False) -> list[Event]:
        self.pending += text
        events = []
        while self.pending:
            marker = "</think>" if self.inside else "<think>"
            position = self.pending.find(marker)
            if position >= 0:
                part, self.pending = self.pending[:position], self.pending[position + len(marker):]
                if self.inside:
                    events.extend(self._reasoning("tagged", part))
                elif part:
                    events.append(Event("text", text=part))
                self.inside = not self.inside
                continue
            keep = 0
            if not final:
                for size in range(1, min(len(marker), len(self.pending) + 1)):
                    if self.pending.endswith(marker[:size]):
                        keep = size
            part = self.pending[:-keep] if keep else self.pending
            if self.inside:
                events.extend(self._reasoning("tagged", part))
            elif part:
                events.append(Event("text", text=part))
            self.pending = self.pending[-keep:] if keep else ""
            break
        return events

    def feed(self, delta: dict) -> list[Event]:
        events = []
        for source in ("reasoning_content", "reasoning"):
            if self.source in (None, source):
                events.extend(self._reasoning(source, _text(delta.get(source))))
        events.extend(self.content(_text(delta.get("content"))))
        return events

    def finish(self) -> list[Event]:
        return self.content("", final=True)


class _Calls:
    def __init__(self):
        self.calls: dict[int, dict] = {}

    def feed(self, fragments, *, streaming: bool = True) -> None:
        if fragments is None:
            return
        if not isinstance(fragments, list):
            raise MalformedFrame("Model server returned invalid tool calls.")
        for ordinal, fragment in enumerate(fragments):
            if not isinstance(fragment, dict):
                raise MalformedFrame("Model server returned an invalid tool call.")
            index = fragment.get("index", ordinal if not streaming else None)
            call_id = _text(fragment.get("id"))
            if index is None and call_id:
                index = next((i for i, call in self.calls.items() if call["id"] == call_id), max(self.calls, default=-1) + 1)
            if type(index) is not int or not 0 <= index < 128:
                raise MalformedFrame("Model server returned an invalid tool index.")
            call = self.calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if call_id:
                if call["id"] and call["id"] != call_id:
                    raise MalformedFrame("Model server changed a tool call ID.")
                call["id"] = call_id
            function = fragment.get("function") or {}
            if not isinstance(function, dict) or fragment.get("type", "function") != "function":
                raise MalformedFrame("Model server returned an invalid tool function.")
            call["name"] += _text(function.get("name"))
            call["arguments"] += _text(function.get("arguments"))

    def finish(self) -> list[ToolCall]:
        calls = [ToolCall(index, **call) for index, call in sorted(self.calls.items())]
        if any(not call.id or not call.name for call in calls) or len({call.id for call in calls}) != len(calls):
            raise MalformedFrame("Model server returned incomplete or duplicate tool calls.")
        return calls


async def _lines(content) -> AsyncIterator[bytes]:
    pending = bytearray()
    skip_lf = False
    async for chunk in content.iter_chunked(65536):
        # Handle LF, CRLF and CR, including a CRLF split across reads.
        for byte in chunk:
            if skip_lf and byte == 10:
                skip_lf = False
                continue
            skip_lf = False
            if byte in (10, 13):
                yield bytes(pending)
                pending.clear()
                skip_lf = byte == 13
            else:
                pending.append(byte)
                if len(pending) > MAX_LINE:
                    raise MalformedFrame("Model server exceeded the SSE line limit.")
    if pending:
        yield bytes(pending)


async def _frames(content) -> AsyncIterator[bytes]:
    data, size = [], 0
    async for line in _lines(content):
        if not line:
            if data:
                yield b"\n".join(data)
            data, size = [], 0
        elif line.startswith(b"data:"):
            value = line[5:]
            if value.startswith(b" "):
                value = value[1:]
            size += len(value) + 1
            if size > MAX_FRAME:
                raise MalformedFrame("Model server exceeded the SSE frame limit.")
            data.append(value)
        elif not line.startswith((b":", b"event:", b"id:", b"retry:")):
            raise MalformedFrame("Model server returned an invalid SSE line.")
    if data:
        yield b"\n".join(data)


class Provider:
    def __init__(self, session: aiohttp.ClientSession, url: str, key: str = "", *,
                 timeout: aiohttp.ClientTimeout | None = None):
        self.session, self.url = session, base_url(url)
        if not isinstance(key, str) or len(key) > 4096 or any(not 32 <= ord(char) < 127 for char in key):
            raise ValueError("Model key must be a single line of ASCII text, at most 4096 characters.")
        self._key = key
        self.timeout = timeout or aiohttp.ClientTimeout(total=None, connect=10, sock_read=900)
        self._discovery_timeout = timeout or aiohttp.ClientTimeout(total=30, connect=10, sock_read=15)

    @asynccontextmanager
    async def _request(self, method: str, path: str, *, timeout: aiohttp.ClientTimeout | None = None, **kwargs):
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        try:
            async with self.session.request(method, self.url + path, headers=headers, timeout=timeout or self.timeout,
                                            allow_redirects=False, **kwargs) as response:
                if response.status >= 300:
                    raw = bytearray()
                    while len(raw) < 8192:
                        chunk = await response.content.read(8192 - len(raw))
                        if not chunk:
                            break
                        raw.extend(chunk)
                    excerpt, kind = _error_details(bytes(raw), self._key)
                    error = ProviderBusy if response.status in (429, 503) else ProviderHTTPError
                    raise error(response.status, excerpt, kind)
                yield response
        except asyncio.TimeoutError:
            raise ProviderTimeout("Model server timed out.") from None
        except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError):
            raise ProviderConnectionError("Model server connection failed or ended early.") from None
        except aiohttp.ClientError:
            raise ProviderError("Model server request failed.") from None

    async def _json_response(self, response) -> dict:
        body = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE:
                raise MalformedFrame("Model server exceeded the response limit.")
        return _object(bytes(body))

    async def list_models(self) -> list[str]:
        async with self._request("GET", "/models", timeout=self._discovery_timeout) as response:
            data = (await self._json_response(response)).get("data")
        if not isinstance(data, list) or any(not isinstance(item, dict) or not isinstance(item.get("id"), str)
                                             or not item["id"] or len(item["id"]) > 256 for item in data):
            raise MalformedFrame("Model server returned an invalid model list.")
        return list(dict.fromkeys(item["id"] for item in data))

    @staticmethod
    def _payload(model: str, messages: list[dict], thinking: str, mode: str, tools: list[dict] | None, stream: bool) -> dict:
        if not isinstance(model, str) or not model or not isinstance(messages, list) or not messages:
            raise ValueError("A model and messages are required.")
        payload = {"model": model, "messages": messages, "stream": stream, **thinking_fields(thinking, mode)}
        if stream:
            payload["stream_options"] = {"include_usage": True}
        if tools is not None:
            payload["tools"] = tools
        return payload

    async def stream(self, model: str, messages: list[dict], *, thinking: str = "off", mode: str = "unsupported",
                     tools: list[dict] | None = None, tool_choice: dict | None = None) -> AsyncIterator[Event]:
        payload = self._payload(model, messages, thinking, mode, tools, True)
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        thoughts, calls, total, finished = _Thoughts(), _Calls(), 0, False
        async with self._request("POST", "/chat/completions", json=payload) as response:
            if response.content_type != "text/event-stream":
                raise MalformedFrame("Model server did not return an SSE stream.")
            async for frame in _frames(response.content):
                total += len(frame)
                if total > MAX_RESPONSE:
                    raise MalformedFrame("Model server exceeded the stream limit.")
                if frame.strip() == b"[DONE]":
                    break
                value = _object(frame)
                if "error" in value:
                    excerpt, kind = _error_details(value, self._key)
                    raise ProviderStreamError(excerpt, kind)
                choices = value.get("choices")
                if not isinstance(choices, list) or len(choices) > 1:
                    raise MalformedFrame("Model server returned invalid choices.")
                for choice in choices:
                    if not isinstance(choice, dict) or choice.get("index", 0) != 0 or finished:
                        raise MalformedFrame("Model server returned an unexpected choice.")
                    delta = choice.get("delta")
                    if not isinstance(delta, dict):
                        raise MalformedFrame("Model server returned an invalid delta.")
                    for event in thoughts.feed(delta):
                        yield event
                    calls.feed(delta.get("tool_calls"))
                    reason = choice.get("finish_reason")
                    if reason is not None:
                        if not isinstance(reason, str) or not reason:
                            raise MalformedFrame("Model server returned an invalid finish reason.")
                        for event in thoughts.finish():
                            yield event
                        for call in calls.finish():
                            yield Event("tool_call", tool_call=call)
                        finished = True
                        yield Event("finish", finish_reason=reason)
                if value.get("usage") is not None:
                    if not isinstance(value["usage"], dict):
                        raise MalformedFrame("Model server returned invalid usage.")
                    yield Event("usage", usage=value["usage"])
        if not finished:
            raise MalformedFrame("Model stream ended before a finish reason.")

    async def complete(self, model: str, messages: list[dict], *, thinking: str = "off", mode: str = "unsupported",
                       tools: list[dict] | None = None, max_tokens: int | None = None) -> Completion:
        payload = self._payload(model, messages, thinking, mode, tools, False)
        if max_tokens is not None:
            if type(max_tokens) is not int or not 1 <= max_tokens <= 4096:
                raise ValueError("Invalid completion token limit.")
            payload["max_tokens"] = max_tokens
        async with self._request("POST", "/chat/completions", json=payload) as response:
            value = await self._json_response(response)
        choices = value.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise MalformedFrame("Model server returned invalid choices.")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise MalformedFrame("Model server returned an invalid message.")
        thoughts, calls = _Thoughts(), _Calls()
        events = thoughts.feed(message) + thoughts.finish()
        calls.feed(message.get("tool_calls"), streaming=False)
        usage, reason = value.get("usage"), choice.get("finish_reason")
        if (usage is not None and not isinstance(usage, dict)) or not isinstance(reason, str) or not reason:
            raise MalformedFrame("Model server returned invalid usage or finish reason.")
        return Completion("".join(e.text for e in events if e.kind == "text"),
                          "".join(e.text for e in events if e.kind == "reasoning"), calls.finish(), usage, reason)
