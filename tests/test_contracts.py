from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.contracts import ActionResult, EmailMessage, ExtractionResult


def _email() -> EmailMessage:
    return EmailMessage(
        id="18c0f2a",
        thread_id="18c0f2a",
        subject="Sync Tuesday?",
        body_text="Can we meet next Tuesday at 3?",
        sender="sara@example.com",
        recipients=["me@example.com"],
        received_at=datetime(2026, 8, 16, 9, 0, tzinfo=UTC),
    )


def test_email_message_defaults_recipients() -> None:
    msg = EmailMessage(
        id="a",
        thread_id="a",
        subject="s",
        body_text="b",
        sender="x@example.com",
        received_at=datetime(2026, 8, 16, tzinfo=UTC),
    )
    assert msg.recipients == []


def test_email_message_roundtrips() -> None:
    msg = _email()
    assert EmailMessage.model_validate(msg.model_dump()) == msg


def test_confidence_must_be_a_probability() -> None:
    for bad in (-0.1, 1.1):
        with pytest.raises(ValidationError):
            ExtractionResult(is_meeting=True, confidence=bad, reasoning="x")


def test_non_meeting_needs_no_event_fields() -> None:
    result = ExtractionResult(is_meeting=False, confidence=0.97, reasoning="Newsletter.")
    assert result.start_utc is None
    assert result.timezone is None
    assert result.attendees == []


def test_action_result_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        ActionResult(status="maybe")


def test_action_result_accepts_known_statuses() -> None:
    for status in ("created", "skipped_duplicate", "rejected", "failed"):
        assert ActionResult(status=status).status == status
