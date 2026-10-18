"""The meeting pipeline's feed (M20, D4), on a real Postgres: the rule is SQL.

Rows are written directly, so each case states exactly what the feed sees.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from app.mail import feed
from app.store.ledger import STRANDED_REASON, MessageLedger, MessageStatus

pytestmark = pytest.mark.integration

ME = "me@example.com"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
FEED_FROM = NOW - timedelta(days=3)
PRIMARY = ["CATEGORY_PERSONAL", "INBOX", "UNREAD"]


@pytest.fixture
def mail(conn: psycopg.Connection) -> Iterator[psycopg.Connection]:
    for table in ("gmail_messages", "gmail_cursors", "gmail_fetch_queue"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute(
        """
        INSERT INTO gmail_cursors (account, history_id, feed_from, backfill_until, caught_up_at)
        VALUES (%s, '1', %s, %s, %s)
        """,
        (ME, FEED_FROM, FEED_FROM, NOW),
    )
    yield conn


def _message(
    conn: psycopg.Connection,
    message_id: str,
    *,
    at: datetime = NOW - timedelta(hours=1),
    labels: list[str] | None = None,
    **columns: Any,
) -> None:
    labels = PRIMARY if labels is None else labels
    category = "primary"
    for label, name in (
        ("CATEGORY_PROMOTIONS", "promotions"),
        ("CATEGORY_SOCIAL", "social"),
        ("CATEGORY_UPDATES", "updates"),
        ("CATEGORY_FORUMS", "forums"),
    ):
        if label in labels:
            category = name
            break
    row: dict[str, Any] = {
        "account": ME,
        "message_id": message_id,
        "thread_id": f"t-{message_id}",
        "internal_at": at,
        "label_ids": sorted(labels),
        "direction": "out" if "SENT" in labels else "in",
        "to_self": False,
        "category": category,
        "has_list_unsubscribe": False,
        "arrived_via": "history",
    } | columns
    names = ", ".join(row)
    values = ", ".join(f"%({name})s" for name in row)
    conn.execute(f"INSERT INTO gmail_messages ({names}) VALUES ({values})", row)


def _candidates(conn: psycopg.Connection, limit: int = 50) -> list[str]:
    return feed.candidates(conn, limit, now=NOW)


def test_the_feed_starts_with_the_first_sync_run(mail: psycopg.Connection) -> None:
    assert feed.active(mail)
    mail.execute("DELETE FROM gmail_cursors")
    assert not feed.active(mail)


def test_strictly_inbound_non_bulk_primary_mail_reaches_poll(mail: psycopg.Connection) -> None:
    _message(mail, "plain")
    _message(mail, "read", labels=["CATEGORY_PERSONAL", "INBOX"])  # opened on the phone first
    _message(mail, "archived", labels=["CATEGORY_PERSONAL"])
    _message(mail, "untabbed", labels=["INBOX"])
    # Team mail from Google Groups: a list, in Primary, with a meeting in it.
    _message(mail, "groups", has_list_unsubscribe=True, precedence="list")
    _message(mail, "human-says-so", auto_submitted="no")

    assert sorted(_candidates(mail)) == [
        "archived",
        "groups",
        "human-says-so",
        "plain",
        "read",
        "untabbed",
    ]


@pytest.mark.parametrize(
    ("message_id", "columns"),
    [
        ("sent", {"labels": ["SENT"]}),
        ("to-self", {"to_self": True}),
        ("updates", {"labels": ["CATEGORY_UPDATES", "INBOX"]}),
        ("forums", {"labels": ["CATEGORY_FORUMS", "INBOX"]}),
        ("bulk", {"precedence": "bulk"}),
        ("junk", {"precedence": "junk"}),
        ("auto-reply", {"auto_submitted": "auto-replied"}),
        ("generated", {"auto_submitted": "auto-generated"}),
        ("spam", {"labels": ["CATEGORY_PERSONAL", "SPAM"]}),
        ("trash", {"labels": ["CATEGORY_PERSONAL", "TRASH"]}),
        ("gone", {"gone_at": NOW - timedelta(minutes=5)}),
        # With no tabs at all, List-Unsubscribe is the bulk marker.
        ("untabbed-list", {"labels": ["INBOX"], "has_list_unsubscribe": True}),
    ],
)
def test_the_rest_never_reaches_poll(
    mail: psycopg.Connection, message_id: str, columns: dict[str, Any]
) -> None:
    _message(mail, message_id, **columns)

    assert _candidates(mail) == []
    assert feed.record_too_old(mail, now=NOW + timedelta(days=30)) == []


def test_nothing_older_than_feed_from_less_an_hour_whichever_way_it_arrived(
    mail: psycopg.Connection,
) -> None:
    """The feed decides by time. The backfill's margin hour is fed; what the
    old poller had its chance at, before that, never is."""
    _message(mail, "margin", at=FEED_FROM - timedelta(minutes=30), arrived_via="backfill")
    _message(mail, "before", at=FEED_FROM - timedelta(hours=2), arrived_via="switch_over")
    _message(mail, "after", at=FEED_FROM + timedelta(minutes=5), arrived_via="history")

    assert _candidates(mail) == ["margin", "after"]


def test_a_message_with_a_ledger_row_is_skipped(mail: psycopg.Connection) -> None:
    _message(mail, "done")
    _message(mail, "new")
    MessageLedger(mail).claim("done", "done")

    assert _candidates(mail) == ["new"]


def test_oldest_first_up_to_the_batch(mail: psycopg.Connection) -> None:
    for hours in (3, 1, 2):
        _message(mail, f"{hours}h", at=NOW - timedelta(hours=hours))

    assert _candidates(mail, limit=2) == ["3h", "2h"]


def test_a_row_waiting_on_the_fetch_queue_is_held_back(mail: psycopg.Connection) -> None:
    """A catch-up queues the week's rows to be fetched again; until that
    answers, a message trashed during the outage still looks like Inbox."""
    _message(mail, "refetch")
    _message(mail, "unreadable")
    mail.execute(
        """
        INSERT INTO gmail_fetch_queue (message_id, reason, strikes, status)
        VALUES ('refetch', 'refetch', 0, 'queued'), ('unreadable', 'refetch', 5, 'unreadable')
        """
    )

    assert _candidates(mail) == ["unreadable"]


@pytest.mark.parametrize("behind", [timedelta(minutes=31), None])
def test_the_feed_offers_nothing_while_the_sync_is_behind(
    mail: psycopg.Connection, behind: timedelta | None
) -> None:
    """A row's labels are only as current as the sync. Hours into a failing
    sync, a message the owner trashed meanwhile still looks like Inbox."""
    _message(mail, "waiting")
    _message(mail, "eight-days", at=NOW - timedelta(days=8))
    mail.execute(
        "UPDATE gmail_cursors SET feed_from = %s, caught_up_at = %s",
        (NOW - timedelta(days=30), None if behind is None else NOW - behind),
    )

    assert _candidates(mail) == []
    assert feed.record_too_old(mail, now=NOW) == []


def test_the_feed_offers_mail_once_the_sync_has_caught_up_lately(
    mail: psycopg.Connection,
) -> None:
    _message(mail, "waiting")
    mail.execute("UPDATE gmail_cursors SET caught_up_at = %s", (NOW - timedelta(minutes=29),))

    assert _candidates(mail) == ["waiting"]


def test_mail_older_than_seven_days_is_recorded_as_skipped_without_a_model_call(
    mail: psycopg.Connection,
) -> None:
    _message(mail, "eight-days", at=NOW - timedelta(days=8))  # e.g. restored from the trash
    _message(mail, "six-days", at=NOW - timedelta(days=6))
    mail.execute("UPDATE gmail_cursors SET feed_from = %s", (NOW - timedelta(days=30),))

    assert feed.record_too_old(mail, now=NOW) == ["eight-days"]
    assert feed.record_too_old(mail, now=NOW) == []  # once

    entry = MessageLedger(mail).get("eight-days")
    assert entry is not None
    assert (entry.status, entry.error) == (MessageStatus.SKIPPED, feed.TOO_OLD)
    assert entry.thread_id == "eight-days"
    assert _candidates(mail) == ["six-days"]


# --- claims stranded by a restart -------------------------------------------------

BOOT = NOW
"""When the process booted. Claims made before it, and not younger than the
guard, are stranded."""


def _claim(
    conn: psycopg.Connection, message_id: str, *, ago: timedelta = timedelta(hours=1)
) -> None:
    MessageLedger(conn).claim(message_id, message_id)
    conn.execute(
        "UPDATE processed_messages SET created_at = %s WHERE gmail_message_id = %s",
        (BOOT - ago, message_id),
    )


def _recover(
    conn: psycopg.Connection,
    *,
    parked: Any = lambda message_id: False,
    forget: Any = lambda message_id: None,
) -> feed.StrandedClaims:
    return feed.recover_stranded(
        conn, parked=parked, forget=forget, claimed_before=BOOT - feed.STRANDED_AFTER, now=NOW
    )


def test_stranded_claims_are_left_released_or_failed(mail: psycopg.Connection) -> None:
    ledger = MessageLedger(mail)
    _message(mail, "parked")
    _message(mail, "offered")
    _message(mail, "too-old", at=NOW - timedelta(days=8))
    _message(mail, "bulk", precedence="bulk")
    for message_id in ("parked", "offered", "too-old", "bulk", "unknown"):
        _claim(mail, message_id)
    ledger.claim("settled", "settled")
    ledger.mark("settled", MessageStatus.SKIPPED, error="not a meeting")
    forgotten: list[str] = []

    result = _recover(
        mail, parked=lambda message_id: message_id == "parked", forget=forgotten.append
    )

    assert (result.left, result.released, result.failed, result.errors) == (1, 1, 3, 0)
    assert forgotten == ["offered"]
    assert ledger.get("offered") is None  # the feed offers it again
    assert feed.candidates(mail, 10, now=NOW) == ["offered"]
    parked = ledger.get("parked")
    assert parked is not None and parked.status is MessageStatus.CLAIMED
    for message_id in ("too-old", "bulk", "unknown"):
        entry = ledger.get(message_id)
        assert entry is not None
        assert (entry.status, entry.error) == (MessageStatus.FAILED, STRANDED_REASON)
    settled = ledger.get("settled")
    assert settled is not None and settled.status is MessageStatus.SKIPPED


def test_a_chat_claim_is_never_taken_for_stranded_mail(mail: psycopg.Connection) -> None:
    """A proposal from chat (M19, D8) has no mail to offer again and no
    thread to ask about. `park_chat` claims and parks it at once."""
    MessageLedger(mail).claim("chat-7-1", "chat-7", source="chat")
    mail.execute(
        "UPDATE processed_messages SET created_at = %s WHERE gmail_message_id = 'chat-7-1'",
        (BOOT - timedelta(hours=1),),
    )

    def never_asked(message_id: str) -> bool:
        raise AssertionError(f"{message_id} was asked about")

    result = _recover(mail, parked=never_asked)

    assert (result.left, result.released, result.failed, result.errors) == (0, 0, 0, 0)
    entry = MessageLedger(mail).get("chat-7-1")
    assert entry is not None and entry.status is MessageStatus.CLAIMED


def test_a_claim_younger_than_an_hour_is_no_longer_left_claimed_for_ever(
    mail: psycopg.Connection,
) -> None:
    """Before M20, boot failed only claims over an hour old."""
    _message(mail, "fresh")
    _claim(mail, "fresh", ago=timedelta(minutes=11))

    assert _recover(mail).released == 1


def test_a_claim_a_live_poller_could_hold_is_left_for_the_second_pass(
    mail: psycopg.Connection,
) -> None:
    """A poller in another process -- a CLI pass over `fly ssh console` --
    may be mid-message. There is no poller lock to ask, so a claim younger
    than the guard is left alone; the second pass, once the guard has
    passed, settles the claims made before boot."""
    _message(mail, "young")
    _claim(mail, "young", ago=timedelta(minutes=2))

    assert _recover(mail).released == 0
    entry = MessageLedger(mail).get("young")
    assert entry is not None and entry.status is MessageStatus.CLAIMED

    second = feed.recover_stranded(
        mail,
        parked=lambda message_id: False,
        forget=lambda message_id: None,
        claimed_before=BOOT,
        now=NOW + feed.STRANDED_AFTER,
    )
    assert second.released == 1


def test_a_claim_that_cannot_be_settled_is_left_and_the_rest_are_settled(
    mail: psycopg.Connection,
) -> None:
    """One unreadable checkpoint used to abort boot on every restart."""
    for message_id in ("unreadable", "unknown"):
        _claim(mail, message_id)

    def parked(message_id: str) -> bool:
        if message_id == "unreadable":
            raise ValueError("the checkpoint will not deserialise")
        return False

    result = _recover(mail, parked=parked)

    assert (result.errors, result.failed) == (1, 1)
    entry = MessageLedger(mail).get("unreadable")
    assert entry is not None and entry.status is MessageStatus.CLAIMED


# --- an outage (M20's open question, closed 2026-10-18) -------------------------------------

OUTAGES_MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "014_poll_outages.sql"


def test_the_outages_migration_can_be_run_again(mail: psycopg.Connection) -> None:
    """Again on a table with rows, and still with one check on the count."""
    _message(mail, "m1")
    sql = OUTAGES_MIGRATION.read_text(encoding="utf-8")
    mail.execute(sql)
    mail.execute(sql)

    checks = mail.execute(
        """
        SELECT count(*) FROM pg_constraint
         WHERE conrelid = 'gmail_messages'::regclass AND contype = 'c'
           AND pg_get_constraintdef(oid) LIKE '%%outages >= 0%%'
        """
    ).fetchone()
    assert checks is not None and checks[0] == 1


def test_the_outages_migration_upgrades_a_table_with_rows(mail: psycopg.Connection) -> None:
    """As production meets it: rows written before the columns existed. The
    fixture's transaction is rolled back, the dropped columns with it."""
    mail.execute("ALTER TABLE gmail_messages DROP COLUMN outages, DROP COLUMN last_outage_at")
    _message(mail, "before")

    mail.execute(OUTAGES_MIGRATION.read_text(encoding="utf-8"))

    row = mail.execute(
        "SELECT outages, last_outage_at FROM gmail_messages WHERE message_id = 'before'"
    ).fetchone()
    assert row == (0, None)


def test_a_message_released_for_an_outage_waits_before_the_feed_offers_it_again(
    mail: psycopg.Connection,
) -> None:
    _message(mail, "rested")
    _message(mail, "resting")
    feed.record_outage(mail, "rested", now=NOW - feed.OUTAGE_WAIT)
    feed.record_outage(mail, "resting", now=NOW - feed.OUTAGE_WAIT + timedelta(minutes=1))

    assert _candidates(mail) == ["rested"]


def test_the_third_tick_after_an_outage_offers_the_message_and_the_second_does_not(
    mail: psycopg.Connection,
) -> None:
    """Passes start ten minutes apart, a few seconds either way: a pass that
    opened its session faster than the stamped one still offers it."""
    _message(mail, "three-ticks-ago")
    _message(mail, "two-ticks-ago")
    feed.record_outage(mail, "three-ticks-ago", now=NOW - timedelta(minutes=30, seconds=-3))
    feed.record_outage(mail, "two-ticks-ago", now=NOW - timedelta(minutes=20, seconds=3))

    assert _candidates(mail) == ["three-ticks-ago"]


def test_a_message_still_waiting_at_seven_days_is_recorded_too_old(
    mail: psycopg.Connection,
) -> None:
    _message(mail, "aged", at=NOW - timedelta(days=8))
    mail.execute("UPDATE gmail_cursors SET feed_from = %s", (NOW - timedelta(days=30),))
    feed.record_outage(mail, "aged", now=NOW - timedelta(minutes=5))

    assert feed.record_too_old(mail, now=NOW) == ["aged"]


def test_outages_are_counted_per_message(mail: psycopg.Connection) -> None:
    _message(mail, "m1")
    _message(mail, "m2")

    assert [feed.record_outage(mail, "m1", now=NOW) for _ in range(3)] == [1, 2, 3]
    assert feed.record_outage(mail, "m2", now=NOW) == 1


def test_an_outage_on_a_message_the_sync_never_stored_counts_as_its_last(
    mail: psycopg.Connection,
) -> None:
    """Nothing could count it, so poll dead-letters it rather than retry it blind."""
    assert feed.record_outage(mail, "unknown", now=NOW) == feed.MAX_OUTAGES
