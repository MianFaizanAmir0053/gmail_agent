"""Checkpoint retention (M15), and the web channel's records (M16, D8).

Every polled message's full body -- one-time-code mail included -- sits in its
graph checkpoint, and nothing used to delete it. The purge keeps what a
pending decision still needs, and nothing else. The proposal cards and the
owner's corrections quote the same mail, so they follow the same clock.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any, TypedDict, cast

import psycopg
import pytest
from langgraph.graph import END, START, StateGraph

from app.channel.decide import decide
from app.channel.park import proposal_from, write_park
from app.channel.worker import ATTEMPTS_EXHAUSTED, settle_decided, settle_failed
from app.graph.checkpointer import postgres_checkpointer
from app.graph.nodes import CARRIED_A_CODE, NO_START_TIME, NOT_A_MEETING, SWEEP_REASON
from app.jobs.purge import purge
from app.mail.feed import GONE, TOO_OLD
from app.rag.ingest import STOPPED
from app.store.ledger import STRANDED_REASON, MessageLedger, MessageStatus

pytestmark = pytest.mark.integration


class _State(TypedDict):
    x: int


def _checkpoint(database_url: str, thread_id: str) -> None:
    """Leave a real checkpoint for `thread_id`, as a graph run would."""
    builder: StateGraph[_State, None, _State, _State] = StateGraph(_State)
    builder.add_node("n", lambda state: {"x": state["x"] + 1})
    builder.add_edge(START, "n")
    builder.add_edge("n", END)
    with postgres_checkpointer(database_url) as saver:
        builder.compile(checkpointer=saver).invoke(
            {"x": 0}, {"configurable": {"thread_id": thread_id}}
        )


def _has_checkpoint(conn: psycopg.Connection, thread_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM checkpoints WHERE thread_id = %s LIMIT 1", (thread_id,))
    return row.fetchone() is not None


def _age(conn: psycopg.Connection, message_id: str, days: int) -> None:
    conn.execute(
        "UPDATE processed_messages SET updated_at = now() - make_interval(days => %s)"
        " WHERE gmail_message_id = %s",
        (days, message_id),
    )


@pytest.fixture
def ids(migrated_database: str) -> Iterator[dict[str, str]]:
    """Unique thread ids: checkpoints are written on their own connection and
    outlive the test's rolled-back transaction, so they are removed at the end."""
    names = ("skipped", "parked", "failed_new", "failed_old", "claimed")
    made = {name: f"{name}-{uuid.uuid4().hex[:8]}" for name in names}
    yield made
    with postgres_checkpointer(migrated_database) as saver:
        for thread_id in made.values():
            saver.delete_thread(thread_id)


def _ledger(conn: psycopg.Connection, ids: dict[str, str]) -> None:
    ledger = MessageLedger(conn)
    for name, message_id in ids.items():
        ledger.claim(message_id, message_id)
        status = {
            "skipped": MessageStatus.SKIPPED,
            "parked": MessageStatus.AWAITING_APPROVAL,
            "failed_new": MessageStatus.FAILED,
            "failed_old": MessageStatus.FAILED,
            "claimed": None,
        }[name]
        if status is not None:
            ledger.mark(message_id, status, error="quoted: your code is 482913")
    _age(conn, ids["failed_old"], days=8)


def test_finished_threads_lose_their_checkpoints_and_pending_ones_keep_them(
    conn: psycopg.Connection, migrated_database: str, ids: dict[str, str]
) -> None:
    for thread_id in ids.values():
        _checkpoint(migrated_database, thread_id)
    _ledger(conn, ids)

    purge(conn, migrated_database)

    assert not _has_checkpoint(conn, ids["skipped"])
    assert not _has_checkpoint(conn, ids["failed_old"])
    assert _has_checkpoint(conn, ids["parked"])  # a decision still needs it
    assert _has_checkpoint(conn, ids["failed_new"])  # kept a week for diagnosis
    assert _has_checkpoint(conn, ids["claimed"])  # in flight


def test_model_written_reasons_are_cleared_after_a_week(
    conn: psycopg.Connection, migrated_database: str, ids: dict[str, str]
) -> None:
    """`skip` stores the model's reasoning, which quotes the email it read."""
    _ledger(conn, ids)
    _age(conn, ids["skipped"], days=8)

    purge(conn, migrated_database)

    entry = MessageLedger(conn).get(ids["skipped"])
    assert entry is not None and entry.error is None


def test_fixed_operator_reasons_survive(
    conn: psycopg.Connection, migrated_database: str, ids: dict[str, str]
) -> None:
    """These are what M24's statistics and M15's evidence are built from."""
    ledger = MessageLedger(conn)
    kept = {}
    for reason in (
        "declined by user",
        SWEEP_REASON,
        STRANDED_REASON,
        "made under another mode",  # an expiry (M17, D2)
    ):
        message_id = f"kept-{uuid.uuid4().hex[:8]}"
        ledger.claim(message_id, message_id)
        status = MessageStatus.FAILED if reason == STRANDED_REASON else MessageStatus.REJECTED
        ledger.mark(message_id, status, error=reason)
        _age(conn, message_id, days=30)
        kept[message_id] = reason

    purge(conn, migrated_database)

    for message_id, reason in kept.items():
        entry = ledger.get(message_id)
        assert entry is not None and entry.error == reason


def test_the_mail_feeds_fixed_reasons_survive(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    """Too old, gone before its turn, the classifier's no (M20, D4), and mail
    set aside for carrying a code (M18, D10): code wrote them, they quote
    nothing, and `/health` and the failures view count them."""
    ledger = MessageLedger(conn)
    kept = {}
    for reason in (TOO_OLD, GONE, NOT_A_MEETING, CARRIED_A_CODE):
        message_id = f"kept-{uuid.uuid4().hex[:8]}"
        ledger.claim(message_id, message_id)
        ledger.mark(message_id, MessageStatus.SKIPPED, error=reason)
        _age(conn, message_id, days=30)
        kept[message_id] = reason

    purge(conn, migrated_database)

    for message_id, reason in kept.items():
        entry = ledger.get(message_id)
        assert entry is not None and entry.error == reason


def test_a_meeting_with_no_start_time_keeps_its_fixed_reason(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    """Code wrote it in place of the model's reasoning (M18, D10)."""
    ledger = MessageLedger(conn)
    message_id = f"kept-{uuid.uuid4().hex[:8]}"
    ledger.claim(message_id, message_id)
    ledger.mark(message_id, MessageStatus.SKIPPED, error=NO_START_TIME)
    _age(conn, message_id, days=30)

    purge(conn, migrated_database)

    entry = ledger.get(message_id)
    assert entry is not None and entry.error == NO_START_TIME


# --- stored errors (M18, D7) -----------------------------------------------------


def _errors(conn: psycopg.Connection, *, days_old: int, error: str) -> tuple[uuid.UUID, int]:
    """A failed run with a failed span, and a failed ingestion run, `days_old`."""
    trace_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO runs (trace_id, gmail_message_id, status, started_at, error)"
        " VALUES (%s, %s, 'failed', now() - make_interval(days => %s), %s)",
        (trace_id, f"run-{trace_id.hex[:8]}", days_old, error),
    )
    conn.execute(
        "INSERT INTO spans (trace_id, node, status, started_at, latency_ms, error)"
        " VALUES (%s, 'extract', 'error', now() - make_interval(days => %s), 5, %s)",
        (trace_id, days_old, error),
    )
    row = conn.execute(
        "INSERT INTO ingest_runs (query, status, started_at, error)"
        " VALUES ('test', 'failed', now() - make_interval(days => %s), %s) RETURNING id",
        (days_old, error),
    ).fetchone()
    assert row is not None
    return trace_id, int(row[0])


def _stored(conn: psycopg.Connection, trace_id: uuid.UUID, ingest_id: int) -> list[str | None]:
    found = conn.execute(
        """
        SELECT (SELECT error FROM runs WHERE trace_id = %(t)s),
               (SELECT error FROM spans WHERE trace_id = %(t)s),
               (SELECT error FROM ingest_runs WHERE id = %(i)s)
        """,
        {"t": trace_id, "i": ingest_id},
    ).fetchone()
    assert found is not None
    return list(found)


def test_stored_errors_are_cut_to_their_type_after_a_week(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    error = "RuntimeError: upstream said [code removed], see [link: tracker.example]"
    old = _errors(conn, days_old=8, error=error)
    new = _errors(conn, days_old=6, error=error)
    stopped = _errors(conn, days_old=30, error=STOPPED)

    result = purge(conn, migrated_database)

    assert _stored(conn, *old) == ["RuntimeError"] * 3
    assert _stored(conn, *new) == [error] * 3  # a week for diagnosis
    assert _stored(conn, *stopped) == [STOPPED] * 3  # fixed words, quoting nothing
    assert result.errors_cut >= 3


# --- the mail sync's records (M20, D7) -----------------------------------------


def _mail_row(conn: psycopg.Connection, message_id: str, *, days_old: int) -> None:
    conn.execute(
        """
        INSERT INTO gmail_messages (account, message_id, thread_id, internal_at, direction,
                                    to_self, category, has_list_unsubscribe, arrived_via)
        VALUES ('me@example.com', %s, 't', now() - make_interval(days => %s), 'in', false,
                'primary', false, 'history')
        """,
        (message_id, days_old),
    )


def _mail_ids(conn: psycopg.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT message_id FROM gmail_messages").fetchall()}


def test_mail_metadata_is_kept_180_days_and_gone_rows_a_week(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    conn.execute("DELETE FROM gmail_messages")
    _mail_row(conn, "day-179", days_old=179)
    _mail_row(conn, "day-181", days_old=181)
    _mail_row(conn, "gone-6-days", days_old=10)
    _mail_row(conn, "gone-8-days", days_old=10)
    conn.execute(
        """
        UPDATE gmail_messages SET gone_at = now() - CASE message_id
            WHEN 'gone-6-days' THEN interval '6 days' ELSE interval '8 days' END
         WHERE message_id LIKE 'gone-%%'
        """
    )
    # Ledger rows are untouched: they are the record of what was done.
    ledger = MessageLedger(conn)
    ledger.claim("day-181", "day-181")
    ledger.mark("day-181", MessageStatus.SKIPPED, error=TOO_OLD)

    result = purge(conn, migrated_database)

    assert _mail_ids(conn) == {"day-179", "gone-6-days"}
    assert result.mail_messages_deleted == 2
    entry = ledger.get("day-181")
    assert entry is not None and entry.error == TOO_OLD


# --- the web channel's records (M16, D8) -------------------------------------

CARD: dict[str, Any] = {
    "proposed": {"title": "Design review", "attendees": ["sara@example.com"]},
    "conflicts": [],
    "dry_run": True,
    "review_issues": [],
    "action_type": "calendar_invite",
    "pipeline_version": "0123456789ab",
}

CORRECTION = "Make it 4pm, in my office"


def _parked(conn: psycopg.Connection, message_id: str) -> None:
    MessageLedger(conn).claim(message_id, message_id)
    with conn.transaction():
        write_park(conn, proposal_from(message_id, CARD, 1), ledger_status=MessageStatus.CLAIMED)


def _edited(conn: psycopg.Connection, message_id: str) -> int:
    """Parked, then edited from the web. Returns the open decision's id."""
    _parked(conn, message_id)
    result = decide(conn, message_id, action="edit", revision=1, correction=CORRECTION, via="web")
    assert result.decision_id is not None
    return result.decision_id


def _settled(conn: psycopg.Connection, message_id: str, ledger_status: MessageStatus) -> None:
    """Parked, edited, and the edit applied: the worker recorded its outcome."""
    decision_id = _edited(conn, message_id)
    with conn.transaction():
        if ledger_status is MessageStatus.FAILED:
            settle_failed(conn, decision_id, message_id, reason=ATTEMPTS_EXHAUSTED)
        else:
            # The graph marks the ledger; the worker then settles from it.
            MessageLedger(conn).mark(message_id, ledger_status)
            settle_decided(conn, decision_id, message_id, final_status=ledger_status.value)


def _payload(conn: psycopg.Connection, message_id: str) -> Any:
    row = conn.execute("SELECT payload FROM proposals WHERE message_id = %s", (message_id,))
    found = row.fetchone()
    assert found is not None
    return found[0]


def _correction(conn: psycopg.Connection, message_id: str) -> str | None:
    row = conn.execute("SELECT correction FROM decisions WHERE message_id = %s", (message_id,))
    found = row.fetchone()
    assert found is not None
    return cast(str | None, found[0])


@pytest.mark.parametrize("ledger_status", [MessageStatus.REJECTED, MessageStatus.FAILED])
@pytest.mark.parametrize(("days", "cleared"), [(6, False), (8, True)])
def test_a_settled_proposal_loses_its_content_a_week_after_its_ledger_did(
    conn: psycopg.Connection,
    migrated_database: str,
    ledger_status: MessageStatus,
    days: int,
    cleared: bool,
) -> None:
    """The card's title and guests, and the owner's correction, quote the mail.

    The clock is the ledger's, as for M15's reasons. FAILED counts too: the
    worker produces it, and its content would otherwise be kept for ever.
    """
    message_id = f"settled-{uuid.uuid4().hex[:8]}"
    _settled(conn, message_id, ledger_status)
    _age(conn, message_id, days=days)

    purge(conn, migrated_database)

    assert (_payload(conn, message_id) is None) is cleared
    assert (_correction(conn, message_id) is None) is cleared


def test_the_purge_counts_what_it_cleared(conn: psycopg.Connection, migrated_database: str) -> None:
    message_id = f"settled-{uuid.uuid4().hex[:8]}"
    _settled(conn, message_id, MessageStatus.REJECTED)
    _age(conn, message_id, days=8)

    result = purge(conn, migrated_database)

    assert (result.proposals_cleared, result.corrections_cleared) == (1, 1)


def _evidence(conn: psycopg.Connection, message_id: str) -> tuple[Any, ...]:
    row = conn.execute(
        """
        SELECT p.revision, p.status, p.final_status, p.action_type, p.pipeline_version,
               p.dry_run, p.parked_at, p.updated_at,
               d.revision, d.action, d.via, d.action_type, d.pipeline_version,
               d.decided_at, d.latency_seconds, d.outcome, d.reason, d.attempts,
               d.settled_at
          FROM proposals p JOIN decisions d USING (message_id)
         WHERE p.message_id = %s
        """,
        (message_id,),
    ).fetchone()
    assert row is not None
    return tuple(row)


def test_m24s_evidence_survives_the_purge(conn: psycopg.Connection, migrated_database: str) -> None:
    """What M24 counts autonomy from quotes no email, so it is kept for good."""
    message_id = f"settled-{uuid.uuid4().hex[:8]}"
    _settled(conn, message_id, MessageStatus.REJECTED)
    _age(conn, message_id, days=30)
    before = _evidence(conn, message_id)

    purge(conn, migrated_database)

    assert _payload(conn, message_id) is None  # the purge did reach this row
    assert _evidence(conn, message_id) == before


def test_an_open_proposal_keeps_its_content_whatever_its_ledger_says(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    """The owner can still decide a pending card, and the worker still owns a
    deciding one. Their ledgers can already be final: the M15 CLI left pending
    cards behind that way, and the graph marks the ledger before the worker
    settles the decision."""
    waiting = f"waiting-{uuid.uuid4().hex[:8]}"
    _parked(conn, waiting)
    applying = f"applying-{uuid.uuid4().hex[:8]}"
    _edited(conn, applying)
    for message_id in (waiting, applying):
        MessageLedger(conn).mark(message_id, MessageStatus.REJECTED)
        _age(conn, message_id, days=30)

    purge(conn, migrated_database)

    assert _payload(conn, waiting) is not None
    assert _payload(conn, applying) is not None
    assert _correction(conn, applying) == CORRECTION


def test_expired_pairing_codes_are_deleted(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    conn.execute("DELETE FROM pairing_codes")  # rolled back with the test
    conn.execute(
        """
        INSERT INTO pairing_codes (code_sha256, issued_to, expires_at)
        VALUES ('expired', 'session-1', now() - interval '1 minute'),
               ('live', 'session-2', now() + interval '4 minutes')
        """
    )

    result = purge(conn, migrated_database)

    assert conn.execute("SELECT code_sha256 FROM pairing_codes").fetchall() == [("live",)]
    assert result.pairing_codes_deleted == 1


# --- a calendar write's stored request (M17, D8) ------------------------------------------


def _write_begun(conn: psycopg.Connection, message_id: str, *, days_ago: int) -> None:
    """A write that began `days_ago` and was never finished: its request stays."""
    decision_id = _edited(conn, message_id)
    conn.execute(
        """
        INSERT INTO outbound_actions
               (decision_id, message_id, tool, tier, args_hash, dry_run, nonce, status,
                calendar_id, event_id, request, started_at)
        VALUES (%s, %s, 'calendar.create_hold', 1, 'hash', false, 'nonce', 'executing',
                'test-calendar', 'ev1', '{"summary": "Design review"}',
                now() - %s * interval '1 day')
        """,
        (decision_id, message_id, days_ago),
    )


def _request(conn: psycopg.Connection, message_id: str) -> Any:
    row = conn.execute("SELECT request FROM outbound_actions WHERE message_id = %s", (message_id,))
    found = row.fetchone()
    assert found is not None
    return found[0]


def test_a_stored_request_is_cleared_a_week_after_its_write_began(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    """It holds the event's content. By then nothing will replay it: a write
    still unconfirmed is only looked up, by its event's id."""
    old, recent = f"write-{uuid.uuid4().hex[:8]}", f"write-{uuid.uuid4().hex[:8]}"
    _write_begun(conn, old, days_ago=8)
    _write_begun(conn, recent, days_ago=6)

    result = purge(conn, migrated_database)

    assert _request(conn, old) is None
    assert _request(conn, recent) == {"summary": "Design review"}
    assert result.requests_cleared >= 1
