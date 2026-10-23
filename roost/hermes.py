"""Server-only, narrow client for the pinned Hermes dashboard API.

The rest of Roost speaks in conversations, jobs and the profile; Hermes's paths, query strings, ID quoting
and response variants stay in this module. Records are Hermes's own, plus an `id` that is always set,
and lists always come wrapped (`{"sessions": [...]}`, `{"jobs": [...]}`, `{"runs": [...]}`).
"""

import asyncio
import json
import re
from pathlib import Path
from urllib.parse import quote

from aiohttp import ClientError, ClientSession, ClientTimeout, CookieJar, WSMsgType

from . import approvals


MAX_RESPONSE_BYTES = 8 * 1024 * 1024
RPC_TIMEOUT = 40
# A conversation, job or profile ID: also a path segment, so never "." or "..".
ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
SESSION_PAGE = 100
RUNS_LIMIT = 20
JOB_ACTIONS = ("pause", "resume", "trigger")


def plain_id(value) -> str:
    """The ID when it is a safe path segment, else ""."""
    return value if isinstance(value, str) and ID.fullmatch(value) and value not in {".", ".."} else ""


def _segment(value: str) -> str:
    if not plain_id(value):
        raise HermesError(400, "Invalid identifier.")
    return quote(value, safe="")


def _with_id(item, *keys) -> dict | None:
    """A copy of Hermes's record with `id` taken from the first of `keys` it has; None without one."""
    if not isinstance(item, dict):
        return None
    found = next((plain_id(item.get(key)) for key in keys if plain_id(item.get(key))), "")
    return {**item, "id": found} if found else None


def _listed(result, key: str, *id_keys, required: bool = True) -> dict:
    """Hermes answers some lists bare and some wrapped; Roost always wraps them under `key`.
    A record without a usable ID is dropped, unless `required` is False (a run need not link anywhere)."""
    rows = result if isinstance(result, list) else (result.get(key) if isinstance(result, dict) else None)
    records = []
    for item in rows if isinstance(rows, list) else []:
        record = _with_id(item, *id_keys)
        if record is None and not required and isinstance(item, dict):
            record = {name: value for name, value in item.items() if name != "id"}
        if record is not None:
            records.append(record)
    return {**(result if isinstance(result, dict) else {}), key: records}


def _messages(result) -> dict:
    """A transcript with `messages` always a list; anything else from Hermes is invalid."""
    if not isinstance(result, dict) or not isinstance(result.get("messages"), list):
        raise HermesError(502, "Hermes returned invalid data.")
    return result


class HermesError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


class Hermes:
    def __init__(self, base_url: str, username: str, password_file: Path):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password_file = password_file
        self.http = None
        self.login_lock = asyncio.Lock()
        self.authenticated = False

    async def start(self) -> None:
        self.http = ClientSession(cookie_jar=CookieJar(unsafe=True), timeout=ClientTimeout(total=45))

    async def close(self) -> None:
        if self.http is not None:
            await self.http.close()

    async def _login(self, force: bool = False) -> None:
        async with self.login_lock:
            if self.authenticated and not force:
                return
            try:
                stored = self.password_file.read_text(encoding="utf-8").rstrip("\r\n")
                async with self.http.post(self.base_url + "/auth/password-login", json={
                    "provider": "basic", "username": self.username, "password": stored,
                }, allow_redirects=False) as response:
                    if response.status != 200:
                        raise HermesError(502, "Hermes dashboard sign-in failed.")
            except (OSError, UnicodeError, ClientError, asyncio.TimeoutError) as exc:
                raise HermesError(503, "Hermes dashboard is unavailable.") from exc
            self.authenticated = True

    async def _request(self, method: str, path: str, body: dict | None = None, *, timeout: int = 45):
        await self._login()
        for attempt in range(2):
            try:
                async with self.http.request(method, self.base_url + path, json=body,
                                             timeout=ClientTimeout(total=timeout), allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308, 401, 403} and attempt == 0:
                        await self._login(force=True)
                        continue
                    if response.status >= 300:
                        # 400 is Hermes rejecting the request itself (such as a duplicate title), not a fault.
                        raise HermesError(response.status if response.status in {400, 404} else 502,
                                          "Hermes could not complete that request.")
                    if response.content_length and response.content_length > MAX_RESPONSE_BYTES:
                        raise HermesError(502, "Hermes returned too much data.")
                    payload = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        if len(payload) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise HermesError(502, "Hermes returned too much data.")
                        payload.extend(chunk)
                    try:
                        result = json.loads(payload)
                    except (ValueError, UnicodeError, RecursionError):
                        raise HermesError(502, "Hermes returned invalid data.") from None
                    if not isinstance(result, (dict, list)):
                        raise HermesError(502, "Hermes returned invalid data.")
                    return result
            except (ClientError, asyncio.TimeoutError) as exc:
                raise HermesError(503, "Hermes is unavailable.") from exc
        raise HermesError(502, "Hermes dashboard sign-in expired.")

    # Conversations ("sessions" in Hermes). Conversation records carry `id` from Hermes's `session_id`.

    async def sessions(self, offset: int = 0) -> dict:
        """One page of conversations, newest first, without job runs."""
        result = await self._request(
            "GET", f"/api/sessions?limit={SESSION_PAGE}&order=recent&exclude_sources=cron&offset={int(offset)}")
        return _listed(result, "sessions", "session_id", "id")

    async def messages(self, sid: str) -> dict:
        """The newest part of a transcript (Hermes returns at most its newest 500 rows)."""
        return _messages(await self._request("GET", f"/api/sessions/{_segment(sid)}/messages?inline_images=false"))

    async def history(self, sid: str, *, offset: int, limit: int) -> list:
        """One page of the whole transcript, oldest first, compressed history included."""
        result = await self._request(
            "GET", f"/api/sessions/{_segment(sid)}/messages?inline_images=false&order=oldest&include_compacted=true"
                   f"&limit={int(limit)}&offset={int(offset)}")
        return _messages(result)["messages"]

    async def rename_session(self, sid: str, title: str) -> dict:
        return await self._request("PATCH", f"/api/sessions/{_segment(sid)}", {"title": title})

    async def delete_session(self, sid: str) -> dict:
        return await self._request("DELETE", f"/api/sessions/{_segment(sid)}")

    # Jobs ("cron jobs" in Hermes). Job records carry `id` from Hermes's `id` or `job_id`.

    async def jobs(self) -> dict:
        return _listed(await self._request("GET", "/api/cron/jobs"), "jobs", "id", "job_id")

    async def job(self, jid: str) -> dict:
        return self._job(await self._request("GET", f"/api/cron/jobs/{_segment(jid)}"))

    async def runs(self, jid: str) -> dict:
        """The job's most recent runs. A run's `id` is the conversation it wrote, when it has one."""
        result = await self._request("GET", f"/api/cron/jobs/{_segment(jid)}/runs?limit={RUNS_LIMIT}")
        return _listed(result, "runs", "id", "session_id", required=False)

    async def create_job(self, fields: dict) -> dict:
        return self._job(await self._request("POST", "/api/cron/jobs", fields))

    async def update_job(self, jid: str, updates: dict) -> dict:
        return self._job(await self._request("PUT", f"/api/cron/jobs/{_segment(jid)}", {"updates": updates}))

    async def job_action(self, jid: str, action: str) -> dict:
        if action not in JOB_ACTIONS:
            raise HermesError(404, "Unknown job action.")
        return self._job(await self._request("POST", f"/api/cron/jobs/{_segment(jid)}/{action}"))

    async def delete_job(self, jid: str) -> dict:
        return await self._request("DELETE", f"/api/cron/jobs/{_segment(jid)}")

    @staticmethod
    def _job(result) -> dict:
        """Hermes answers a job change with the job as saved; other answers pass through."""
        return _with_id(result, "id", "job_id") or (result if isinstance(result, dict) else {})

    # The profile this dashboard serves: its name, display name and SOUL.

    async def profile(self) -> dict:
        """The active profile's record. Its name is a path segment, so anything unusual reads as "default"."""
        name = await self._profile_name()
        listed = await self._request("GET", "/api/profiles")
        profiles = listed.get("profiles") if isinstance(listed, dict) else None
        found = next((item for item in profiles if isinstance(item, dict) and item.get("name") == name), None) \
            if isinstance(profiles, list) else None
        return {**(found or {}), "name": name}

    async def _profile_name(self) -> str:
        active = await self._request("GET", "/api/profiles/active")
        return plain_id(active.get("current") if isinstance(active, dict) else None) or "default"

    async def soul(self) -> str:
        result = await self._request("GET", f"/api/profiles/{_segment(await self._profile_name())}/soul")
        content = result.get("content") if isinstance(result, dict) else None
        return content if isinstance(content, str) else ""

    async def save_soul(self, content: str) -> None:
        await self._request("PUT", f"/api/profiles/{_segment(await self._profile_name())}/soul", {"content": content})

    # The rest of the dashboard.

    async def status(self) -> dict:
        result = await self._request("GET", "/api/status", timeout=10)
        return result if isinstance(result, dict) else {}

    async def restart(self) -> None:
        await self._request("POST", "/api/gateway/restart")

    async def transcribe(self, data_url: str, mime_type: str) -> str:
        result = await self._request("POST", "/api/audio/transcribe", {"data_url": data_url, "mime_type": mime_type}, timeout=180)
        text = result.get("transcript") if isinstance(result, dict) else None
        return text.strip() if isinstance(text, str) else ""

    async def speak(self, text: str) -> str:
        """The spoken text as a data URL ("" when Hermes sent none)."""
        result = await self._request("POST", "/api/audio/speak", {"text": text}, timeout=180)
        spoken = result.get("data_url") if isinstance(result, dict) else None
        return spoken if isinstance(spoken, str) else ""

    async def _socket(self):
        await self._login()
        ticket_data = await self._request("POST", "/api/auth/ws-ticket")
        ticket = ticket_data.get("ticket") if isinstance(ticket_data, dict) else None
        if not isinstance(ticket, str) or not ticket:
            raise HermesError(502, "Hermes did not provide a chat ticket.")
        ws_url = self.base_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        ws_url += "/api/ws?ticket=" + quote(ticket, safe="")
        try:
            ws = await self.http.ws_connect(ws_url, timeout=20, max_msg_size=8 * 1024 * 1024)
        except (ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise HermesError(503, "Hermes chat is unavailable.") from exc
        try:
            await self._rpc_on_socket(ws, 0, "client.capabilities", {"server_requests": True})
        except (ClientError, asyncio.TimeoutError, ValueError) as exc:
            await ws.close()
            raise HermesError(503, "Hermes approval support is unavailable.") from exc
        except BaseException:
            await ws.close()
            raise
        return ws

    async def approvals(self, stored_id: str, request_id=None, choice=None):
        """Read a redacted resume snapshot; resolve only an exact, still-offered request."""
        ws = await self._socket()
        try:
            result = await self._rpc_on_socket(ws, 1, "session.resume", {
                "session_id": stored_id, "omit_messages": True, "close_on_disconnect": False})
            pending = approvals.snapshot(result)
            if request_id is None:
                return pending
            item = next((item for item in pending if item["request_id"] == request_id), None)
            if item is None or choice not in {"once", "deny"} or choice not in item["choices"]:
                raise HermesError(409, "This request is no longer waiting for that decision. Refresh the conversation.")
            runtime_id = result.get("session_id")
            if not isinstance(runtime_id, str) or not runtime_id:
                raise HermesError(502, "Hermes did not identify the approval session.")
            resolved = await self._rpc_on_socket(ws, 2, "approval.respond", {
                "session_id": runtime_id, "request_id": request_id, "choice": choice})
            if resolved.get("resolved") != 1:
                raise HermesError(409, "This request expired or was already answered. Refresh the conversation.")
            return {"resolved": True}
        except (ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise HermesError(503, "Hermes approvals are unavailable. No decision was confirmed.") from exc
        finally:
            await ws.close()

    async def send_message(self, text: str, stored_id: str | None = None, follow=None, reasoning: str | None = None,
                           applied=None) -> str:
        """Submit a turn; Hermes persists and finishes the turn itself even if Roost hangs up.
        With `follow`, the socket stays open and `follow(session_id, events)` is called, where `events`
        yields this turn's event frames (type and payload) until the socket closes.
        Busy-session acknowledgements call follow(session_id, None): polling owns the overlapping turns.
        `reasoning` is a level to set for this conversation first ("default" restores Hermes's own);
        `applied(level)` is called once Hermes has accepted it."""
        ws = await self._socket()
        try:
            first_method = "session.resume" if stored_id else "session.create"
            first_params = ({"session_id": stored_id, "omit_messages": True,
                             "close_on_disconnect": False} if stored_id else
                            {"source": "roost", "follow_profile_config": True,
                             "close_on_disconnect": False})
            first = await self._rpc_on_socket(ws, 1, first_method, first_params)
            runtime_id = first.get("session_id")
            if not isinstance(runtime_id, str) or not runtime_id:
                raise HermesError(502, "Hermes did not open the conversation.")
            if reasoning is not None and await self._set_reasoning(ws, runtime_id, reasoning) and applied is not None:
                applied(reasoning)
            # The turn may start streaming before prompt.submit answers; keep those frames.
            early = []
            submitted = await self._rpc_on_socket(ws, 2, "prompt.submit", {"session_id": runtime_id, "text": text}, early)
            session_id = first.get("stored_session_id") or stored_id or runtime_id
            if not isinstance(session_id, str) or not session_id:
                raise HermesError(502, "Hermes returned an invalid conversation identifier.")
        except (ClientError, asyncio.TimeoutError, ValueError) as exc:
            await ws.close()
            raise HermesError(503, "Hermes chat is unavailable.") from exc
        except BaseException:
            await ws.close()
            raise
        if submitted.get("status") in {"queued", "steered", "redirected"}:
            # This socket can still receive the OLD turn's completion. Do not label it as the follow-up.
            await ws.close()
            if follow is not None:
                follow(session_id, None)
        elif follow is None:
            await ws.close()
        else:
            follow(session_id, self._turn_events(ws, runtime_id, early))
        return session_id

    async def _set_reasoning(self, ws, runtime_id: str, level: str) -> bool:
        """Set how hard the model thinks in this conversation only. Hermes keeps a conversation's choice
        while it stays loaded but not after a restart, so Roost sets it with each message. A rejected
        level (an older Hermes, an unknown word) leaves the message to be sent as it would have been."""
        try:
            if level == "default":
                # Without a conversation, Hermes answers with its own configured level.
                current = await self._rpc_on_socket(ws, 3, "config.get", {"key": "reasoning"})
                level_word = current.get("value")
                if not isinstance(level_word, str) or not level_word:
                    return False
            else:
                level_word = level
            await self._rpc_on_socket(ws, 4, "config.set", {"session_id": runtime_id, "key": "reasoning", "value": level_word})
            return True
        except HermesError:
            return False

    @staticmethod
    async def _turn_events(ws, runtime_id: str, early: list):
        """This session's event frames as (type, payload) pairs; closes the socket when done."""
        try:
            for frame in early:
                if (event := Hermes._event(frame, runtime_id)) is not None:
                    yield event
            while True:
                message = await ws.receive()
                if message.type != WSMsgType.TEXT:
                    return
                try:
                    frame = message.json()
                except (ValueError, RecursionError):
                    return
                if (event := Hermes._event(frame, runtime_id)) is not None:
                    yield event
                elif isinstance(frame, dict) and frame.get("method") not in {None, "approval"} and "id" in frame:
                    await Hermes._unsupported(ws, frame)
        finally:
            await ws.close()

    @staticmethod
    def _event(frame, runtime_id: str) -> tuple[str, dict] | None:
        if isinstance(frame, dict) and frame.get("method") == "approval":
            params = frame.get("params")
            if isinstance(params, dict) and params.get("session_id") == runtime_id:
                return "approval.request", {**params, "server_id": frame.get("id")}
        params = frame.get("params") if isinstance(frame, dict) and frame.get("method") == "event" else None
        if not isinstance(params, dict) or params.get("session_id") != runtime_id or not isinstance(params.get("type"), str):
            return None
        payload = params.get("payload")
        return params["type"], payload if isinstance(payload, dict) else {}

    @staticmethod
    async def _unsupported(ws, frame):
        await ws.send_json({"jsonrpc": "2.0", "id": frame["id"],
                            "error": {"code": -32601, "message": "Roost does not support this request."}})

    @staticmethod
    async def _rpc_on_socket(ws, request_id: int, method: str, params: dict, events: list | None = None) -> dict:
        # Unrelated gateway events must not restart the deadline indefinitely.
        async with asyncio.timeout(RPC_TIMEOUT):
            await ws.send_json({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            while True:
                message = await ws.receive()
                if message.type != WSMsgType.TEXT:
                    raise HermesError(502, "Hermes closed the chat connection.")
                try:
                    frame = message.json()
                except (ValueError, RecursionError):
                    raise HermesError(502, "Hermes returned invalid chat data.") from None
                if not isinstance(frame, dict):
                    raise HermesError(502, "Hermes returned invalid chat data.")
                if frame.get("id") != request_id:
                    if frame.get("method") in {"event", "approval"}:
                        if events is not None:
                            events.append(frame)
                    elif frame.get("method") and "id" in frame:
                        await Hermes._unsupported(ws, frame)
                    continue
                if "error" in frame:
                    raise HermesError(502, "Hermes rejected the chat request.")
                result = frame.get("result")
                if not isinstance(result, dict):
                    raise HermesError(502, "Hermes returned invalid chat data.")
                return result
