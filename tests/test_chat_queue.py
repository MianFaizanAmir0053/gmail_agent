"""Proposals from chat on the queue mail uses (M19, D8): no graph, no checkpoint.

Each test parks a chat proposal as a turn will (`park_chat`), records a
decision as every channel does (`decide`), and lets the worker apply it.
Gmail is never asked about a chat proposal, so the fake here fails if it is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, cast

import psycopg
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.channel import worker
from app.channel.decide import Via, card_token, decide, request_withdraw
from app.channel.park import ParkConflictError, ProposalRecord, park_chat
from app.channel.worker import MAX_ATTEMPTS, apply_open
from app.contracts import ExtractionResult
from app.google.calendar import WriteRejectedError
from app.graph.nodes import DECLINED, Deps
from app.graph.runner import GraphSession
from app.policy import audit, contacts, control
from app.policy.contacts import unconfirmed_outsiders
from app.policy.hashing import Binding, args_key
from app.policy.registry import Registry
from app.store.ledger import MessageLedger

KEY = args_key("test-key")
CALENDAR = "test-calendar"
START = datetime.now(UTC).replace(microsecond=0) + timedelta(days=3)
SARA = "sara.khan@example.com"
ALI = "ali.raza@example.org"


class NoGmail:
    """Fails if asked anything: chat's recipient rule never reads Gmail."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"Gmail was asked {name} for a chat proposal")


@dataclass
class Calendar:
    dry_run: bool = True
    calendar_id: str = CALENDAR
    events: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    calls: int = 0
    fail_after: int = 0
    fail_find: int = 0
    rejects: bool = False

    def freebusy(self, start: datetime, end: datetime) -> list[Any]:
        return []

    def insert(self, calendar_id: str, body: dict[str, Any], *, event_id: str) -> str | None:
        self.calls += 1
        if self.dry_run:
            return None
        if self.rejects:
            raise WriteRejectedError("Google refused the request")
        self.events[(calendar_id, event_id)] = body
        if self.fail_after:
            self.fail_after -= 1
            raise ConnectionError("network down after Google made the event")
        return event_id

    def find(self, calendar_id: str, event_id: str) -> str | None:
        if self.fail_find:
            self.fail_find -= 1
            raise ConnectionError("Google unavailable")
        return event_id if (calendar_id, event_id) in self.events else None


def _session(conn: psycopg.Connection, calendar: Calendar) -> GraphSession:
    gmail = NoGmail()
    deps = Deps(
        gmail=cast(Any, gmail),
        pipeline=cast(Any, None),
        calendar=cast(Any, calendar),
        ledger=MessageLedger(conn),
        user_timezone="Asia/Karachi",
        registry=Registry(
            conn, calendar, key=KEY, outsiders=partial(unconfirmed_outsiders, conn, gmail)
        ),
        pipeline_version="chat-test",
        args_key=KEY,
    )
    return GraphSession(deps=deps, conn=conn, checkpointer=InMemorySaver(), trace=False)


@pytest.fixture
def db(conn: psycopg.Connection) -> psycopg.Connection:
    conn.execute("DELETE FROM proposals")  # rolled back with the test
    conn.execute("DELETE FROM chat_turns")
    conn.execute("DELETE FROM confirmed_contacts")
    conn.execute("UPDATE control SET paused = false WHERE id = 1")
    return conn


def _turn(conn: psycopg.Connection, typed: str) -> int:
    row = conn.execute(
        """
        INSERT INTO chat_turns (idempotency_key, typed, status, started_at)
        VALUES (%s, %s, 'running', now()) RETURNING id
        """,
        (f"key-{abs(hash(typed)):016d}", typed),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _park(
    conn: psycopg.Connection,
    *,
    typed: str = f"book 30 minutes with {SARA} on Monday",
    guests: tuple[str, ...] = (SARA,),
    sources: dict[str, str] | None = None,
    conflicts: tuple[str, ...] = (),
    dry_run: bool = True,
    turn: int | None = None,
) -> str:
    turn = _turn(conn, typed) if turn is None else turn
    proposed = ExtractionResult(
        is_meeting=True,
        title="Call with Sara",
        start_utc=START,
        end_utc=START + timedelta(minutes=30),
        timezone="Asia/Karachi",
        attendees=list(guests),
        confidence=1.0,
        reasoning="Proposed from a chat turn.",
    )
    with conn.transaction():
        record = park_chat(
            conn,
            turn_id=turn,
            step=3,
            proposed=proposed,
            guest_sources=sources if sources is not None else dict.fromkeys(guests, "typed"),
            conflicts=list(conflicts),
            pipeline_version="chat-test",
            binding=Binding(calendar_id=CALENDAR, key=KEY),
            dry_run=dry_run,
        )
    return record.message_id


def _confirm(conn: psycopg.Connection, message_id: str) -> Any:
    row = conn.execute(
        "SELECT args_hash, dry_run, generation, revision FROM proposals WHERE message_id = %s",
        (message_id,),
    ).fetchone()
    assert row is not None
    return decide(
        conn,
        message_id,
        action="confirm",
        revision=row[3],
        via="web",
        token=card_token(row[0], row[1], row[2]),
        dry_run=row[1],
    )


def _state(conn: psycopg.Connection, message_id: str) -> tuple[Any, ...]:
    row = conn.execute(
        """
        SELECT p.status, p.final_status, p.generation, m.status, m.error
          FROM proposals p JOIN processed_messages m ON m.gmail_message_id = p.message_id
         WHERE p.message_id = %s
        """,
        (message_id,),
    ).fetchone()
    assert row is not None
    return tuple(row)


def _outcomes(conn: psycopg.Connection, message_id: str) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT action, outcome, reason FROM decisions WHERE message_id = %s ORDER BY id",
        (message_id,),
    ).fetchall()


def _audited(conn: psycopg.Connection, message_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT kind FROM audit_log
         WHERE message_id = %s
           AND kind IN ('decision_withdrawn', 'withdraw_declined', 'proposal_expired')
         ORDER BY id
        """,
        (message_id,),
    ).fetchall()
    return [row[0] for row in rows]


def _no_thread(self: GraphSession, asked: str) -> Any:
    raise AssertionError(f"a thread was read for {asked}")


# --- parked -----------------------------------------------------------------------------


@pytest.mark.integration
def test_a_step_is_parked_once(db: psycopg.Connection) -> None:
    turn = _turn(db, f"book 30 minutes with {SARA} on Monday")
    _park(db, turn=turn)
    with pytest.raises(ParkConflictError):
        _park(db, turn=turn)


@pytest.mark.integration
def test_a_chat_proposal_parks_with_its_source_and_its_proposed_event(
    db: psycopg.Connection,
) -> None:
    message_id = _park(db)

    assert message_id.startswith("chat-") and ":" not in message_id
    row = db.execute(
        """
        SELECT p.source, p.chat_turn_id IS NOT NULL, p.payload->'proposed'->>'title',
               p.payload->'guest_sources', p.tool, m.source, m.status
          FROM proposals p JOIN processed_messages m ON m.gmail_message_id = p.message_id
         WHERE p.message_id = %s
        """,
        (message_id,),
    ).fetchone()
    assert row == (
        "chat",
        True,
        "Call with Sara",
        {SARA: "typed"},
        "calendar.create_invite",
        "chat",
        "awaiting_approval",
    )


# --- Confirm ----------------------------------------------------------------------------


@pytest.mark.integration
def test_a_confirm_in_dry_run_runs_through_the_registry_and_writes_nothing(
    db: psycopg.Connection,
) -> None:
    calendar = Calendar(dry_run=True)
    message_id = _park(db)
    assert _confirm(db, message_id).status == "queued"

    applied = apply_open(_session(db, calendar))

    assert applied == [(message_id, "skipped")]
    assert _state(db, message_id) == ("decided", "skipped", 1, "skipped", "dry_run")
    assert calendar.events == {}
    action = db.execute(
        "SELECT status FROM outbound_actions WHERE decision_id = "
        "(SELECT id FROM decisions WHERE message_id = %s)",
        (message_id,),
    ).fetchone()
    assert action == ("dry_run",)


@pytest.mark.integration
def test_a_confirm_writes_the_event_to_the_write_calendar(db: psycopg.Connection) -> None:
    calendar = Calendar(dry_run=False)
    message_id = _park(db, dry_run=False)
    _confirm(db, message_id)

    assert apply_open(_session(db, calendar)) == [(message_id, "created")]

    (((where, _event_id), body),) = calendar.events.items()
    assert where == CALENDAR
    assert body["summary"] == "Call with Sara"
    assert body["attendees"] == [{"email": SARA}]
    assert message_id in body["description"]
    assert _state(db, message_id)[:4] == ("decided", "created", 1, "created")


@pytest.mark.integration
def test_a_write_google_refuses_fails_with_its_reason(db: psycopg.Connection) -> None:
    message_id = _park(db, dry_run=False)
    _confirm(db, message_id)

    assert apply_open(_session(db, Calendar(dry_run=False, rejects=True))) == [
        (message_id, "failed")
    ]
    assert _outcomes(db, message_id) == [("confirm", "failed", audit.REASONS["provider"])]
    assert _state(db, message_id)[3] == "failed"


@pytest.mark.integration
@pytest.mark.parametrize("attempts_spent", [False, True])
def test_a_decision_on_a_proposal_already_final_settles_as_that(
    db: psycopg.Connection, attempts_spent: bool
) -> None:
    """The ledger is what says the event was made: a decision still open
    beside a final ledger settles as the ledger says, and nothing runs."""
    calendar = Calendar(dry_run=False)
    message_id = _park(db, dry_run=False)
    _confirm(db, message_id)
    db.execute(
        """
        UPDATE processed_messages SET status = 'created', calendar_event_id = 'made-earlier'
         WHERE gmail_message_id = %s
        """,
        (message_id,),
    )
    if attempts_spent:
        _spend_attempts(db, message_id)

    assert apply_open(_session(db, calendar)) == [(message_id, "created")]
    assert _state(db, message_id)[:2] == ("decided", "created")
    assert calendar.calls == 0


@pytest.mark.integration
def test_a_guest_the_owner_typed_needs_no_allow(db: psycopg.Connection) -> None:
    message_id = _park(db, typed=f"invite {SARA} to a call on Monday")
    _confirm(db, message_id)
    assert apply_open(_session(db, Calendar())) == [(message_id, "skipped")]


@pytest.mark.integration
def test_a_guest_neither_typed_nor_allowed_comes_back_marked(db: psycopg.Connection) -> None:
    """Inside are the addresses typed in the request, read again at the
    Confirm, and the confirmed contacts (D8)."""
    message_id = _park(db, typed="call with Ali on Monday", guests=(ALI,), sources={ALI: "contact"})
    _confirm(db, message_id)

    assert apply_open(_session(db, Calendar())) == [(message_id, "no_effect")]

    assert _state(db, message_id)[:3] == ("pending", None, 2)
    outside = db.execute(
        "SELECT payload->'outside_guests' FROM proposals WHERE message_id = %s", (message_id,)
    ).fetchone()
    assert outside == ([ALI],)
    assert _outcomes(db, message_id) == [("confirm", "no_effect", audit.REASONS["guests"])]

    # Allowed, the next Confirm runs.
    contacts.allow(db, ALI, via="web", key=KEY)
    _confirm(db, message_id)
    assert apply_open(_session(db, Calendar())) == [(message_id, "skipped")]


@pytest.mark.integration
def test_a_returned_proposal_keeps_its_card_and_is_shown_at_its_new_generation(
    db: psycopg.Connection,
) -> None:
    """Re-parked from the stored row, which is all a chat proposal has: the
    conflicts and the guests' sources stay, and the card announced carries
    the generation a Confirm from it needs."""
    message_id = _park(
        db,
        typed="call with Ali on Monday",
        guests=(ALI,),
        sources={ALI: "contact"},
        conflicts=("Overlaps Standup",),
    )
    _confirm(db, message_id)
    announced: list[ProposalRecord] = []

    assert apply_open(_session(db, Calendar()), announce=announced.append) == [
        (message_id, "no_effect")
    ]

    [record] = announced
    assert (record.message_id, record.generation) == (message_id, 2)
    stored = db.execute(
        "SELECT generation, payload FROM proposals WHERE message_id = %s", (message_id,)
    ).fetchone()
    assert stored is not None and stored[0] == record.generation
    payload = stored[1]
    assert payload["conflicts"] == ["Overlaps Standup"]
    assert payload["guest_sources"] == {ALI: "contact"}
    assert payload["outside_guests"] == [ALI]
    assert payload["proposed"]["title"] == "Call with Sara"


@pytest.mark.integration
def test_a_request_retention_has_cleared_names_no_guest(db: psycopg.Connection) -> None:
    message_id = _park(db, typed=f"invite {SARA} to a call on Monday")
    assert unconfirmed_outsiders(db, NoGmail(), message_id, [SARA]) == []

    db.execute("UPDATE chat_turns SET typed = NULL")
    assert unconfirmed_outsiders(db, NoGmail(), message_id, [SARA]) == [SARA]


@pytest.mark.integration
def test_a_proposal_bound_to_another_calendar_comes_back(db: psycopg.Connection) -> None:
    message_id = _park(db)
    _confirm(db, message_id)
    moved = Calendar(calendar_id="another-calendar")

    assert apply_open(_session(db, moved)) == [(message_id, "no_effect")]
    assert _outcomes(db, message_id) == [("confirm", "no_effect", audit.REASONS["changed"])]
    assert moved.calls == 0


@pytest.mark.integration
def test_a_proposal_from_the_other_mode_is_expired_never_run(db: psycopg.Connection) -> None:
    calendar = Calendar(dry_run=False)
    message_id = _park(db, dry_run=True)
    _confirm(db, message_id)
    session = _session(db, calendar)

    assert apply_open(session) == [(message_id, "no_effect")]
    assert apply_open(session) == [(message_id, "rejected")]  # the expiry's sweep
    assert _state(db, message_id)[3:] == ("rejected", audit.REASONS["mode"])
    assert calendar.calls == 0


# --- Cancel, the sweep, Edit ------------------------------------------------------------


@pytest.mark.integration
def test_cancel_ends_it_as_declined(db: psycopg.Connection) -> None:
    message_id = _park(db)
    decide(db, message_id, action="cancel", revision=1, via="web")

    assert apply_open(_session(db, Calendar())) == [(message_id, "rejected")]
    assert _state(db, message_id) == ("decided", "rejected", 1, "rejected", DECLINED)


@pytest.mark.integration
def test_the_sweep_ends_it_with_its_reason(db: psycopg.Connection) -> None:
    message_id = _park(db)
    reason = audit.REASONS["mode"]
    decide(db, message_id, action="sweep", revision=1, via="sweep", reason=reason)

    assert apply_open(_session(db, Calendar())) == [(message_id, "rejected")]
    assert _state(db, message_id)[3:] == ("rejected", reason)


@pytest.mark.integration
@pytest.mark.parametrize("via", ["web", "telegram", "cli"])
def test_an_edit_is_refused_from_every_channel(db: psycopg.Connection, via: Via) -> None:
    message_id = _park(db)
    result = decide(
        db, message_id, action="edit", revision=1, via=via, correction="make it Tuesday"
    )
    assert (result.status, result.detail) == (
        "invalid",
        "a proposal from chat cannot be edited; ask again",
    )
    assert _state(db, message_id)[0] == "pending"


@pytest.mark.integration
def test_an_edit_recorded_anyway_goes_back_to_the_owner(db: psycopg.Connection) -> None:
    """One written past `decide()` -- by hand, or by a channel yet to come --
    runs nothing: the proposal comes back as it was."""
    message_id = _park(db)
    db.execute("UPDATE proposals SET status = 'deciding' WHERE message_id = %s", (message_id,))
    db.execute(
        """
        INSERT INTO decisions (message_id, revision, action, via, action_type,
                               pipeline_version, correction, decided_at, latency_seconds)
        VALUES (%s, 1, 'edit', 'web', 'calendar_invite', 'chat-test', 'make it Tuesday',
                now(), 0)
        """,
        (message_id,),
    )
    calendar = Calendar()

    assert apply_open(_session(db, calendar)) == [(message_id, "no_effect")]
    assert _outcomes(db, message_id) == [("edit", "no_effect", audit.REASONS["settled"])]
    assert _state(db, message_id)[:3] == ("pending", None, 2)
    assert calendar.calls == 0


# --- Withdraw and the pause --------------------------------------------------------------


@pytest.mark.integration
def test_withdraw_while_paused_returns_it_and_nothing_is_written(db: psycopg.Connection) -> None:
    calendar = Calendar(dry_run=False)
    message_id = _park(db, dry_run=False)
    decision = _confirm(db, message_id)
    control.switch(db, paused=True, via="web")
    session = _session(db, calendar)

    assert apply_open(session) == []  # paused: the Confirm waits
    assert request_withdraw(db, decision.decision_id) == "requested"
    assert apply_open(session) == [(message_id, "withdrawn")]

    assert calendar.calls == 0
    assert _state(db, message_id)[:3] == ("pending", None, 2)
    assert _outcomes(db, message_id) == [("confirm", "no_effect", audit.REASONS["withdrawn"])]
    assert _audited(db, message_id) == ["decision_withdrawn"]


@pytest.mark.integration
def test_a_withdraw_is_declined_once_the_write_has_begun(db: psycopg.Connection) -> None:
    calendar = Calendar(dry_run=False, fail_after=1)
    message_id = _park(db, dry_run=False)
    decision = _confirm(db, message_id)
    session = _session(db, calendar)
    assert apply_open(session) == [(message_id, "retrying")]

    request_withdraw(db, decision.decision_id)
    assert apply_open(session) == [(message_id, "declined")]
    assert _audited(db, message_id) == ["withdraw_declined"]

    db.execute("UPDATE decisions SET next_attempt_at = now() WHERE message_id = %s", (message_id,))
    assert apply_open(session) == [(message_id, "created")]  # and it goes on as it was
    assert len(calendar.events) == 1


@pytest.mark.integration
def test_a_withdraw_checks_the_action_again_under_its_lock(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another worker started the write after this one read the action."""
    message_id = _park(db)
    decision = _confirm(db, message_id)
    request_withdraw(db, decision.decision_id)
    original = worker._with_action_status

    def read_then_started(connection: psycopg.Connection, seen: Any) -> Any:
        read = original(connection, seen)
        db.execute(
            "UPDATE outbound_actions SET status = 'executing', calendar_id = 'c', event_id = 'e'"
        )
        return read

    monkeypatch.setattr(worker, "_with_action_status", read_then_started)

    assert apply_open(_session(db, Calendar())) == [(message_id, "declined")]
    assert _state(db, message_id)[0] == "deciding"


@pytest.mark.integration
def test_a_withdraw_made_during_the_checks_stops_the_write(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    calendar = Calendar(dry_run=False)
    message_id = _park(db, dry_run=False)
    decision = _confirm(db, message_id)
    original = worker._check_chat_confirm

    def withdraw_then_check(*args: Any, **kwargs: Any) -> str | None:
        request_withdraw(db, decision.decision_id)  # tapped while the checks run
        return original(*args, **kwargs)

    monkeypatch.setattr(worker, "_check_chat_confirm", withdraw_then_check)

    assert apply_open(_session(db, calendar)) == [(message_id, "withdrawn")]
    assert calendar.calls == 0


@pytest.mark.integration
def test_a_pause_pressed_during_the_checks_holds_the_write_for_withdraw(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    calendar = Calendar(dry_run=False)
    message_id = _park(db, dry_run=False)
    decision = _confirm(db, message_id)
    original = worker._check_chat_confirm

    def pause_then_check(*args: Any, **kwargs: Any) -> str | None:
        control.switch(db, paused=True, via="web")  # tapped while the checks run
        return original(*args, **kwargs)

    monkeypatch.setattr(worker, "_check_chat_confirm", pause_then_check)
    session = _session(db, calendar)

    assert apply_open(session) == [(message_id, "paused")]
    assert calendar.calls == 0
    request_withdraw(db, decision.decision_id)
    assert apply_open(session) == [(message_id, "withdrawn")]
    assert calendar.calls == 0


@pytest.mark.integration
def test_withdrawing_a_proposal_from_the_other_mode_is_recorded_as_its_expiry(
    db: psycopg.Connection,
) -> None:
    calendar = Calendar(dry_run=False)
    message_id = _park(db, dry_run=True)
    decision = _confirm(db, message_id)
    request_withdraw(db, decision.decision_id)

    assert apply_open(_session(db, calendar)) == [(message_id, "expired")]
    assert _audited(db, message_id) == ["proposal_expired"]
    assert calendar.calls == 0


# --- giving up ---------------------------------------------------------------------------


def _spend_attempts(conn: psycopg.Connection, message_id: str) -> None:
    conn.execute(
        "UPDATE decisions SET attempts = %s, next_attempt_at = now() WHERE message_id = %s",
        (MAX_ATTEMPTS, message_id),
    )


@pytest.mark.integration
def test_giving_up_on_a_write_begun_looks_it_up(db: psycopg.Connection) -> None:
    """The write landed though the call failed: found, it settles as created."""
    calendar = Calendar(dry_run=False, fail_after=1)
    message_id = _park(db, dry_run=False)
    _confirm(db, message_id)
    session = _session(db, calendar)

    assert apply_open(session) == [(message_id, "retrying")]
    _spend_attempts(db, message_id)
    assert apply_open(session) == [(message_id, "created")]
    assert _state(db, message_id)[:4] == ("decided", "created", 1, "created")


@pytest.mark.integration
def test_giving_up_never_settles_a_write_on_a_guess(db: psycopg.Connection) -> None:
    calendar = Calendar(dry_run=False, fail_after=1, fail_find=1)
    message_id = _park(db, dry_run=False)
    _confirm(db, message_id)
    session = _session(db, calendar)

    apply_open(session)
    _spend_attempts(db, message_id)
    assert apply_open(session) == [(message_id, "unconfirmed")]
    assert _outcomes(db, message_id) == [("confirm", None, None)]  # still open


@pytest.mark.integration
def test_giving_up_before_any_write_returns_it(db: psycopg.Connection) -> None:
    message_id = _park(db)
    _confirm(db, message_id)
    _spend_attempts(db, message_id)

    assert apply_open(_session(db, Calendar())) == [(message_id, "no_effect")]
    assert _outcomes(db, message_id) == [("confirm", "no_effect", "attempts exhausted")]
    assert _state(db, message_id)[:3] == ("pending", None, 2)


@pytest.mark.integration
def test_giving_up_after_the_mode_changed_expires_it(db: psycopg.Connection) -> None:
    calendar = Calendar(dry_run=False)
    message_id = _park(db, dry_run=True)
    _confirm(db, message_id)
    _spend_attempts(db, message_id)
    session = _session(db, calendar)

    assert apply_open(session) == [(message_id, "no_effect")]
    assert apply_open(session) == [(message_id, "rejected")]  # the expiry's sweep
    assert _state(db, message_id)[3:] == ("rejected", audit.REASONS["mode"])
    assert calendar.calls == 0


@pytest.mark.integration
def test_giving_up_on_a_dry_run_the_registry_recorded_settles_it(db: psycopg.Connection) -> None:
    """Only the settle was lost: the action says the dry run ran."""
    message_id = _park(db)
    decision = _confirm(db, message_id)
    db.execute(
        "UPDATE outbound_actions SET status = 'dry_run' WHERE decision_id = %s",
        (decision.decision_id,),
    )
    _spend_attempts(db, message_id)

    assert apply_open(_session(db, Calendar())) == [(message_id, "skipped")]
    assert _state(db, message_id) == ("decided", "skipped", 1, "skipped", "dry_run")


# --- reconciliation reads no thread for chat ----------------------------------------------


@pytest.mark.integration
def test_reconcile_closes_a_settled_chat_proposal_without_a_thread(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.channel.reconcile import reconcile

    message_id = _park(db)
    db.execute(
        "UPDATE processed_messages SET status = 'rejected' WHERE gmail_message_id = %s",
        (message_id,),
    )
    session = _session(db, Calendar())
    monkeypatch.setattr(GraphSession, "thread", _no_thread)
    result = reconcile(session, bind_and_expire=True)

    assert (result.closed, result.errors) == (1, 0)
    assert _state(db, message_id)[:2] == ("decided", "rejected")


@pytest.mark.integration
def test_reconcile_resyncs_no_chat_row_from_a_thread(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Were they mail's, reconciliation would read a thread for both: a
    ledger row with no proposal, and a proposal with no hash. `park_chat`
    leaves neither, since it claims, parks and binds at once."""
    from app.channel.reconcile import reconcile

    unbound = _park(db)
    db.execute("UPDATE proposals SET args_hash = NULL WHERE message_id = %s", (unbound,))
    assert MessageLedger(db).claim("chat-99-1", "chat-99", source="chat")
    db.execute(
        "UPDATE processed_messages SET status = 'awaiting_approval'"
        " WHERE gmail_message_id = 'chat-99-1'"
    )
    monkeypatch.setattr(GraphSession, "thread", _no_thread)

    result = reconcile(_session(db, Calendar()), bind_and_expire=True)

    assert (result.recorded, result.errors) == (0, 0)
