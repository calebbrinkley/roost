"""Server-only, narrow client for the pinned Hermes dashboard API."""

import asyncio
import json
from pathlib import Path
from urllib.parse import quote

from aiohttp import ClientError, ClientSession, ClientTimeout, CookieJar, WSMsgType


MAX_RESPONSE_BYTES = 8 * 1024 * 1024
RPC_TIMEOUT = 40


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

    async def request(self, method: str, path: str, body: dict | None = None, *, timeout: int = 45):
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

    async def send_message(self, text: str, stored_id: str | None = None, follow=None) -> str:
        """Submit a turn; Hermes persists and finishes the turn itself even if Roost hangs up.
        With `follow`, the socket stays open and `follow(session_id, events)` is called, where `events`
        yields this turn's event frames (type and payload) until the socket closes."""
        await self._login()
        ticket_data = await self.request("POST", "/api/auth/ws-ticket")
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
            first_method = "session.resume" if stored_id else "session.create"
            first_params = ({"session_id": stored_id, "omit_messages": True,
                             "close_on_disconnect": False} if stored_id else
                            {"source": "roost", "follow_profile_config": True,
                             "close_on_disconnect": False})
            first = await self._rpc_on_socket(ws, 1, first_method, first_params)
            runtime_id = first.get("session_id")
            if not isinstance(runtime_id, str) or not runtime_id:
                raise HermesError(502, "Hermes did not open the conversation.")
            # The turn may start streaming before prompt.submit answers; keep those frames.
            early = []
            await self._rpc_on_socket(ws, 2, "prompt.submit", {"session_id": runtime_id, "text": text}, early)
            session_id = first.get("stored_session_id") or stored_id or runtime_id
            if not isinstance(session_id, str) or not session_id:
                raise HermesError(502, "Hermes returned an invalid conversation identifier.")
        except (ClientError, asyncio.TimeoutError, ValueError) as exc:
            await ws.close()
            raise HermesError(503, "Hermes chat is unavailable.") from exc
        except BaseException:
            await ws.close()
            raise
        if follow is None:
            await ws.close()
        else:
            follow(session_id, self._turn_events(ws, runtime_id, early))
        return session_id

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
        finally:
            await ws.close()

    @staticmethod
    def _event(frame, runtime_id: str) -> tuple[str, dict] | None:
        params = frame.get("params") if isinstance(frame, dict) and frame.get("method") == "event" else None
        if not isinstance(params, dict) or params.get("session_id") != runtime_id or not isinstance(params.get("type"), str):
            return None
        payload = params.get("payload")
        return params["type"], payload if isinstance(payload, dict) else {}

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
                    if events is not None and frame.get("method") == "event":
                        events.append(frame)
                    continue
                if "error" in frame:
                    raise HermesError(502, "Hermes rejected the chat request.")
                result = frame.get("result")
                if not isinstance(result, dict):
                    raise HermesError(502, "Hermes returned invalid chat data.")
                return result
