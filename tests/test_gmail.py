from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any

from app.google.gmail import extract_body, to_email_message


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
