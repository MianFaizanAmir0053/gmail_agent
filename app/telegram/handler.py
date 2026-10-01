"""Turning a Telegram update into a recorded decision.

Written as a pure-ish function over an update dict rather than a framework
handler, so the same code path serves the production webhook and the
development poller, and tests drive it with plain dictionaries.

Since M16 this records decisions through `decide()` and answers "Queued"; it
never resumes a thread. The worker applies the decision, and a re-park after
an edit comes back as a new card through the park step's announcement.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.channel.decide import Action, decide
from app.telegram import cards
from app.telegram.client import Sender


class NotAllowedError(PermissionError):
    """An update arrived from a chat that is not on the allowlist."""


def _nothing() -> None:
    return None


@dataclass(slots=True)
class TelegramHandler:
    conn: Any
    """A connection that commits each statement: the worker must see the
    decision at once (`app.store.db.connect_autocommit`)."""

    bot: Sender
    allowed_chat_ids: frozenset[int]
    on_queued: Callable[[], None] = field(default=_nothing)
    """Called after a decision is recorded -- to wake the worker in this process."""
    dry_run: bool = True
    """This process's `DRY_RUN`. A Confirm is held to it (M17, D2); left at the
    default by mistake, a live proposal is refused rather than run."""

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

        action, revision, token, message_id = cards.parse_callback(query.get("data", ""))

        # Answer first. Telegram re-delivers an unanswered callback, which shows
        # up as a phantom second tap on a button that books calendar events.
        self.bot.answer_callback(query.get("id", ""))

        if action not in (cards.CONFIRM, cards.CANCEL, cards.EDIT):
            return f"ignored: unknown action {action!r}"

        if revision is None:
            # Without a revision there is no telling which version it approves.
            self.bot.send_message(chat_id, cards.FROM_BEFORE_M16)
            return f"{message_id}: card without a revision refused"

        if action == cards.EDIT:
            self.bot.send_message(
                chat_id, cards.edit_prompt(message_id, revision), force_reply=True
            )
            return f"{message_id}: awaiting correction"

        if action == cards.CONFIRM and token is None:
            # It binds nothing, so it confirms nothing (M17, D2).
            self.bot.send_message(chat_id, cards.FROM_BEFORE_M17)
            return f"{message_id}: Confirm without a token refused"

        return self._decide(chat_id, message_id, action, revision, token=token)

    # --- free-text replies ------------------------------------------------

    def _on_message(self, message: dict[str, Any]) -> str:
        chat_id = int((message.get("chat") or {}).get("id", 0))
        self._check(chat_id)

        replied_to = (message.get("reply_to_message") or {}).get("text", "")
        target = cards.edit_target(replied_to)
        if target is None:
            self.bot.send_message(
                chat_id, "Nothing to do. Proposals arrive here with buttons attached."
            )
            return "ignored: not a correction reply"

        message_id, revision = target
        if revision is None:
            self.bot.send_message(chat_id, cards.FROM_BEFORE_M16)
            return f"{message_id}: correction to a card without a revision refused"

        correction = (message.get("text") or "").strip()
        if not correction:
            return f"{message_id}: empty correction ignored"

        return self._decide(chat_id, message_id, cards.EDIT, revision, correction)

    # --- the queue --------------------------------------------------------

    def _decide(
        self,
        chat_id: int,
        message_id: str,
        action: str,
        revision: int,
        correction: str = "",
        *,
        token: str | None = None,
    ) -> str:
        if action == cards.CONFIRM:
            result = decide(
                self.conn,
                message_id,
                action="confirm",
                revision=revision,
                via="telegram",
                token=token,
                dry_run=self.dry_run,
            )
        else:
            result = decide(
                self.conn,
                message_id,
                action=_action(action),
                revision=revision,
                correction=correction,
                via="telegram",
            )
        match result.status:
            case "queued":
                self.on_queued()
                self.bot.send_message(chat_id, cards.QUEUED)
            case "stale":
                self.bot.send_message(chat_id, cards.STALE)
            case "not_found":
                self.bot.send_message(chat_id, cards.GONE)
            case "not_ready":
                self.bot.send_message(chat_id, cards.NOT_READY)
            case _:
                self.bot.send_message(chat_id, f"Not accepted: {result.detail}.")
        return f"{message_id}: {result.status}"


def _action(value: str) -> Action:
    match value:
        case "confirm" | "cancel" | "edit":
            return value
    raise ValueError(f"not a Telegram action: {value!r}")
