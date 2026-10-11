"""Server-only, narrow client for the pinned Hermes dashboard API."""

import asyncio
from pathlib import Path
from urllib.parse import quote

from aiohttp import ClientError, ClientSession, ClientTimeout, CookieJar, WSMsgType


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
                password = self.password_file.read_text(encoding="utf-8").rstrip("\r\n")
                async with self.http.post(self.base_url + "/auth/password-login", json={
                    "provider": "basic", "username": self.username, "password": password,
                }, allow_redirects=False) as response:
                    if response.status != 200:
                        raise HermesError(502, "Hermes dashboard sign-in failed.")
            except (OSError, ClientError, asyncio.TimeoutError) as exc:
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
                    if response.content_length and response.content_length > 8 * 1024 * 1024:
                        raise HermesError(502, "Hermes returned too much data.")
                    return await response.json()
            except (ClientError, asyncio.TimeoutError) as exc:
                raise HermesError(503, "Hermes is unavailable.") from exc
        raise HermesError(502, "Hermes dashboard sign-in expired.")

    async def send_message(self, text: str, stored_id: str | None = None) -> str:
        """Submit a turn then detach; Hermes persists and finishes the turn itself."""
        await self._login()
        ticket_data = await self.request("POST", "/api/auth/ws-ticket")
        ticket = ticket_data.get("ticket")
        if not isinstance(ticket, str) or not ticket:
            raise HermesError(502, "Hermes did not provide a chat ticket.")
        ws_url = self.base_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        ws_url += "/api/ws?ticket=" + quote(ticket, safe="")
        try:
            async with self.http.ws_connect(ws_url, timeout=20, max_msg_size=8 * 1024 * 1024) as ws:
                first_method = "session.resume" if stored_id else "session.create"
                first_params = ({"session_id": stored_id, "omit_messages": True,
                                 "close_on_disconnect": False} if stored_id else
                                {"source": "roost", "follow_profile_config": True,
                                 "close_on_disconnect": False})
                first = await self._rpc_on_socket(ws, 1, first_method, first_params)
                runtime_id = first.get("session_id")
                if not isinstance(runtime_id, str) or not runtime_id:
                    raise HermesError(502, "Hermes did not open the conversation.")
                await self._rpc_on_socket(ws, 2, "prompt.submit", {"session_id": runtime_id, "text": text})
                return str(first.get("stored_session_id") or stored_id or runtime_id)
        except (ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise HermesError(503, "Hermes chat is unavailable.") from exc

    @staticmethod
    async def _rpc_on_socket(ws, request_id: int, method: str, params: dict) -> dict:
        await ws.send_json({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            message = await asyncio.wait_for(ws.receive(), timeout=40)
            if message.type != WSMsgType.TEXT:
                raise HermesError(502, "Hermes closed the chat connection.")
            frame = message.json()
            if frame.get("id") != request_id:
                continue
            if "error" in frame:
                raise HermesError(502, "Hermes rejected the chat request.")
            return frame.get("result") or {}
