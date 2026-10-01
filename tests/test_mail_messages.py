"""The mail sync's records and what a message is classified as (M20, D1, D2, D8).

Metadata only: the table has no column a subject, snippet or body could go
in. Classification is pure and tested as such; storage is Postgres behaviour
-- CHECK constraints, ON CONFLICT -- so it is tested against a real server.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from app.google.gmail import SYNC_HEADERS, MessageMeta, to_message_meta
from app.mail.messages import (
    MessageRow,
    apply_labels,
    category_of,
    classify,
    direction_of,
    kept,
    mark_gone,
    owner_addresses,
    store,
)

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "011_mail_sync.sql"

ME = "me@example.com"
OWNERS = owner_addresses(ME, "Me.Owner@Gmail.com", "alias@work.example")
AT = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def _columns(conn: psycopg.Connection, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,)
    ).fetchall()
    return {row[0] for row in rows}


# --- the migration (20.1) -----------------------------------------------------


@pytest.mark.integration
def test_the_migration_can_be_run_again(conn: psycopg.Connection) -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    conn.execute(sql)
    conn.execute(sql)


@pytest.mark.integration
def test_no_column_could_hold_content(conn: psycopg.Connection) -> None:
    """No subject, snippet or body until M18 can strip one-time codes."""
    for table in ("gmail_messages", "gmail_cursors", "gmail_fetch_queue"):
        columns = _columns(conn, table)
        assert columns, f"{table} is missing"
        assert not columns & {"subject", "snippet", "body", "body_text", "payload"}


@pytest.mark.integration
def test_the_records_have_the_columns_the_spec_names(conn: psycopg.Connection) -> None:
    assert {
        "account",
        "message_id",
        "thread_id",
        "internal_at",
        "label_ids",
        "direction",
        "to_self",
        "category",
        "from_addr",
        "to_addrs",
        "cc_addrs",
        "has_list_unsubscribe",
        "precedence",
        "auto_submitted",
        "arrived_via",
        "first_seen_at",
        "updated_at",
        "gone_at",
    } <= _columns(conn, "gmail_messages")
    assert {
        "account",
        "history_id",
        "feed_from",
        "switch_over_at",
        "caught_up_at",
        "backfill_until",
        "gap_from",
        "gap_until",
        "gap_progress",
        "catch_ups",
    } <= _columns(conn, "gmail_cursors")
    assert {"message_id", "reason", "queued_at", "strikes", "status"} <= _columns(
        conn, "gmail_fetch_queue"
    )


def _insert_message(conn: psycopg.Connection, **overrides: object) -> None:
    row: dict[str, object] = {
        "account": "me@example.com",
        "message_id": "m1",
        "thread_id": "t1",
        "internal_at": "2026-10-01T09:00:00Z",
        "direction": "in",
        "to_self": False,
        "category": "primary",
        "has_list_unsubscribe": False,
        "arrived_via": "history",
    } | overrides
    columns = ", ".join(row)
    placeholders = ", ".join(f"%({name})s" for name in row)
    conn.execute(f"INSERT INTO gmail_messages ({columns}) VALUES ({placeholders})", row)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("direction", "sideways"),
        ("category", "spam"),
        ("arrived_via", "magic"),
    ],
)
def test_the_closed_lists_are_enforced(conn: psycopg.Connection, column: str, value: str) -> None:
    conn.execute("DELETE FROM gmail_messages")
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _insert_message(conn, **{column: value})


@pytest.mark.integration
def test_a_message_has_one_row_per_account(conn: psycopg.Connection) -> None:
    conn.execute("DELETE FROM gmail_messages")
    _insert_message(conn)
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        _insert_message(conn)


@pytest.mark.integration
def test_a_gap_is_recorded_whole_or_not_at_all(conn: psycopg.Connection) -> None:
    """`gap_from` and `gap_until` mean nothing apart."""
    conn.execute("DELETE FROM gmail_cursors")
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(
            """
            INSERT INTO gmail_cursors (account, history_id, feed_from, backfill_until, gap_from)
            VALUES ('me@example.com', '1', now(), now(), now())
            """
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("column", "value"),
    [("status", "maybe"), ("reason", "whim"), ("strikes", -1)],
)
def test_the_fetch_queue_takes_only_its_own_words(
    conn: psycopg.Connection, column: str, value: object
) -> None:
    conn.execute("DELETE FROM gmail_fetch_queue")
    row: dict[str, object] = {"message_id": "m1", "reason": "label_change"} | {column: value}
    columns = ", ".join(row)
    placeholders = ", ".join(f"%({name})s" for name in row)
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(f"INSERT INTO gmail_fetch_queue ({columns}) VALUES ({placeholders})", row)


# --- classification (20.3) ----------------------------------------------------


def _meta(
    labels: set[str],
    *,
    sender: str = "Sara <Sara@Example.com>",
    to: str = ME,
    cc: str = "",
    **headers: str,
) -> MessageMeta:
    named = {"From": sender, "To": to, "Cc": cc} | {
        name.replace("_", "-"): value for name, value in headers.items()
    }
    return MessageMeta(
        id="m1",
        thread_id="t1",
        label_ids=frozenset(labels),
        internal_date=AT,
        headers={name: value for name, value in named.items() if value},
    )


def test_sent_mail_is_outbound_and_everything_else_inbound() -> None:
    assert direction_of(frozenset({"SENT"})) == "out"
    assert direction_of(frozenset({"SENT", "INBOX"})) == "out"
    assert direction_of(frozenset({"INBOX", "UNREAD"})) == "in"


@pytest.mark.parametrize(
    ("labels", "category"),
    [
        ({"CATEGORY_PERSONAL", "INBOX"}, "primary"),
        ({"INBOX"}, "primary"),
        (set(), "primary"),
        ({"CATEGORY_UPDATES"}, "updates"),
        ({"CATEGORY_FORUMS"}, "forums"),
        ({"CATEGORY_PROMOTIONS"}, "promotions"),
        ({"CATEGORY_SOCIAL"}, "social"),
        # Gmail lists a message in a tab by that tab's label, so any other
        # category label means it is not in Primary, whatever else it has.
        ({"CATEGORY_PERSONAL", "CATEGORY_UPDATES"}, "updates"),
    ],
)
def test_categories_map_and_no_category_is_primary(labels: set[str], category: str) -> None:
    assert category_of(frozenset(labels)) == category


@pytest.mark.parametrize(
    ("labels", "stored"),
    [
        ({"INBOX", "CATEGORY_PERSONAL"}, True),
        ({"INBOX"}, True),
        ({"CATEGORY_UPDATES"}, True),  # deadlines and money, for M21
        ({"CATEGORY_FORUMS"}, True),
        ({"SENT"}, True),
        ({"SENT", "CATEGORY_SOCIAL"}, True),  # the owner's own mail, whatever its tab
        ({"INBOX", "CATEGORY_PROMOTIONS"}, False),
        ({"INBOX", "CATEGORY_SOCIAL"}, False),
        ({"DRAFT"}, False),
        ({"CHAT"}, False),
        ({"SPAM", "CATEGORY_PERSONAL"}, False),
        ({"TRASH", "SENT"}, False),
    ],
)
def test_what_is_stored(labels: set[str], stored: bool) -> None:
    assert kept(frozenset(labels)) is stored


def test_mail_from_the_owner_to_the_owner_is_to_self() -> None:
    assert classify(_meta({"SENT", "INBOX"}, sender=ME, to=ME), OWNERS).to_self


@pytest.mark.parametrize(
    ("sender", "to", "expected"),
    [
        ("Me <me@example.com>", "Sara <sara@example.com>", False),  # a sent reply
        ("sara@example.com", ME, False),  # inbound
        # Gmail ignores dots and +tags, so these are the owner's own address.
        ("me.owner+news@gmail.com", "meowner@googlemail.com", True),
        ("alias@work.example", "Me <ME@example.com>", True),  # forwarded from an alias
        ("me@example.com", "sara@example.com, alias@work.example", True),
    ],
)
def test_to_self_comes_from_the_headers(sender: str, to: str, expected: bool) -> None:
    assert classify(_meta({"INBOX"}, sender=sender, to=to), OWNERS).to_self is expected


def test_a_copy_to_the_owner_is_not_to_self() -> None:
    """Only `To` counts: a cc to yourself is a copy of mail to someone else."""
    row = classify(_meta({"SENT"}, sender=ME, to="sara@example.com", cc=ME), OWNERS)
    assert not row.to_self


def test_addresses_are_lower_cased_as_written() -> None:
    row = classify(
        _meta(
            {"INBOX"},
            sender="Sara <Sara.Ahmed@Gmail.com>",
            to="Me <ME@Example.com>, ops@Example.com",
            cc="Bilal <Bilal+x@Example.com>",
        ),
        OWNERS,
    )
    assert row.from_addr == "sara.ahmed@gmail.com"
    assert row.to_addrs == ("me@example.com", "ops@example.com")
    assert row.cc_addrs == ("bilal+x@example.com",)


def test_the_bulk_signals_are_kept_raw_and_the_unsubscribe_url_is_not() -> None:
    row = classify(
        _meta(
            {"INBOX"},
            List_Unsubscribe="<https://example.com/unsubscribe?token=secret>",
            Precedence=" Bulk ",
            Auto_Submitted="Auto-Generated",
        ),
        OWNERS,
    )
    assert row.has_list_unsubscribe is True
    assert row.precedence == "bulk"
    assert row.auto_submitted == "auto-generated"
    assert "secret" not in repr(row)


def test_absent_bulk_headers_are_absent_not_empty() -> None:
    row = classify(_meta({"INBOX"}), OWNERS)
    assert (row.has_list_unsubscribe, row.precedence, row.auto_submitted) == (False, None, None)


def test_labels_are_kept_in_a_stable_order() -> None:
    row = classify(_meta({"UNREAD", "INBOX", "CATEGORY_PERSONAL"}), OWNERS)
    assert row.label_ids == ("CATEGORY_PERSONAL", "INBOX", "UNREAD")


# --- storage (20.3) -------------------------------------------------------------


@pytest.fixture
def mail(conn: psycopg.Connection) -> Iterator[psycopg.Connection]:
    """The test connection with the mail sync's records cleared, inside the
    transaction the `conn` fixture rolls back."""
    for table in ("gmail_messages", "gmail_cursors", "gmail_fetch_queue"):
        conn.execute(f"DELETE FROM {table}")
    yield conn


def _row(conn: psycopg.Connection, message_id: str = "m1") -> dict[str, Any] | None:
    cursor = conn.execute(
        "SELECT * FROM gmail_messages WHERE account = %s AND message_id = %s", (ME, message_id)
    )
    found = cursor.fetchone()
    if found is None:
        return None
    assert cursor.description is not None
    return dict(zip([column.name for column in cursor.description], found, strict=True))


def _primary(labels: set[str] | None = None) -> MessageRow:
    return classify(_meta(labels or {"INBOX", "UNREAD", "CATEGORY_PERSONAL"}), OWNERS)


@pytest.mark.integration
def test_a_kept_message_is_stored_once_and_storing_it_again_changes_nothing(
    mail: psycopg.Connection,
) -> None:
    assert store(mail, ME, _primary(), "history") == "inserted"
    before = _row(mail)

    assert store(mail, ME, _primary(), "history") == "unchanged"
    assert _row(mail) == before


@pytest.mark.integration
def test_how_a_row_first_arrived_is_kept(mail: psycopg.Connection) -> None:
    store(mail, ME, _primary(), "backfill")
    store(mail, ME, _primary({"INBOX", "CATEGORY_PERSONAL"}), "history")

    row = _row(mail)
    assert row is not None
    assert row["arrived_via"] == "backfill"
    assert row["label_ids"] == ["CATEGORY_PERSONAL", "INBOX"]


@pytest.mark.integration
@pytest.mark.parametrize(
    "labels",
    [{"INBOX", "CATEGORY_PROMOTIONS"}, {"INBOX", "CATEGORY_SOCIAL"}, {"DRAFT"}, {"SPAM"}],
)
def test_what_d1_leaves_out_is_not_stored(mail: psycopg.Connection, labels: set[str]) -> None:
    assert store(mail, ME, _primary(labels), "history") == "not_kept"
    assert _row(mail) is None


@pytest.mark.integration
def test_a_stored_message_fetched_again_takes_its_current_labels_even_in_the_trash(
    mail: psycopg.Connection,
) -> None:
    store(mail, ME, _primary(), "history")

    assert store(mail, ME, _primary({"TRASH", "CATEGORY_PERSONAL"}), "catch_up") == "updated"

    row = _row(mail)
    assert row is not None
    assert row["label_ids"] == ["CATEGORY_PERSONAL", "TRASH"]


@pytest.mark.integration
def test_a_label_change_applies_without_a_fetch_and_recomputes_the_category(
    mail: psycopg.Connection,
) -> None:
    store(mail, ME, _primary(), "history")

    outcome = apply_labels(
        mail, ME, "m1", added=frozenset({"CATEGORY_UPDATES"}), removed=frozenset({"UNREAD"})
    )

    assert outcome == "changed"
    row = _row(mail)
    assert row is not None
    assert row["label_ids"] == ["CATEGORY_PERSONAL", "CATEGORY_UPDATES", "INBOX"]
    assert row["category"] == "updates"


@pytest.mark.integration
def test_a_label_change_recomputes_the_direction(mail: psycopg.Connection) -> None:
    store(mail, ME, _primary({"INBOX"}), "history")

    apply_labels(mail, ME, "m1", added=frozenset({"SENT"}))

    row = _row(mail)
    assert row is not None and row["direction"] == "out"


@pytest.mark.integration
def test_moving_the_owners_own_mail_to_the_inbox_changes_nothing_that_feeds(
    mail: psycopg.Connection,
) -> None:
    """Moving a conversation to the Inbox labels the owner's own replies INBOX
    too. `to_self` comes from the headers, so it stays."""
    store(mail, ME, classify(_meta({"SENT"}, sender=ME, to=ME), OWNERS), "history")

    apply_labels(mail, ME, "m1", added=frozenset({"INBOX", "CATEGORY_PERSONAL"}))

    row = _row(mail)
    assert row is not None
    assert (row["to_self"], row["direction"]) == (True, "out")


@pytest.mark.integration
def test_a_label_change_to_a_message_not_stored_says_so(mail: psycopg.Connection) -> None:
    assert apply_labels(mail, ME, "unknown", removed=frozenset({"SPAM"})) == "absent"
    assert apply_labels(mail, ME, "unknown", removed=frozenset({"SPAM"})) == "absent"


@pytest.mark.integration
def test_the_same_label_change_twice_changes_nothing_the_second_time(
    mail: psycopg.Connection,
) -> None:
    store(mail, ME, _primary(), "history")
    apply_labels(mail, ME, "m1", removed=frozenset({"UNREAD"}))
    before = _row(mail)

    assert apply_labels(mail, ME, "m1", removed=frozenset({"UNREAD"})) == "unchanged"
    assert _row(mail) == before


@pytest.mark.integration
def test_a_gone_message_is_marked_once_and_a_later_fetch_unmarks_it(
    mail: psycopg.Connection,
) -> None:
    store(mail, ME, _primary(), "history")

    assert mark_gone(mail, ME, "m1") is True
    assert mark_gone(mail, ME, "m1") is False
    assert mark_gone(mail, ME, "never stored") is False
    row = _row(mail)
    assert row is not None and row["gone_at"] is not None

    # A fetch that answered is the stronger evidence: the message is there.
    assert store(mail, ME, _primary(), "queue") == "updated"
    row = _row(mail)
    assert row is not None and row["gone_at"] is None


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


@pytest.mark.integration
def test_a_seeded_subject_snippet_and_body_never_reach_the_table(
    mail: psycopg.Connection,
) -> None:
    """Even if Gmail ignored the field mask and sent them all."""
    message = {
        "id": "m1",
        "threadId": "t1",
        "labelIds": ["INBOX", "CATEGORY_PERSONAL"],
        "internalDate": str(int(AT.timestamp() * 1000)),
        "snippet": "Your code is 482913",
        "payload": {
            "headers": [
                {"name": "From", "value": "Sara <sara@example.com>"},
                {"name": "To", "value": ME},
                {"name": "Subject", "value": "Your code is 482913"},
            ],
            "body": {"data": _b64("Your code is 482913")},
            "parts": [{"mimeType": "text/plain", "body": {"data": _b64("Code 482913")}}],
        },
    }

    store(mail, ME, classify(to_message_meta(message, SYNC_HEADERS), OWNERS), "history")

    row = _row(mail)
    assert row is not None
    assert "482913" not in repr(row)
