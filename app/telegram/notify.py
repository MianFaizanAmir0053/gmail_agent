"""Pushing proposals out to a human."""

from __future__ import annotations

from typing import Any

from app.telegram import cards
from app.telegram.client import Sender


def send_approval_card(
    bot: Sender, chat_id: int, message_id: str, payload: dict[str, Any], *, zone: str
) -> None:
    bot.send_message(
        chat_id,
        cards.approval_card(payload, zone=zone),
        keyboard=cards.keyboard(message_id),
    )


def admin_chat_id(allowed_chat_ids: list[int]) -> int | None:
    """Where unsolicited proposals go.

    The first allowlisted chat, rather than a separate setting: a second ID that
    must be kept in sync with the allowlist is a way to end up sending
    proposals to a chat that is not permitted to answer them.
    """
    return allowed_chat_ids[0] if allowed_chat_ids else None
