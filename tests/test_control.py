"""The owner's switches (M17, D6)."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime

import psycopg
import pytest

from app.config import Settings
from app.jobs import control as cli
from app.policy import control
from app.policy.control import Control


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


def _cli(monkeypatch: pytest.MonkeyPatch, *argv: str) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    def switch(conn: object, *, paused: bool, via: str) -> bool:
        calls.append(("pause" if paused else "resume", via))
        return paused  # pausing changes something; resuming finds it not paused

    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: Settings(_env_file=None, database_url="postgresql://x/y", gemini_api_key="k"),
    )
    monkeypatch.setattr(cli, "connect_autocommit", lambda url: nullcontext(object()))
    monkeypatch.setattr(control, "switch", switch)
    monkeypatch.setattr(
        control,
        "read",
        lambda conn: Control(True, datetime(2026, 10, 2, 9, 0, tzinfo=UTC), "cli", "warning"),
    )
    monkeypatch.setattr("sys.argv", ["control", *argv])
    cli.main()
    return calls


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
