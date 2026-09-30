from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.google.gmail import (
    METADATA_HEADERS,
    GmailClient,
    extract_body,
    to_email_message,
    to_message_meta,
)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _part(mime: str, text: str) -> dict[str, Any]:
    return {"mimeType": mime, "body": {"data": _b64(text)}}


def test_extracts_simple_plain_text() -> None:
    assert extract_body(_part("text/plain", "Can we meet Tuesday?")) == "Can we meet Tuesday?"


def test_prefers_plain_text_over_html() -> None:
    payload: dict[str, Any] = {
        "mimeType": "multipart/alternative",
        "parts": [_part("text/plain", "plain wins"), _part("text/html", "<p>html loses</p>")],
    }
    assert extract_body(payload) == "plain wins"


def test_falls_back_to_html_when_no_plain_part() -> None:
    payload: dict[str, Any] = {
        "mimeType": "multipart/alternative",
        "parts": [_part("text/html", "<p>Meet at <b>3pm</b></p>")],
    }
    assert extract_body(payload) == "Meet at 3pm"


def test_html_fallback_drops_script_and_style() -> None:
    html = "<style>p{color:red}</style><p>Real text</p><script>alert(1)</script>"
    payload: dict[str, Any] = {"parts": [_part("text/html", html)]}
    body = extract_body(payload)
    assert "Real text" in body
    assert "alert" not in body
    assert "color:red" not in body


def test_numeric_and_named_entities_are_decoded() -> None:
    """A hand-written table covered five named entities and left `&#128206;`
    sitting in the body, where it went on to be embedded verbatim."""
    payload: dict[str, Any] = {
        "parts": [_part("text/html", "<p>&#128206; Attached &amp; signed &#8212; done</p>")]
    }
    assert extract_body(payload) == "\N{PAPERCLIP} Attached & signed \N{EM DASH} done"


def test_finds_deeply_nested_parts() -> None:
    payload: dict[str, Any] = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "application/pdf", "body": {"attachmentId": "x"}},
            {"mimeType": "multipart/alternative", "parts": [_part("text/plain", "buried")]},
        ],
    }
    assert extract_body(payload) == "buried"


def test_attachment_only_message_yields_empty_body() -> None:
    payload: dict[str, Any] = {
        "mimeType": "multipart/mixed",
        "parts": [{"mimeType": "image/png", "body": {"attachmentId": "x"}}],
    }
    assert extract_body(payload) == ""


def test_malformed_base64_does_not_raise() -> None:
    payload: dict[str, Any] = {"mimeType": "text/plain", "body": {"data": "!!!not-base64!!!"}}
    assert extract_body(payload) == ""


def test_undecodable_bytes_are_replaced_not_fatal() -> None:
    raw = base64.urlsafe_b64encode(b"caf\xe9 meeting").decode()  # latin-1 in a utf-8 world
    payload: dict[str, Any] = {"mimeType": "text/plain", "body": {"data": raw}}
    assert "meeting" in extract_body(payload)


def test_to_email_message_maps_headers_and_time() -> None:
    message: dict[str, Any] = {
        "id": "18c0f2a",
        "threadId": "18c0f2a",
        "internalDate": "1786000000000",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": "Sync Tuesday?"},
                {"name": "From", "value": "Sara Ahmed <Sara@Example.com>"},
                {"name": "To", "value": "me@example.com"},
                {"name": "Cc", "value": "Bilal <bilal@example.com>, ops@example.com"},
            ],
            "body": {"data": _b64("Can we meet next Tuesday at 3?")},
        },
    }

    email = to_email_message(message)

    assert email.id == "18c0f2a"
    assert email.subject == "Sync Tuesday?"
    assert email.sender == "sara@example.com"  # normalised to lowercase
    assert email.recipients == ["me@example.com", "bilal@example.com", "ops@example.com"]
    assert email.body_text == "Can we meet next Tuesday at 3?"
    assert email.received_at.tzinfo is not None
    assert email.received_at == datetime.fromtimestamp(1786000000, tz=UTC)


def test_missing_headers_do_not_raise() -> None:
    message: dict[str, Any] = {
        "id": "a",
        "threadId": "a",
        "internalDate": "0",
        "payload": {"mimeType": "text/plain", "body": {"data": _b64("hi")}},
    }
    email = to_email_message(message)
    assert email.subject == ""
    assert email.sender == ""
    assert email.recipients == []


# --- metadata-only reads (M15) ---------------------------------------------


@dataclass
class _Request:
    result: dict[str, Any]

    def execute(self) -> dict[str, Any]:
        return self.result


@dataclass
class _Threads:
    pages: list[dict[str, Any]]
    thread: dict[str, Any]
    list_calls: list[dict[str, Any]] = field(default_factory=list)
    get_calls: list[dict[str, Any]] = field(default_factory=list)

    def list(self, **kwargs: Any) -> _Request:
        self.list_calls.append(kwargs)
        return _Request(self.pages[len(self.list_calls) - 1])

    def get(self, **kwargs: Any) -> _Request:
        self.get_calls.append(kwargs)
        return _Request(self.thread)


@dataclass
class _Users:
    threads_api: _Threads
    profile: dict[str, Any]

    def threads(self) -> _Threads:
        return self.threads_api

    def getProfile(self, **kwargs: Any) -> _Request:  # noqa: N802 -- Google's name
        return _Request(self.profile)


@dataclass
class _Service:
    users_api: _Users

    def users(self) -> _Users:
        return self.users_api


def _client(
    pages: list[dict[str, Any]] | None = None, thread: dict[str, Any] | None = None
) -> tuple[GmailClient, _Threads]:
    threads = _Threads(pages=pages or [{}], thread=thread or {"messages": []})
    service = _Service(_Users(threads, {"emailAddress": "Me.Owner@Gmail.com", "historyId": "1"}))
    return GmailClient(service), threads


def test_thread_listing_follows_every_page() -> None:
    """A capped walk would quietly undercount a busy fortnight."""
    client, threads = _client(
        pages=[
            {"threads": [{"id": "t1"}, {"id": "t2"}], "nextPageToken": "p2"},
            {"threads": [{"id": "t3"}], "nextPageToken": "p3"},
            {"threads": [{"id": "t4"}]},
        ]
    )

    assert client.thread_ids("after:1 before:2") == ["t1", "t2", "t3", "t4"]
    assert [call.get("pageToken") for call in threads.list_calls] == [None, "p2", "p3"]


def test_thread_metadata_never_asks_for_bodies() -> None:
    client, threads = _client(thread={"messages": []})

    client.thread_metadata("t1")

    assert threads.get_calls[0]["format"] == "metadata"
    assert list(threads.get_calls[0]["metadataHeaders"]) == list(METADATA_HEADERS)


def test_message_meta_keeps_only_the_named_headers() -> None:
    message = {
        "id": "m1",
        "threadId": "t1",
        "labelIds": ["INBOX", "CATEGORY_PERSONAL"],
        "internalDate": str(int(datetime(2026, 10, 6, 9, 30, tzinfo=UTC).timestamp() * 1000)),
        "payload": {
            "headers": [
                {"name": "From", "value": "Sara <sara@example.com>"},
                {"name": "DKIM-Signature", "value": "v=1; a=rsa-sha256"},
                {"name": "Subject", "value": "Contract"},
                {"name": "Auto-Submitted", "value": "no"},
            ]
        },
    }

    meta = to_message_meta(message)

    assert meta.internal_date == datetime(2026, 10, 6, 9, 30, tzinfo=UTC)
    assert meta.label_ids == frozenset({"INBOX", "CATEGORY_PERSONAL"})
    assert dict(meta.headers) == {
        "From": "Sara <sara@example.com>",
        "Subject": "Contract",
        "Auto-Submitted": "no",
    }


def test_the_profile_address_is_normalised_to_lower_case() -> None:
    client, _ = _client()
    assert client.profile_address() == "me.owner@gmail.com"
