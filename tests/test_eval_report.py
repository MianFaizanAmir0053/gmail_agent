"""Persistence of eval results, and the guard against publishing a broken run."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from app.contracts import EmailMessage, ExtractionResult
from app.eval.dataset import Fixture
from app.eval.publish import publish
from app.eval.report import save, to_dict
from app.eval.scorer import EvalReport, score

NOW = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
START = datetime(2026, 8, 19, 11, 0, tzinfo=UTC)

QUOTA_ERROR = ["fx-004: ClientError: 429 RESOURCE_EXHAUSTED"]


def _report() -> EvalReport:
    expected = ExtractionResult(
        is_meeting=True,
        title="Design review",
        start_utc=START,
        end_utc=START + timedelta(hours=1),
        timezone="Asia/Karachi",
        attendees=["sara@example.com"],
        confidence=0.9,
        reasoning="",
    )
    fixture = Fixture(
        id="fx-001",
        now_utc=NOW,
        user_timezone="Asia/Karachi",
        email=EmailMessage(
            id="fx-001",
            thread_id="fx-001",
            subject="s",
            body_text="b",
            sender="sara@example.com",
            recipients=["me@example.com"],
            received_at=NOW,
        ),
        expected=expected,
    )
    return score([fixture], [expected])


def test_a_clean_run_is_marked_trustworthy() -> None:
    data = to_dict(_report(), extractor="gemini")

    assert data["trustworthy"] is True
    assert data["errors"] == []


def test_errors_are_recorded_not_only_printed() -> None:
    """They used to live on the terminal only, so the saved file looked legitimate."""
    data = to_dict(_report(), extractor="gemini", errors=QUOTA_ERROR)

    assert data["trustworthy"] is False
    assert data["errors"] == QUOTA_ERROR


def test_an_invalid_run_says_so_in_its_filename(tmp_path: Path) -> None:
    """Visible from a directory listing, not only from opening the file."""
    path = save(_report(), extractor="gemini", directory=tmp_path, errors=QUOTA_ERROR)

    assert "INVALID" in path.name


def test_a_clean_run_keeps_the_ordinary_name(tmp_path: Path) -> None:
    path = save(_report(), extractor="gemini", directory=tmp_path)

    assert "INVALID" not in path.name


@pytest.mark.integration
def test_publish_refuses_a_run_that_errored(conn: psycopg.Connection, tmp_path: Path) -> None:
    """Otherwise a quota outage is drawn on the accuracy chart as a regression."""
    payload = to_dict(_report(), extractor="gemini", errors=QUOTA_ERROR)
    path = tmp_path / "eval-INVALID-gemini-x.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    published, skipped = publish(conn, tmp_path)

    assert published == 0
    assert skipped == 1


@pytest.mark.integration
def test_publish_accepts_a_clean_run(conn: psycopg.Connection, tmp_path: Path) -> None:
    # `publish` commits, and it keys on filename -- so a fixed name would publish
    # once and then be a duplicate for every later run against the same database.
    name = f"eval-gemini-{uuid4().hex}.json"
    payload = to_dict(_report(), extractor="gemini")
    (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")

    try:
        published, _ = publish(conn, tmp_path)
        assert published == 1
    finally:
        conn.execute("DELETE FROM eval_runs WHERE source_file = %s", (name,))
        conn.commit()


@pytest.mark.integration
def test_publish_ignores_files_that_are_not_eval_results(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    (tmp_path / "notes.json").write_text(json.dumps({"hello": "world"}), encoding="utf-8")

    published, skipped = publish(conn, tmp_path)

    assert (published, skipped) == (0, 1)
