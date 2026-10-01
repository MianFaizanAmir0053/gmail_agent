"""`python -m app.mail.sync` (M20, D8), against a fake mailbox and a real Postgres."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fake_gmail import SECRET, FakeMailbox

from app.config import Settings
from app.google.gmail import GmailClient
from app.mail import feed, quota
from app.mail import sync as cli
from app.mail.quota import Pacer
from app.store.ledger import MessageLedger, MessageStatus

pytestmark = pytest.mark.integration

ME = "me@example.com"
NOW = datetime.now(UTC).replace(microsecond=0)
PRIMARY = {"INBOX", "UNREAD", "CATEGORY_PERSONAL"}


@pytest.fixture
def mail(conn: psycopg.Connection) -> Iterator[psycopg.Connection]:
    for table in ("gmail_messages", "gmail_cursors", "gmail_fetch_queue"):
        conn.execute(f"DELETE FROM {table}")
    yield conn


def _client(box: FakeMailbox) -> GmailClient:
    return GmailClient(box, pacer=Pacer(), share=quota.SYNC, retry_for=0)


def _synced(conn: psycopg.Connection, box: FakeMailbox) -> None:
    """One run from a fresh database: the cursor at the mailbox's present."""
    cli.sync_once(conn, _client(box), owners=(ME,), now=lambda: NOW)


@pytest.fixture
def run_main(
    mail: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Any:
    """`main`, on the test's connection and a fake mailbox."""
    box = FakeMailbox()
    base: dict[str, Any] = {
        "_env_file": None,
        "database_url": "postgresql://unused",
        "gemini_api_key": "test-key",
    }
    settings = Settings(**base)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "_connect", lambda url: nullcontext(mail))
    monkeypatch.setattr(cli, "sync_client", lambda settings: _client(box))

    def run(*argv: str) -> tuple[int, str]:
        code = cli.main(list(argv))
        return code, capsys.readouterr().out

    run.box = box  # type: ignore[attr-defined]
    return run


def test_once_runs_one_pass(run_main: Any, mail: psycopg.Connection) -> None:
    run_main.box.deliver("m1", labels=PRIMARY, at=NOW)

    code, out = run_main("--once")

    assert code == 0
    assert "reached the end of history" in out
    assert cli.load_cursor(mail, ME) is not None


def test_status_shows_the_records_as_counts_and_times(
    run_main: Any, mail: psycopg.Connection
) -> None:
    run_main.box.deliver("m1", labels=PRIMARY, at=NOW)
    run_main("--once")
    run_main.box.deliver("m2", labels={"SENT"}, at=NOW, sender=ME, to="sara@example.com")
    run_main("--once")
    MessageLedger(mail).claim("old", "old")
    MessageLedger(mail).mark("old", MessageStatus.SKIPPED, error=feed.TOO_OLD)

    code, out = run_main("--status")

    assert code == 0
    assert ME in out
    assert "by direction: in 1, out 1" in out
    assert "by category: primary 2" in out
    assert "by arrived_via: history 1, switch_over 1" in out or "history 2" in out
    assert "Latest too-old records: 1" in out and "old" in out
    assert SECRET not in out


def test_status_before_any_run_says_so(run_main: Any) -> None:
    code, out = run_main("--status")

    assert code == 0
    assert "No sync has run yet" in out


def test_show_prints_one_rows_metadata_and_no_content(
    run_main: Any, mail: psycopg.Connection
) -> None:
    run_main.box.deliver("m1", labels=PRIMARY, at=NOW)
    run_main("--once")
    MessageLedger(mail).claim("m1", "m1")
    MessageLedger(mail).mark("m1", MessageStatus.SKIPPED, error="the model's words about it")

    code, out = run_main("--show", "m1")

    assert code == 0
    assert "direction: in" in out and "category: primary" in out
    assert "ledger: skipped" in out
    assert "model's words" not in out and SECRET not in out
    assert run_main("--show", "nope")[0] == 1


def test_catch_up_records_a_gap_and_the_next_runs_work_it_off(
    run_main: Any, mail: psycopg.Connection
) -> None:
    box = run_main.box
    box.deliver("m1", labels=PRIMARY, at=NOW - timedelta(minutes=30))
    run_main("--once")
    box.messages["m1"].labels |= {"TRASH"}  # trashed during the gap, no history kept

    code, out = run_main("--catch-up")

    assert code == 0 and "Recorded a gap" in out
    cursor = cli.load_cursor(mail, ME)
    assert cursor is not None and cursor.gap_from is not None
    assert "m1" not in feed.candidates(mail, 10)  # held for its re-fetch

    run_main("--once")

    cursor = cli.load_cursor(mail, ME)
    assert cursor is not None and cursor.gap_from is None
    row = mail.execute("SELECT label_ids FROM gmail_messages WHERE message_id = 'm1'").fetchone()
    assert row is not None and "TRASH" in row[0]
    assert "m1" not in feed.candidates(mail, 10)  # never fed


def _unreadable(conn: psycopg.Connection, message_id: str, queued_ago: str) -> None:
    conn.execute(
        """
        INSERT INTO gmail_fetch_queue (message_id, reason, queued_at, strikes, status, failed_at)
        VALUES (%s, 'fetch_failed', now() - %s::interval, 5, 'unreadable', now())
        """,
        (message_id, queued_ago),
    )


def _queue(conn: psycopg.Connection) -> dict[str, tuple[int, str]]:
    rows = conn.execute("SELECT message_id, strikes, status FROM gmail_fetch_queue").fetchall()
    return {row[0]: (row[1], row[2]) for row in rows}


def test_retry_unreadable_queues_them_again_with_no_strikes(
    run_main: Any, mail: psycopg.Connection
) -> None:
    """Five strikes were for ever: nothing, not even a catch-up, queued an
    unreadable message again. Within the backfill's reach, this does."""
    run_main("--once")
    _unreadable(mail, "recent", "2 days")
    _unreadable(mail, "ancient", "200 days")  # queued before the backfill's floor

    code, out = run_main("--retry-unreadable")

    assert code == 0 and "1 unreadable message(s) queued again" in out
    assert _queue(mail) == {"recent": (0, "queued"), "ancient": (5, "unreadable")}


def test_a_catch_up_queues_unreadable_mail_again_too(
    run_main: Any, mail: psycopg.Connection
) -> None:
    run_main("--once")
    _unreadable(mail, "stuck", "1 hour")

    code, out = run_main("--catch-up")

    assert code == 0 and "1 unreadable message(s) queued again" in out
    assert _queue(mail)["stuck"] == (0, "queued")


def test_check_feed_passes_on_a_clean_switch_over(run_main: Any, mail: psycopg.Connection) -> None:
    run_main.box.deliver("m1", labels=PRIMARY, at=NOW)
    run_main.box.deliver("sent", labels={"SENT"}, at=NOW, sender=ME, to="sara@example.com")
    run_main("--once")
    MessageLedger(mail).claim("m1", "m1")

    code, out = run_main("--check-feed")

    assert code == 0
    assert out.count("none (good)") == 3
    assert "m1  processed" in out
    assert "sent  left out by the feed's rule" in out


def test_check_feed_asks_gmail_for_the_switch_over_hour(
    run_main: Any, mail: psycopg.Connection
) -> None:
    """Reading only stored rows, it could never fail for mail the sync
    missed: Gmail's listing is what the stored rows are checked against."""
    run_main.box.deliver("missed", labels=PRIMARY, at=NOW)
    run_main("--once")
    mail.execute("DELETE FROM gmail_messages WHERE message_id = 'missed'")  # as if never stored

    code, out = run_main("--check-feed")

    assert code == 1
    assert "never stored: missed" in out


def test_check_feed_says_why_switch_over_mail_is_held(
    run_main: Any, mail: psycopg.Connection
) -> None:
    run_main.box.deliver("held", labels=PRIMARY, at=NOW)
    run_main("--once")
    mail.execute(
        "INSERT INTO gmail_fetch_queue (message_id, reason, strikes) VALUES ('held', 'refetch', 1)"
    )

    code, out = run_main("--check-feed")

    assert code == 0
    assert "held  held: queued for a fetch (refetch, 1 strike(s))" in out


def test_check_feed_finds_older_mail_processed_after_the_switch_over(
    run_main: Any, mail: psycopg.Connection
) -> None:
    run_main.box.put("old", labels=PRIMARY, at=NOW - timedelta(days=3))
    run_main("--once")  # the backfill stores it
    MessageLedger(mail).claim("old", "old")  # ...and something fed it

    code, out = run_main("--check-feed")

    assert code == 1
    assert "processed after the switch-over: old" in out


def test_check_feed_finds_switch_over_mail_left_unprocessed(
    run_main: Any, mail: psycopg.Connection
) -> None:
    run_main.box.deliver("waiting", labels=PRIMARY, at=NOW)
    run_main("--once")

    code, out = run_main("--check-feed")

    assert code == 1
    assert "neither processed nor recorded: waiting" in out
