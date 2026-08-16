"""Rendering proposals for a human.

Times are shown in the recipient's own zone, never UTC. Everything downstream
stores UTC because that is the only sane way to compare instants -- but a person
approving "11:00" when they meant 16:00 is exactly the failure this whole
human-in-the-loop step exists to prevent, so the card converts back.
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

CONFIRM = "confirm"
CANCEL = "cancel"
EDIT = "edit"

EDIT_PROMPT_PREFIX = "Correction for"
"""The Edit reply-prompt carries the message ID in its text.

Correlating the user's free-text reply back to a proposal that way keeps the
handler stateless -- no pending-edit table, nothing to expire or leak.
"""


def _escape(value: object) -> str:
    return html.escape(str(value or ""))


def _local(value: str | datetime | None, zone: str) -> str:
    if value is None:
        return "?"
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    return moment.astimezone(ZoneInfo(zone)).strftime("%a %d %b, %H:%M")


def callback_data(action: str, message_id: str) -> str:
    """Telegram caps `callback_data` at 64 bytes.

    Gmail message IDs are ~16 characters, so `action:id` fits with room to
    spare -- but nothing longer should ever be smuggled in here.
    """
    data = f"{action}:{message_id}"
    if len(data.encode()) > 64:
        raise ValueError(f"callback_data too long ({len(data.encode())} bytes): {data!r}")
    return data


def parse_callback(data: str) -> tuple[str, str]:
    action, _, message_id = data.partition(":")
    return action, message_id


def keyboard(message_id: str) -> list[list[dict[str, str]]]:
    return [
        [
            {"text": "✅ Confirm", "callback_data": callback_data(CONFIRM, message_id)},
            {"text": "✏️ Edit", "callback_data": callback_data(EDIT, message_id)},
            {"text": "✖️ Cancel", "callback_data": callback_data(CANCEL, message_id)},
        ]
    ]


def approval_card(payload: dict[str, Any], *, zone: str) -> str:
    proposed = payload.get("proposed") or {}
    conflicts = payload.get("conflicts") or []

    lines = [
        f"<b>{_escape(proposed.get('title') or '(untitled)')}</b>",
        f"🕒 {_escape(_local(proposed.get('start_utc'), zone))}"
        f" - {_escape(_local(proposed.get('end_utc'), zone))}",
        f"🌍 {_escape(proposed.get('timezone') or zone)}",
    ]

    attendees = proposed.get("attendees") or []
    if attendees:
        lines.append(f"👥 {_escape(', '.join(attendees))}")
    if proposed.get("location"):
        lines.append(f"📍 {_escape(proposed['location'])}")

    confidence = proposed.get("confidence")
    if isinstance(confidence, int | float):
        lines.append(f"📊 confidence {confidence:.0%}")

    for conflict in conflicts:
        lines.append(f"\n⚠️ {_escape(conflict)}")

    if proposed.get("reasoning"):
        lines.append(f"\n<i>{_escape(proposed['reasoning'])}</i>")

    return "\n".join(lines)


def edit_prompt(message_id: str) -> str:
    return (
        f"{EDIT_PROMPT_PREFIX} {message_id}\n"
        'Reply to this message with what to change, e.g. "4pm not 3pm" '
        'or "add sara@example.com".'
    )


def message_id_from_edit_prompt(text: str) -> str | None:
    if not text.startswith(EDIT_PROMPT_PREFIX):
        return None
    remainder = text[len(EDIT_PROMPT_PREFIX) :].strip()
    return remainder.split()[0] if remainder else None


def outcome_text(status: str, *, event_id: str | None = None) -> str:
    match status:
        case "created":
            return f"✅ Added to your calendar (<code>{_escape(event_id)}</code>)."
        case "dry_run":
            return "🧪 Dry run — nothing written. Set <code>DRY_RUN=false</code> to book for real."
        case "rejected":
            return "✖️ Cancelled. Nothing was added."
        case "skipped_duplicate":
            return "⏭️ Skipped."
        case _:
            return "⚠️ Failed. Check the logs."
