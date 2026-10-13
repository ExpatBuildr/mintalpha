"""Telegram Bot API client (raw HTTPS, long polling) plus a mock with the same interface."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from html import unescape

import httpx

from .retry import FatalError, RetryableError, with_retries

log = logging.getLogger("radar.telegram")


def project_keyboard(pid: int) -> list[list[dict]]:
    return [
        [{"text": "✅ Minted", "callback_data": f"a:m:{pid}"}, {"text": "❌ Missed", "callback_data": f"a:x:{pid}"}],
        [{"text": "🙈 Dismiss", "callback_data": f"a:d:{pid}"}, {"text": "😴 Snooze", "callback_data": f"a:s:{pid}"}],
        [{"text": "🔎 Details", "callback_data": f"a:v:{pid}"}],
    ]


def snooze_keyboard(pid: int) -> list[list[dict]]:
    return [
        [{"text": "1h", "callback_data": f"a:s1:{pid}"}, {"text": "4h", "callback_data": f"a:s4:{pid}"},
         {"text": "24h", "callback_data": f"a:s24:{pid}"}],
        [{"text": "↩️ Back", "callback_data": f"a:b:{pid}"}],
    ]


class TelegramClient:
    def __init__(self, token: str, http: httpx.AsyncClient | None = None, sleep=asyncio.sleep):
        self.base = f"https://api.telegram.org/bot{token}"
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10))
        self.sleep = sleep

    async def _call(self, method: str, payload: dict, timeout: float | None = None) -> dict:
        async def do():
            resp = await self.http.post(f"{self.base}/{method}", json=payload, timeout=timeout or 20)
            try:
                data = resp.json()
            except json.JSONDecodeError:
                raise RetryableError(f"telegram {method}: non-JSON response HTTP {resp.status_code}")
            if resp.status_code == 429:
                raise RetryableError("telegram rate limited",
                                     retry_after=float((data.get("parameters") or {}).get("retry_after", 5)))
            if resp.status_code >= 500:
                raise RetryableError(f"telegram {method}: HTTP {resp.status_code}")
            if resp.status_code in (401, 404):
                raise FatalError(f"telegram {method}: bot token rejected (HTTP {resp.status_code})")
            if not data.get("ok"):
                raise FatalError(f"telegram {method}: {data.get('description')}")
            return data["result"]
        return await with_retries(do, attempts=4, op=f"telegram.{method}", sleep=self.sleep)

    async def send_message(self, chat_id: str, text: str, buttons: list[list[dict]] | None = None) -> int:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        try:
            result = await self._call("sendMessage", payload)
        except FatalError as exc:
            if "can't parse entities" in str(exc):  # fall back to plain text rather than drop the alert
                plain = unescape(re.sub(r"<[^>]+>", "", text))
                payload.update(text=plain)
                payload.pop("parse_mode")
                result = await self._call("sendMessage", payload)
            else:
                raise
        return result["message_id"]

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]})

    async def edit_buttons(self, chat_id: str, message_id: int, buttons: list[list[dict]] | None) -> None:
        try:
            await self._call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id,
                                                        "reply_markup": {"inline_keyboard": buttons or []}})
        except FatalError as exc:
            if "message is not modified" not in str(exc):
                log.warning("edit_buttons_failed", extra={"error": str(exc)})

    async def get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        payload = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return await self._call("getUpdates", payload, timeout=timeout + 15)

    async def set_commands(self, commands: list[tuple[str, str]]) -> None:
        await self._call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in commands]})

    async def get_me(self) -> dict:
        return await self._call("getMe", {})


class MockTelegram:
    """Records messages instead of sending them. `echo=True` prints a readable transcript."""

    def __init__(self, echo: bool = False):
        self.sent: list[dict] = []
        self.answers: list[tuple[str, str]] = []
        self.edits: list[tuple[int, list | None]] = []
        self.updates: asyncio.Queue = asyncio.Queue()
        self.echo = echo
        self.fail_next = 0
        self._next_id = 1000

    async def send_message(self, chat_id: str, text: str, buttons=None) -> int:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RetryableError("simulated Telegram outage")
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons, "message_id": self._next_id})
        if self.echo:
            plain = unescape(re.sub(r"<[^>]+>", "", text))
            labels = " ".join(f"[{b['text']}]" for row in (buttons or []) for b in row)
            print(f"\n──── Telegram (mock) ────\n{plain}" + (f"\n{labels}" if labels else ""))
        return self._next_id

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.answers.append((callback_id, text))

    async def edit_buttons(self, chat_id: str, message_id: int, buttons) -> None:
        self.edits.append((message_id, buttons))

    async def get_updates(self, offset, timeout) -> list[dict]:
        try:
            item = await asyncio.wait_for(self.updates.get(), timeout=min(timeout, 1))
            return [item]
        except asyncio.TimeoutError:
            return []

    async def set_commands(self, commands) -> None:
        return None

    async def get_me(self) -> dict:
        return {"username": "mock_radar_bot"}
