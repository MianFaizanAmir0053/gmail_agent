"""Gmail reads.

Body extraction is deliberately a pure function (`extract_body`) so it can be
tested against recorded payloads without touching the network -- it is the part
most likely to be wrong, since real messages nest parts arbitrarily.
"""

from __future__ import annotations

import base64
import binascii
import html
import re
from datetime import UTC, datetime
from email.utils import getaddresses
from typing import Any, cast

from app.contracts import EmailMessage

_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\n\s*\n\s*\n+")


def _decode(data: str) -> str:
    """Decode Gmail's base64url payload. Returns "" on malformed input."""
    try:
        raw = base64.urlsafe_b64decode(data.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError, ValueError):
        return ""
    # Real mail is frequently mislabelled; never assume utf-8 decodes cleanly.
    return raw.decode("utf-8", errors="replace")


def _html_to_text(markup: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", markup)
    text = re.sub(r"(?i)<br\s*/?>|</p>", "\n", text)
    text = _TAG.sub("", text)
    # html.unescape rather than a hand-written table. The table handled five
    # named entities and left numeric ones alone, so transactional mail arrived
    # with literal `&#128206;` in the body -- which then went on to be embedded.
    text = html.unescape(text)
    return _WHITESPACE.sub("\n\n", text).strip()


def _walk(part: dict[str, Any], acc: dict[str, str]) -> None:
    mime = part.get("mimeType", "")
    data = part.get("body", {}).get("data")

    if data and mime in ("text/plain", "text/html") and mime not in acc:
        acc[mime] = _decode(data)

    for child in part.get("parts", []):
        _walk(cast(dict[str, Any], child), acc)


def extract_body(payload: dict[str, Any]) -> str:
    """Best-effort plain text from a Gmail payload.

    Prefers `text/plain`; falls back to stripped `text/html`. Returns "" when
    the message carries no textual part at all (attachment-only mail).
    """
    found: dict[str, str] = {}
    _walk(payload, found)

    if "text/plain" in found:
        return found["text/plain"].strip()
    if "text/html" in found:
        return _html_to_text(found["text/html"])
    return ""


def _header(payload: dict[str, Any], name: str) -> str:
    lowered = name.lower()
    for header in payload.get("headers", []):
        if header.get("name", "").lower() == lowered:
            return cast(str, header.get("value", ""))
    return ""


def _addresses(raw: str) -> list[str]:
    return [addr.lower() for _, addr in getaddresses([raw]) if addr]


def to_email_message(message: dict[str, Any]) -> EmailMessage:
    """Convert a Gmail `users.messages.get` response into our contract type."""
    payload = cast(dict[str, Any], message.get("payload", {}))

    # internalDate is epoch milliseconds, UTC, and set by Gmail itself -- more
    # reliable than the Date: header, which senders routinely get wrong.
    received_at = datetime.fromtimestamp(int(message["internalDate"]) / 1000, tz=UTC)

    sender = _addresses(_header(payload, "From"))
    recipients = _addresses(_header(payload, "To")) + _addresses(_header(payload, "Cc"))

    return EmailMessage(
        id=cast(str, message["id"]),
        thread_id=cast(str, message["threadId"]),
        subject=_header(payload, "Subject"),
        body_text=extract_body(payload),
        sender=sender[0] if sender else "",
        recipients=recipients,
        received_at=received_at,
    )


class GmailClient:
    def __init__(self, service: Any) -> None:
        self._service = service

    def list_unread(self, max_results: int = 10) -> list[str]:
        """Return unread message IDs, newest first."""
        response = cast(
            dict[str, Any],
            self._service.users()
            .messages()
            .list(userId="me", q="is:unread", maxResults=max_results)
            .execute(),
        )
        return [cast(str, m["id"]) for m in response.get("messages", [])]

    def search(self, query: str, limit: int = 200) -> list[str]:
        """Message IDs matching a Gmail search query, newest first.

        Paginated, unlike `list_unread`: retrieval ingestion walks months of
        history, and Gmail caps a single page at 500 regardless of what
        `maxResults` asks for. Stops at `limit` so a wide query cannot turn into
        an unbounded walk of the whole mailbox.
        """
        ids: list[str] = []
        page_token: str | None = None

        while len(ids) < limit:
            response = cast(
                dict[str, Any],
                self._service.users()
                .messages()
                .list(
                    userId="me",
                    q=query,
                    maxResults=min(500, limit - len(ids)),
                    pageToken=page_token,
                )
                .execute(),
            )
            ids.extend(cast(str, m["id"]) for m in response.get("messages", []))

            page_token = cast(str | None, response.get("nextPageToken"))
            if not page_token:
                break

        return ids[:limit]

    def get_message(self, message_id: str) -> EmailMessage:
        response = cast(
            dict[str, Any],
            self._service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute(),
        )
        return to_email_message(response)

    def current_history_id(self) -> str:
        """Mailbox history cursor. M04 seeds incremental sync from this."""
        profile = cast(dict[str, Any], self._service.users().getProfile(userId="me").execute())
        return cast(str, profile["historyId"])
