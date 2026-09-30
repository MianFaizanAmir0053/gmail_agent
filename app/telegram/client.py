"""Minimal Telegram Bot API client.

Plain HTTP against four endpoints. A library would add a dependency, an async
runtime opinion, and a lot of surface we do not use, in exchange for saving
about forty lines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

API_ROOT = "https://api.telegram.org"


class TelegramError(RuntimeError):
    """The Bot API rejected a call."""


class Sender(Protocol):
    """What the handler needs. Lets tests record calls instead of making them."""

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        keyboard: list[list[dict[str, str]]] | None = None,
        force_reply: bool = False,
    ) -> dict[str, Any]: ...

    def answer_callback(self, callback_id: str, text: str = "") -> None: ...

    def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None: ...


@dataclass(slots=True)
class TelegramClient:
    token: str = field(repr=False)
    """The bot token. Kept out of the repr, which lands in logs and error reports."""

    timeout: float = 20.0

    def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = httpx.post(
            f"{API_ROOT}/bot{self.token}/{method}", json=payload, timeout=self.timeout
        )
        body: dict[str, Any] = response.json()
        if not body.get("ok"):
            raise TelegramError(f"{method} failed: {body.get('description', body)}")
        result: dict[str, Any] = body.get("result") or {}
        return result

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        keyboard: list[list[dict[str, str]]] | None = None,
        force_reply: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        elif force_reply:
            payload["reply_markup"] = {"force_reply": True, "selective": True}
        return self._post("sendMessage", payload)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        """Stops the button's spinner. Telegram retries the update if this is
        never sent, so skipping it produces phantom duplicate taps."""
        self._post("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None:
        self._post(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
            },
        )

    def set_webhook(self, url: str, secret_token: str) -> dict[str, Any]:
        return self._post(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token,
                "allowed_updates": ["message", "callback_query"],
                "drop_pending_updates": True,
            },
        )

    def delete_webhook(self) -> dict[str, Any]:
        return self._post("deleteWebhook", {"drop_pending_updates": True})

    def get_updates(self, offset: int | None = None, timeout: int = 0) -> list[dict[str, Any]]:
        """Development-only polling.

        Production uses the webhook -- one process, no extra worker. This exists
        so the flow can be exercised locally without a public URL or a tunnel.
        """
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset

        response = httpx.post(
            f"{API_ROOT}/bot{self.token}/getUpdates", json=payload, timeout=self.timeout + timeout
        )
        body = response.json()
        if not body.get("ok"):
            raise TelegramError(f"getUpdates failed: {body.get('description', body)}")
        updates: list[dict[str, Any]] = body.get("result") or []
        return updates
