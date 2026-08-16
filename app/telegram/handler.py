"""Turning a Telegram update into a graph decision.

Written as a pure-ish function over an update dict rather than a framework
handler, so the same code path serves the production webhook and the
development poller, and tests drive it with plain dictionaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.store.ledger import MessageLedger, MessageStatus
from app.telegram import cards
from app.telegram.client import Sender


class NotAllowedError(PermissionError):
    """An update arrived from a chat that is not on the allowlist."""


class SessionLike(Protocol):
    """The slice of `GraphSession` this handler uses.

    A Protocol rather than the concrete class so tests can drive the whole flow
    without Postgres, Gmail, or Gemini -- `GraphSession` satisfies it
    structurally.
    """

    conn: Any

    def pending(self, message_id: str) -> dict[str, Any] | None: ...

    def resume(self, message_id: str, decision: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(slots=True)
class TelegramHandler:
    session: SessionLike
    bot: Sender
    allowed_chat_ids: frozenset[int]
    user_timezone: str

    def _check(self, chat_id: int) -> None:
        """Anyone who finds the bot username can message it, and this bot reads
        your email. An empty allowlist means nobody, which is the safe default."""
        if chat_id not in self.allowed_chat_ids:
            raise NotAllowedError(f"chat {chat_id} is not on the allowlist")

    def handle(self, update: dict[str, Any]) -> str:
        """Returns a short description of what happened, for logs."""
        if "callback_query" in update:
            return self._on_button(update["callback_query"])
        if "message" in update:
            return self._on_message(update["message"])
        return "ignored: no message or callback"

    # --- buttons ----------------------------------------------------------

    def _on_button(self, query: dict[str, Any]) -> str:
        message = query.get("message") or {}
        chat_id = int((message.get("chat") or {}).get("id", 0))
        self._check(chat_id)

        action, message_id = cards.parse_callback(query.get("data", ""))

        # Answer first. Telegram re-delivers an unanswered callback, which shows
        # up as a phantom second tap on a button that books calendar events.
        self.bot.answer_callback(query.get("id", ""))

        if action == cards.EDIT:
            self.bot.send_message(chat_id, cards.edit_prompt(message_id), force_reply=True)
            return f"{message_id}: awaiting correction"

        if action not in (cards.CONFIRM, cards.CANCEL):
            return f"ignored: unknown action {action!r}"

        return self._resume(chat_id, message_id, {"action": action})

    # --- free-text replies ------------------------------------------------

    def _on_message(self, message: dict[str, Any]) -> str:
        chat_id = int((message.get("chat") or {}).get("id", 0))
        self._check(chat_id)

        replied_to = (message.get("reply_to_message") or {}).get("text", "")
        message_id = cards.message_id_from_edit_prompt(replied_to)
        if message_id is None:
            self.bot.send_message(
                chat_id, "Nothing to do. Proposals arrive here with buttons attached."
            )
            return "ignored: not a correction reply"

        correction = (message.get("text") or "").strip()
        if not correction:
            return f"{message_id}: empty correction ignored"

        return self._resume(chat_id, message_id, {"action": cards.EDIT, "correction": correction})

    # --- graph ------------------------------------------------------------

    def _resume(self, chat_id: int, message_id: str, decision: dict[str, Any]) -> str:
        if self.session.pending(message_id) is None:
            self.bot.send_message(chat_id, "That proposal is no longer waiting for a decision.")
            return f"{message_id}: nothing pending"

        self.session.resume(message_id, decision)

        # An edit re-runs extraction and parks again, so send the new card
        # rather than an outcome.
        pending = self.session.pending(message_id)
        if pending is not None:
            self.bot.send_message(
                chat_id,
                cards.approval_card(pending, zone=self.user_timezone),
                keyboard=cards.keyboard(message_id),
            )
            return f"{message_id}: revised, awaiting approval"

        entry = MessageLedger(self.session.conn).get(message_id)
        status = entry.status if entry else MessageStatus.FAILED
        event_id = entry.calendar_event_id if entry else None

        self.bot.send_message(chat_id, cards.outcome_text(_to_action(status), event_id=event_id))
        return f"{message_id}: {status}"


def _to_action(status: MessageStatus) -> str:
    match status:
        case MessageStatus.CREATED:
            return "created"
        case MessageStatus.REJECTED:
            return "rejected"
        case MessageStatus.SKIPPED:
            return "dry_run"
        case _:
            return "failed"
