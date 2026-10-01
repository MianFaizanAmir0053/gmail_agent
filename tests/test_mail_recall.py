"""Recall, of the sync and of the feed (M20, D5), against a fake mailbox and a
real Postgres.

The checks run at a NOW long before any real row: the audit log is shared
and append-only, and whatever M17's own work writes to it today must not
look like a pause in these windows.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fake_gmail import FakeMailbox

from app.google.gmail import GmailClient
from app.mail import feed
from app.mail.quota import Pacer
from app.mail.recall import RecallResult, held_intervals, recall, send_alerts
from app.store.ledger import MessageLedger, MessageStatus

pytestmark = pytest.mark.integration

ME = "me@example.com"
NOW = datetime(2020, 3, 10, 12, 0, tzinfo=UTC)
PRIMARY = {"INBOX", "CATEGORY_PERSONAL"}


@pytest.fixture
def mail(conn: psycopg.Connection) -> Iterator[psycopg.Connection]:
    for table in ("gmail_messages", "gmail_cursors", "gmail_fetch_queue", "alerts_sent"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute(
        """
        INSERT INTO gmail_cursors (account, history_id, feed_from, backfill_until)
        VALUES (%s, '1', %s, %s)
        """,
        (ME, NOW - timedelta(days=30), NOW - timedelta(days=30)),
    )
    yield conn


def _store(
    conn: psycopg.Connection, message_id: str, *, at: datetime, category: str = "primary"
) -> None:
    labels = sorted(PRIMARY) if category == "primary" else [f"CATEGORY_{category.upper()}"]
    conn.execute(
        """
        INSERT INTO gmail_messages (account, message_id, thread_id, internal_at, label_ids,
                                    direction, to_self, category, has_list_unsubscribe,
                                    arrived_via, first_seen_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, 'in', false, %s, false, 'history', %s, %s)
        """,
        (ME, message_id, f"t-{message_id}", at, labels, category, at, at),
    )


def _recall(conn: psycopg.Connection, box: FakeMailbox) -> RecallResult:
    gmail = GmailClient(box, pacer=Pacer(), retry_for=0)
    return recall(conn, gmail, account=ME, owners=(ME,), now=NOW)


def _audit(conn: psycopg.Connection, kind: str, at: datetime) -> None:
    conn.execute("INSERT INTO audit_log (kind, at) VALUES (%s, %s)", (kind, at))


# --- sync recall ------------------------------------------------------------------


def test_a_missing_id_is_stored_and_fed_and_gives_one_alert(mail: psycopg.Connection) -> None:
    box = FakeMailbox()
    box.put("stored", labels=PRIMARY, at=NOW - timedelta(hours=5))
    box.put("missed", labels=PRIMARY, at=NOW - timedelta(hours=10))
    box.put("outside", labels=PRIMARY, at=NOW - timedelta(hours=1))  # still the sync's turn
    _store(mail, "stored", at=NOW - timedelta(hours=5))
    MessageLedger(mail).claim("stored", "stored")  # processed: the feed is fine

    result = _recall(mail, box)

    assert (result.listed, result.missed, result.repaired) == (2, 1, 1)
    row = mail.execute(
        "SELECT arrived_via FROM gmail_messages WHERE message_id = 'missed'"
    ).fetchone()
    assert row == ("recall",)
    assert "missed" in feed.candidates(mail, 10, now=NOW)  # repaired inbound mail is fed
    assert result.alerts() == ["mail_sync_missed"]

    asked: list[tuple[str, frozenset[str]]] = []

    def deliver(text: str, skip: frozenset[str]) -> set[str]:
        asked.append((text, skip))
        return {"web_push"}

    for _ in range(2):  # a second check the same day sends nothing more
        send_alerts(
            mail, result.alerts(), names=frozenset({"web_push"}), deliver=deliver, day="2020-03-10"
        )
    assert asked == [("Mail sync missed messages", frozenset())]


def test_a_clean_day_raises_nothing(mail: psycopg.Connection) -> None:
    box = FakeMailbox()
    box.put("stored", labels=PRIMARY, at=NOW - timedelta(hours=5))
    _store(mail, "stored", at=NOW - timedelta(hours=5))
    MessageLedger(mail).claim("stored", "stored")

    result = _recall(mail, box)

    assert result.alerts() == []
    assert (result.sync_ok, result.feed_ok, result.categories_ok) == (True, True, True)


# --- feed recall ----------------------------------------------------------------------


def test_a_stalled_feed_raises_its_alert(mail: psycopg.Connection) -> None:
    """A row that has met the rule for over an hour with no ledger row."""
    _store(mail, "waiting", at=NOW - timedelta(hours=3))

    result = _recall(mail, FakeMailbox())

    assert (result.eligible, result.stalled) == (1, 1)
    assert result.alerts() == ["mail_feed_stalled"]


@pytest.mark.parametrize(
    "events",
    [
        [("paused", timedelta(hours=4))],  # paused, and still paused
        [("budget_exhausted", timedelta(hours=4))],  # the cap stopped work
        [("paused", timedelta(days=40))],  # paused long before the window, never resumed
    ],
)
def test_a_pause_or_a_stopped_cap_is_not_a_stall(
    mail: psycopg.Connection, events: list[tuple[str, timedelta]]
) -> None:
    _store(mail, "waiting", at=NOW - timedelta(hours=3))
    for kind, ago in events:
        _audit(mail, kind, NOW - ago)

    result = _recall(mail, FakeMailbox())

    assert (result.stalled, result.held) == (0, 1)
    assert result.alerts() == []


def test_a_pause_that_ended_before_the_row_arrived_excuses_nothing(
    mail: psycopg.Connection,
) -> None:
    _store(mail, "waiting", at=NOW - timedelta(hours=3))
    _audit(mail, "paused", NOW - timedelta(hours=9))
    _audit(mail, "resumed", NOW - timedelta(hours=8))

    assert _recall(mail, FakeMailbox()).stalled == 1


def test_a_cap_ends_with_its_month(mail: psycopg.Connection) -> None:
    exhausted = datetime(2020, 1, 20, tzinfo=UTC)
    _audit(mail, "budget_exhausted", exhausted)

    assert held_intervals(mail, NOW - timedelta(days=60), NOW) == [
        (exhausted, datetime(2020, 2, 1, tzinfo=UTC))
    ]


def test_a_too_old_record_for_day_old_mail_raises_the_feed_alert(
    mail: psycopg.Connection,
) -> None:
    """The age rule is wrong if it skips mail that was hours old."""
    _store(mail, "young", at=NOW - timedelta(hours=6))
    ledger = MessageLedger(mail)
    ledger.claim("young", "young")
    ledger.mark("young", MessageStatus.SKIPPED, error=feed.TOO_OLD)
    mail.execute(
        "UPDATE processed_messages SET created_at = %s WHERE gmail_message_id = 'young'",
        (NOW - timedelta(hours=5),),
    )

    result = _recall(mail, FakeMailbox())

    assert result.too_old_young == 1
    assert result.alerts() == ["mail_feed_stalled"]


# --- category agreement -----------------------------------------------------------------


def test_category_disagreement_is_caught_both_ways(mail: psycopg.Connection) -> None:
    box = FakeMailbox()
    box.put("gmail-primary", labels=PRIMARY, at=NOW - timedelta(hours=5))
    box.put("gmail-updates", labels={"CATEGORY_UPDATES"}, at=NOW - timedelta(hours=5))
    box.put("agrees", labels={"CATEGORY_FORUMS"}, at=NOW - timedelta(hours=5))
    _store(mail, "gmail-primary", at=NOW - timedelta(hours=5), category="updates")
    _store(mail, "gmail-updates", at=NOW - timedelta(hours=5), category="primary")
    _store(mail, "agrees", at=NOW - timedelta(hours=5), category="forums")
    for message_id in ("gmail-primary", "gmail-updates", "agrees"):
        MessageLedger(mail).claim(message_id, message_id)

    result = _recall(mail, box)

    assert (result.categories_checked, result.categories_mismatched) == (3, 2)
    assert result.alerts() == ["mail_sync_missed"]


def test_the_summary_is_counts_and_times_only(mail: psycopg.Connection) -> None:
    summary: dict[str, Any] = _recall(mail, FakeMailbox()).summary()

    assert summary["since"] == (NOW - timedelta(hours=26)).isoformat()
    assert all(isinstance(value, int | str) for value in summary.values())
