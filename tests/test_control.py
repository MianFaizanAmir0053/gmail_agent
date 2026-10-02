"""The owner's switches (M17, D6)."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast

import psycopg
import pytest

from app.channel.decide import decide
from app.channel.park import proposal_from, write_park
from app.config import Settings
from app.jobs import control as cli
from app.policy import control
from app.policy.control import Control
from app.store.ledger import MessageLedger, MessageStatus


@pytest.mark.integration
def test_pause_and_resume_are_each_audited_once(conn: psycopg.Connection) -> None:
    conn.execute("UPDATE control SET paused = false")
    row = conn.execute("SELECT coalesce(max(id), 0) FROM audit_log").fetchone()
    assert row is not None

    assert control.switch(conn, paused=True, via="web") is True
    assert control.switch(conn, paused=True, via="cli") is False  # already paused
    state = control.read(conn)
    assert (state.paused, state.changed_via) == (True, "web")

    assert control.switch(conn, paused=False, via="cli") is True
    assert control.switch(conn, paused=False, via="web") is False
    assert control.is_paused(conn) is False

    kinds = conn.execute("SELECT kind FROM audit_log WHERE id > %s ORDER BY id", (row[0],))
    assert kinds.fetchall() == [("paused",), ("resumed",)]


@pytest.mark.integration
def test_pause_and_resume_say_where_they_came_from(conn: psycopg.Connection) -> None:
    """`changed_via` keeps only the last change: the audit log keeps each."""
    conn.execute("UPDATE control SET paused = false")

    control.switch(conn, paused=True, via="web")
    control.switch(conn, paused=False, via="cli")

    rows = conn.execute(
        "SELECT kind, reason FROM audit_log WHERE kind IN ('paused', 'resumed')"
        " ORDER BY id DESC LIMIT 2"
    ).fetchall()
    assert rows == [("resumed", "from the command line"), ("paused", "from the web app")]


def _queued(conn: psycopg.Connection, message_id: str) -> int:
    """A Cancel waiting for the worker."""
    MessageLedger(conn).claim(message_id, message_id)
    pending = {"proposed": {"title": "Design review", "attendees": []}, "dry_run": True}
    with conn.transaction():
        write_park(conn, proposal_from(message_id, pending, 1), ledger_status=MessageStatus.CLAIMED)
    queued = decide(conn, message_id, action="cancel", revision=1, via="web")
    assert queued.decision_id is not None
    return queued.decision_id


@pytest.mark.integration
def test_resume_moves_each_held_decision_on_by_the_length_of_the_pause(
    conn: psycopg.Connection,
) -> None:
    """One due fifty minutes before the pause is still fifty minutes overdue,
    so the stuck clock still counts it, and a quick pause and resume cannot
    hide it. One that fell due during the pause is due from the Resume."""
    early, late = _queued(conn, "m1"), _queued(conn, "m2")
    conn.execute("UPDATE control SET paused = true, changed_at = now() - interval '2 hours'")
    conn.execute(
        "UPDATE decisions SET next_attempt_at = now() - interval '2 hours 50 minutes'"
        " WHERE id = %s",
        (early,),
    )
    conn.execute(
        "UPDATE decisions SET next_attempt_at = now() - interval '90 minutes' WHERE id = %s",
        (late,),
    )

    control.switch(conn, paused=False, via="web")

    overdue: dict[int, Any] = dict(
        conn.execute(
            "SELECT id, EXTRACT(EPOCH FROM now() - next_attempt_at) FROM decisions"
        ).fetchall()
    )
    assert float(overdue[early]) == pytest.approx(50 * 60)
    assert float(overdue[late]) == pytest.approx(0)


def _cli(
    monkeypatch: pytest.MonkeyPatch,
    *argv: str,
    database_url: str = "postgresql://x/y",
    changed_at: datetime = datetime(2026, 10, 2, 9, 0, tzinfo=UTC),
) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    def switch(conn: object, *, paused: bool, via: str) -> bool:
        calls.append(("pause" if paused else "resume", via))
        return paused  # pausing changes something; resuming finds it not paused

    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: Settings(_env_file=None, database_url=database_url, gemini_api_key="k"),
    )
    monkeypatch.setattr(cli, "connect_autocommit", lambda url: nullcontext(object()))
    monkeypatch.setattr(control, "switch", switch)
    monkeypatch.setattr(control, "read", lambda conn: Control(True, changed_at, "cli", "warning"))
    monkeypatch.setattr("sys.argv", ["control", *argv])
    cli.main()
    return calls


def test_the_command_line_names_the_database_it_switched(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`.env` may name the development database while production runs on:
    the output says which host was switched, and never the password."""
    _cli(monkeypatch, "pause", database_url="postgresql://owner:hunter2@db.example.com:5432/app")

    out = capsys.readouterr().out
    assert "db.example.com" in out
    assert "hunter2" not in out


def test_the_command_line_prints_utc_whatever_the_sessions_zone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    karachi = timezone(timedelta(hours=5))
    _cli(monkeypatch, "status", changed_at=datetime(2026, 10, 2, 14, 0, tzinfo=karachi))

    assert "2026-10-02 09:00 UTC" in capsys.readouterr().out


class NoControlRow:
    """A database whose `control` row is missing: the migrations never ran."""

    def execute(self, *args: object, **kwargs: object) -> NoControlRow:
        return self

    def fetchone(self) -> None:
        return None

    def transaction(self) -> nullcontext[None]:
        return nullcontext()


def test_a_missing_control_row_is_an_error_not_a_running_agent() -> None:
    """Read as "not paused", it would let everything run, and Pause would
    answer that it had worked."""
    with pytest.raises(RuntimeError, match="control row"):
        control.is_paused(cast(Any, NoControlRow()))
    with pytest.raises(RuntimeError, match="control row"):
        control.switch(cast(Any, NoControlRow()), paused=True, via="web")


def test_the_command_line_pauses_as_the_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(monkeypatch, "pause") == [("pause", "cli")]

    out = capsys.readouterr().out
    assert "Paused." in out and "paused: yes" in out and "model spending: warning" in out


def test_resuming_an_agent_not_paused_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(monkeypatch, "resume") == [("resume", "cli")]
    assert "Not paused." in capsys.readouterr().out


def test_status_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(monkeypatch, "status") == []
    assert "paused: yes" in capsys.readouterr().out
