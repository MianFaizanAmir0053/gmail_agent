"""Rendering proposals for a human, on Telegram.

Times are shown in the recipient's own zone, never UTC. Everything downstream
stores UTC because that is the only sane way to compare instants -- but a person
approving "11:00" when they meant 16:00 is exactly the failure this whole
human-in-the-loop step exists to prevent, so the card converts back.

Since M16 a card is drawn from the stored proposal record, never the raw
interrupt payload: the record carries only the card's fields, so the model's
reasoning -- which can quote the email -- never reaches a chat. Every button
carries the revision it was drawn for, so a tap on an out-of-date card is
refused rather than applied to a proposal the owner has not seen.

Since M17 the Confirm button also carries the card's token (the keyed hash's
prefix, the mode and the generation), which `decide()` holds the claim to. A
proposal with nothing to bind gets no Confirm button at all.
"""

from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

from app.channel.decide import card_token
from app.channel.park import ProposalRecord

CONFIRM = "confirm"
CANCEL = "cancel"
EDIT = "edit"

EDIT_PROMPT_PREFIX = "Correction for"
"""The Edit reply-prompt carries the message ID and revision in its text.

Correlating the user's free-text reply back to a proposal that way keeps the
handler stateless -- no pending-edit table, nothing to expire or leak.
"""

QUEUED = "⏳ Queued. The outcome shows in the web app."
STALE = "This card is out of date. The latest version is in the web app."
GONE = "That proposal is no longer waiting for a decision."
FROM_BEFORE_M16 = "This card predates the web app. Decide it there instead."
FROM_BEFORE_M17 = "This Confirm button predates bound approvals. Confirm it in the web app."
NOT_READY = "This proposal is still being prepared. Try again in a minute."


def _escape(value: object) -> str:
    return html.escape(str(value or ""))


def _local(value: str | datetime | None, zone: str) -> str:
    if value is None:
        return "?"
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    return moment.astimezone(ZoneInfo(zone)).strftime("%a %d %b, %H:%M")


def callback_data(action: str, message_id: str, revision: int, token: str | None = None) -> str:
    """Telegram caps `callback_data` at 64 bytes.

    Gmail message IDs are ~16 characters, so `action:revision:id` fits with
    room to spare, and so does a Confirm's `confirm:revision:token:id` -- but
    nothing longer should ever be smuggled in here.
    """
    data = (
        f"{action}:{revision}:{token}:{message_id}"
        if token
        else f"{action}:{revision}:{message_id}"
    )
    if len(data.encode()) > 64:
        raise ValueError(f"callback_data too long ({len(data.encode())} bytes): {data!r}")
    return data


def parse_callback(data: str) -> tuple[str, int | None, str | None, str]:
    """`(action, revision, token, message_id)`.

    A button sent before M16 has no revision, and a Confirm sent before M17 no
    token; neither ever contains a colon, which keeps the forms apart.
    """
    parts = data.split(":")
    if len(parts) == 4 and parts[1].isdigit():
        return parts[0], int(parts[1]), parts[2], parts[3]
    if len(parts) == 3 and parts[1].isdigit():
        return parts[0], int(parts[1]), None, parts[2]
    action, _, message_id = data.partition(":")
    return action, None, None, message_id


def record_token(record: ProposalRecord) -> str | None:
    """The token a card for `record` carries, or None when nothing can run."""
    if record.args_hash is None:
        return None
    return card_token(record.args_hash, record.dry_run, record.generation)


def keyboard(message_id: str, revision: int, token: str | None) -> list[list[dict[str, str]]]:
    """Confirm only with a token: a proposal with nothing to bind cannot be
    confirmed, and a button that always fails would only mislead."""
    row: list[dict[str, str]] = []
    if token is not None:
        row.append(
            {
                "text": "✅ Confirm",
                "callback_data": callback_data(CONFIRM, message_id, revision, token),
            }
        )
    row.append({"text": "✏️ Edit", "callback_data": callback_data(EDIT, message_id, revision)})
    row.append({"text": "✖️ Cancel", "callback_data": callback_data(CANCEL, message_id, revision)})
    return [row]


def approval_card(record: ProposalRecord, *, zone: str) -> str:
    card = record.payload
    lines = [
        f"<b>{_escape(card.get('title') or '(untitled)')}</b>",
        f"🕒 {_escape(_local(card.get('start_utc'), zone))}"
        f" - {_escape(_local(card.get('end_utc'), zone))}",
        f"🌍 {_escape(card.get('timezone') or zone)}",
    ]

    attendees = card.get("attendees") or []
    if attendees:
        lines.append(f"👥 {_escape(', '.join(attendees))}")
    if card.get("location"):
        lines.append(f"📍 {_escape(card['location'])}")

    for conflict in card.get("conflicts") or []:
        lines.append(f"\n⚠️ {_escape(conflict)}")
    for issue in card.get("review_issues") or []:
        lines.append(f"\n🔎 {_escape(issue)}")

    status = f"revision {record.revision} · {'dry run' if record.dry_run else 'live'}"
    lines.append(f"\n<i>{status}</i>")
    return "\n".join(lines)


def edit_prompt(message_id: str, revision: int) -> str:
    return (
        f"{EDIT_PROMPT_PREFIX} {message_id} r{revision}\n"
        'Reply to this message with what to change, e.g. "4pm not 3pm" '
        'or "add sara@example.com".'
    )


def edit_target(text: str) -> tuple[str, int | None] | None:
    """`(message_id, revision)` from an edit prompt, or None for any other text.

    A prompt sent before M16 carries no revision.
    """
    if not text.startswith(EDIT_PROMPT_PREFIX):
        return None
    words = text[len(EDIT_PROMPT_PREFIX) :].split()
    if not words:
        return None
    message_id = words[0]
    tag = words[1] if len(words) > 1 else ""
    if tag.startswith("r") and tag[1:].isdigit():
        return message_id, int(tag[1:])
    return message_id, None
