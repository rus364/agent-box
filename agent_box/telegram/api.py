"""Minimal Telegram Bot API client (long polling; no webhook, no framework).

Only the handful of methods the notifier and remote-control bot need are
implemented, over ``urllib`` from the standard library.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class TelegramAPIError(RuntimeError):
    def __init__(self, method: str, description: str = ""):
        super().__init__(f"Telegram API {method} failed: {description}".strip())
        self.method = method
        self.description = description


class TelegramClient:
    """Never logs or includes the bot token in exceptions/messages."""

    def __init__(self, token: str, base_url: str = "https://api.telegram.org", timeout: float = 20):
        if not token:
            raise ValueError("Telegram bot token required")
        self._token = token
        self._base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        url = f"{self._base_url}/bot{self._token}/{method}"
        body = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                payload = json.load(exc)
            except Exception:
                raise TelegramAPIError(method, f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TelegramAPIError(method, str(exc)) from exc
        if not isinstance(payload, dict) or not payload.get("ok"):
            description = payload.get("description", "") if isinstance(payload, dict) else ""
            raise TelegramAPIError(method, description)
        return payload.get("result")

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict[str, Any]]:
        return self._call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
            timeout=timeout + 10,
        )

    def send_message(
        self, chat_id: str | int, text: str, *,
        reply_markup: dict[str, Any] | None = None,
        message_thread_id: int | None = None,
        parse_mode: str = "HTML",
    ) -> dict[str, Any]:
        return self._call("sendMessage", {
            "chat_id": chat_id, "text": text, "parse_mode": parse_mode,
            "reply_markup": reply_markup, "message_thread_id": message_thread_id,
            "disable_web_page_preview": True,
        })

    def edit_message_text(
        self, chat_id: str | int, message_id: int, text: str, *,
        reply_markup: dict[str, Any] | None = None, parse_mode: str = "HTML",
    ) -> Any:
        return self._call("editMessageText", {
            "chat_id": chat_id, "message_id": message_id, "text": text,
            "parse_mode": parse_mode, "reply_markup": reply_markup,
        })

    def edit_message_reply_markup(
        self, chat_id: str | int, message_id: int, reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        return self._call("editMessageReplyMarkup", {
            "chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup,
        })

    def answer_callback_query(self, callback_query_id: str, text: str = "", show_alert: bool = False) -> Any:
        return self._call("answerCallbackQuery", {
            "callback_query_id": callback_query_id, "text": text or None, "show_alert": show_alert,
        })

    def get_me(self) -> dict[str, Any]:
        return self._call("getMe", {})
