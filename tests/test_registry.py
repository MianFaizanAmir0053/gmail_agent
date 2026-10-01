"""The registry (M17, D1-D3): every side effect goes through it, every
approval is checked again before anything is marked started, and a write
interrupted halfway is finished from what was stored, never rebuilt."""

from __future__ import annotations

import ast
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from app.channel.decide import card_token, decide
from app.channel.park import proposal_from, write_park
from app.contracts import ExtractionResult
from app.google.calendar import WriteRejectedError
from app.policy import audit
from app.policy.hashing import HOLD, INVITE, Binding, args_key, event_args, event_id_for
from app.policy.registry import TOOLS, Approval, PausedError, Registry, Tier
from app.store.ledger import MessageLedger, MessageStatus
from app.tools.calendar_tool import CreateEventInput

ROOT = Path(__file__).resolve().parent.parent
KEY = args_key("test-key")
CALENDAR = "test-calendar"

PROPOSED: dict[str, Any] = {
    "is_meeting": True,
    "title": "Design review",
    "start_utc": "2026-10-05T11:00:00Z",
    "end_utc": "2026-10-05T12:00:00Z",
    "timezone": "Asia/Karachi",
    "attendees": [],
    "location": None,
    "confidence": 0.9,
    "reasoning": "Sara asked to meet",
}


# --- what is registered -------------------------------------------------------


def test_the_registered_tools_are_pinned() -> None:
    """Registering a new INTERNAL or EXTERNAL tool is the owner's call (ask
    first): this test is where that shows."""
    assert TOOLS == {
        "calendar.freebusy": Tier.READ,
        HOLD: Tier.INTERNAL,
        INVITE: Tier.EXTERNAL,
    }
    assert set(TOOLS) == audit.TOOLS


EVENT_WRITES = frozenset({"insert", "patch", "update", "import_", "move", "delete", "quickAdd"})
"""The Calendar API's writes on an `events()` resource."""


def _calendar_writes(path: Path) -> list[str]:
    """Calls that write to a calendar: `create_event` and `delete_event`
    anywhere, and any of the API's event writes on a receiver that is an
    `events()` resource or is named for a calendar."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        name = node.func.attr
        receiver = ast.unparse(node.func.value).lower()
        if name in ("create_event", "delete_event") or (
            name in EVENT_WRITES and ("events()" in receiver or "calendar" in receiver)
        ):
            found.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
    return found


def test_the_scan_sees_writes_however_they_are_spelled() -> None:
    """The scan is only as good as what it recognises."""
    sample = ROOT / "tests" / "_scan_sample.py"
    try:
        sample.write_text(
            "svc.events().insert(calendarId='c', body={}).execute()\n"
            "svc.events().patch(calendarId='c', eventId='e', body={}).execute()\n"
            "self._calendar.update(x)\n"
            "settings.update(x)\n",
            encoding="utf-8",
        )
        assert sorted(line.rsplit(":", 1)[1] for line in _calendar_writes(sample)) == [
            "1",
            "2",
            "3",
        ]
    finally:
        sample.unlink()


def test_only_the_registry_writes_to_the_calendar() -> None:
    """Two operator tools are named exceptions, both against the test calendar
    only; the calendar client itself is where the writes are defined."""
    allowed = {
        "app/policy/registry.py",
        "app/google/smoke.py",
        "app/jobs/calendar_probe.py",
        "app/google/calendar.py",
    }
    found = [
        call
        for path in sorted((ROOT / "app").rglob("*.py"))
        if path.relative_to(ROOT).as_posix() not in allowed
        for call in _calendar_writes(path)
    ]
    assert found == []


# --- a calendar that remembers ------------------------------------------------


@dataclass
class Writer:
    """Stands in for `CalendarClient`'s writes, keeping events by calendar and
    id, and able to fail just before or just after Google makes one."""

    dry_run: bool = False
    calendar_id: str = CALENDAR
    events: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    calls: int = 0
    """Every insert asked for, made or not: a blind second insert shows here
    even where `CalendarClient` would turn Google's `409` into success."""
    inserts: int = 0
    lookups: int = 0
    fail_before: bool = False
    fail_after: bool = False
    reject: bool = False

    def insert(self, calendar_id: str, body: dict[str, Any], *, event_id: str) -> str | None:
        self.calls += 1
        if self.dry_run:
            return None
        if self.reject:
            raise WriteRejectedError("the calendar refused the request (400)")
        if self.fail_before:
            raise ConnectionError("network down before the insert")
        if (calendar_id, event_id) not in self.events:
            self.inserts += 1
            self.events[(calendar_id, event_id)] = body
        if self.fail_after:
            raise ConnectionError("network down after Google made the event")
        return event_id

    def find(self, calendar_id: str, event_id: str) -> str | None:
        self.lookups += 1
        return event_id if (calendar_id, event_id) in self.events else None


# --- an approved proposal (Postgres) ---------------------------------------------


def _approved(
    conn: psycopg.Connection, *, attendees: list[str] | None = None, message_id: str = "m1"
) -> Approval:
    """Park the message, confirm it from its card, and return the approval it
    recorded."""
    proposed = dict(PROPOSED, attendees=attendees or [])
    pending = {
        "proposed": proposed,
        "conflicts": [],
        "dry_run": True,
        "review_issues": [],
        "action_type": "calendar_invite" if attendees else "calendar_hold",
        "pipeline_version": "0123456789ab",
    }
    MessageLedger(conn).claim(message_id, message_id)
    with conn.transaction():
        write_park(
            conn,
            proposal_from(message_id, pending, 1, Binding(calendar_id=CALENDAR, key=KEY)),
            ledger_status=MessageStatus.CLAIMED,
        )
    row = conn.execute(
        "SELECT args_hash, dry_run, generation FROM proposals WHERE message_id = %s",
        (message_id,),
    ).fetchone()
    assert row is not None
    result = decide(
        conn,
        message_id,
        action="confirm",
        revision=1,
        via="web",
        token=card_token(row[0], row[1], row[2]),
        dry_run=row[1],
    )
    assert result.status == "queued"
    action = conn.execute(
        "SELECT id, nonce FROM outbound_actions WHERE decision_id = %s", (result.decision_id,)
    ).fetchone()
    assert action is not None
    return Approval(action_id=action[0], nonce=action[1])


def _args(*, message_id: str = "m1", **changes: Any) -> CreateEventInput:
    extraction = ExtractionResult.model_validate(dict(PROPOSED, **changes))
    return event_args(extraction, message_id)


def _action(conn: psycopg.Connection) -> tuple[Any, ...]:
    row = conn.execute(
        "SELECT status, event_id, calendar_id, request IS NOT NULL, reason"
        " FROM outbound_actions WHERE message_id = 'm1'"
    ).fetchone()
    assert row is not None
    return tuple(row)


def _last_audit(conn: psycopg.Connection) -> tuple[Any, ...]:
    row = conn.execute(
        "SELECT kind, reason FROM audit_log WHERE message_id = 'm1' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    return tuple(row)


def _registry(conn: psycopg.Connection, writer: Writer) -> Registry:
    return Registry(conn, writer, key=KEY)


# --- executing an approval ------------------------------------------------------------


@pytest.mark.integration
def test_an_approved_hold_is_made_once_with_its_deterministic_id(
    conn: psycopg.Connection,
) -> None:
    approval = _approved(conn)
    conn.execute("UPDATE outbound_actions SET dry_run = false")  # approved for real
    writer = Writer()

    outcome = _registry(conn, writer).execute(HOLD, _args(), approval=approval, message_id="m1")

    stored = conn.execute("SELECT args_hash FROM outbound_actions").fetchone()
    assert stored is not None
    expected = event_id_for(KEY, "m1", stored[0])
    assert (outcome.status, outcome.event_id) == ("created", expected)
    assert writer.calls == 1 and (CALENDAR, expected) in writer.events
    # Done: the request, which held the event's content, is gone.
    assert _action(conn) == ("done", expected, CALENDAR, False, None)


@pytest.mark.integration
def test_under_dry_run_nothing_is_written(conn: psycopg.Connection) -> None:
    approval = _approved(conn)
    writer = Writer(dry_run=True)

    outcome = _registry(conn, writer).execute(HOLD, _args(), approval=approval, message_id="m1")

    assert outcome.status == "dry_run"
    assert writer.calls == 0 and writer.lookups == 0
    assert _action(conn)[0] == "dry_run"


@pytest.mark.integration
@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"title": "Design review (moved)"}, "mismatch"),
        ({"start_utc": "2026-10-05T11:30:00Z"}, "mismatch"),
    ],
)
def test_arguments_that_differ_from_the_approval_are_refused(
    conn: psycopg.Connection, change: dict[str, Any], reason: str
) -> None:
    approval = _approved(conn)
    writer = Writer(dry_run=True)

    outcome = _registry(conn, writer).execute(
        HOLD, _args(**change), approval=approval, message_id="m1"
    )

    assert (outcome.status, outcome.reason) == ("refused", audit.REASONS[reason])
    assert _action(conn)[0] == "refused"
    assert _last_audit(conn) == ("action_refused", audit.REASONS[reason])


@pytest.mark.integration
def test_another_nonce_is_refused(conn: psycopg.Connection) -> None:
    approval = _approved(conn)
    forged = Approval(action_id=approval.action_id, nonce="0" * 32)

    outcome = _registry(conn, Writer(dry_run=True)).execute(
        HOLD, _args(), approval=forged, message_id="m1"
    )

    assert (outcome.status, outcome.reason) == ("refused", audit.REASONS["nonce"])
    assert _last_audit(conn) == ("action_refused", audit.REASONS["nonce"])
    # The row is another decision's to settle, not a forger's.
    assert _action(conn)[0] == "approved"


@pytest.mark.integration
def test_an_approval_under_another_mode_is_refused(conn: psycopg.Connection) -> None:
    """Approved under dry run, it never runs live (D2)."""
    approval = _approved(conn)
    writer = Writer(dry_run=False)

    outcome = _registry(conn, writer).execute(HOLD, _args(), approval=approval, message_id="m1")

    assert (outcome.status, outcome.reason) == ("refused", audit.REASONS["mode"])
    assert writer.inserts == 0
    assert _last_audit(conn) == ("action_refused", audit.REASONS["mode"])


@pytest.mark.integration
def test_a_hold_with_guests_is_refused(conn: psycopg.Connection) -> None:
    approval = _approved(conn)

    outcome = _registry(conn, Writer(dry_run=True)).execute(
        HOLD, _args(attendees=["sara@example.com"]), approval=approval, message_id="m1"
    )

    assert (outcome.status, outcome.reason) == ("refused", audit.REASONS["hold_with_guests"])


@pytest.mark.integration
def test_a_time_without_a_zone_is_refused_not_raised(conn: psycopg.Connection) -> None:
    """Google would read it in some zone of its choosing (M04)."""
    approval = _approved(conn)
    naive = _args().model_copy(update={"start_utc": datetime(2026, 10, 5, 11, 0)})

    outcome = _registry(conn, Writer(dry_run=True)).execute(
        HOLD, naive, approval=approval, message_id="m1"
    )

    assert (outcome.status, outcome.reason) == ("refused", audit.REASONS["naive"])
    assert _action(conn)[0] == "refused"


@pytest.mark.integration
def test_without_an_approval_nothing_runs(conn: psycopg.Connection) -> None:
    _approved(conn)

    outcome = _registry(conn, Writer()).execute(HOLD, _args(), approval=None, message_id="m1")

    assert outcome.status == "refused"


@pytest.mark.integration
def test_while_paused_nothing_new_runs(conn: psycopg.Connection) -> None:
    approval = _approved(conn)
    conn.execute("UPDATE control SET paused = true")

    with pytest.raises(PausedError):
        _registry(conn, Writer(dry_run=True)).execute(
            HOLD, _args(), approval=approval, message_id="m1"
        )

    assert _action(conn)[0] == "approved"


# --- finishing an interrupted write (D3) -------------------------------------------------


@pytest.mark.integration
def test_a_write_cut_off_before_google_made_it_is_made_on_the_next_attempt(
    conn: psycopg.Connection,
) -> None:
    approval = _approved(conn)
    conn.execute("UPDATE outbound_actions SET dry_run = false")
    writer = Writer(fail_before=True)
    with pytest.raises(ConnectionError):
        _registry(conn, writer).execute(HOLD, _args(), approval=approval, message_id="m1")
    assert _action(conn)[0] == "executing"

    writer.fail_before = False
    outcome = _registry(conn, writer).execute(HOLD, _args(), approval=approval, message_id="m1")

    assert outcome.status == "created"
    assert writer.lookups == 1  # asked first
    assert (writer.calls, writer.inserts) == (2, 1)  # the cut-off call, then the real one


@pytest.mark.integration
def test_a_write_cut_off_after_google_made_it_is_found_not_made_again(
    conn: psycopg.Connection,
) -> None:
    approval = _approved(conn)
    conn.execute("UPDATE outbound_actions SET dry_run = false")
    writer = Writer(fail_after=True)
    with pytest.raises(ConnectionError):
        _registry(conn, writer).execute(HOLD, _args(), approval=approval, message_id="m1")

    writer.fail_after = False
    outcome = _registry(conn, writer).execute(HOLD, _args(), approval=approval, message_id="m1")

    assert outcome.status == "created"
    assert writer.calls == 1  # found, so never sent again


@pytest.mark.integration
def test_a_later_attempt_replays_what_was_stored_not_what_the_code_builds_now(
    conn: psycopg.Connection,
) -> None:
    """A deploy that moved the calendar, or changed the arguments' code, must
    not book an unapproved event, or a second one where the first cannot be
    seen."""
    approval = _approved(conn)
    conn.execute("UPDATE outbound_actions SET dry_run = false")
    writer = Writer(fail_before=True)
    with pytest.raises(ConnectionError):
        _registry(conn, writer).execute(HOLD, _args(), approval=approval, message_id="m1")

    moved = Writer(calendar_id="another-calendar", events=writer.events)
    outcome = _registry(conn, moved).execute(
        HOLD, _args(title="Rebuilt differently"), approval=approval, message_id="m1"
    )

    assert outcome.status == "created"
    (calendar, _), body = next(iter(moved.events.items()))
    assert calendar == CALENDAR
    assert body["summary"] == "Design review"


@pytest.mark.integration
def test_a_finished_write_returns_its_stored_outcome_and_books_nothing(
    conn: psycopg.Connection,
) -> None:
    approval = _approved(conn)
    conn.execute("UPDATE outbound_actions SET dry_run = false")
    writer = Writer()
    first = _registry(conn, writer).execute(HOLD, _args(), approval=approval, message_id="m1")

    again = _registry(conn, writer).execute(HOLD, _args(), approval=approval, message_id="m1")

    assert again == first
    assert writer.calls == 1 and writer.lookups == 0


@pytest.mark.integration
def test_under_dry_run_a_later_attempt_only_looks(conn: psycopg.Connection) -> None:
    """The kill switch never writes, even to finish a write begun live."""
    approval = _approved(conn)
    conn.execute("UPDATE outbound_actions SET dry_run = false")
    with pytest.raises(ConnectionError):
        _registry(conn, Writer(fail_before=True)).execute(
            HOLD, _args(), approval=approval, message_id="m1"
        )

    switched = Writer(dry_run=True)
    outcome = _registry(conn, switched).execute(HOLD, _args(), approval=approval, message_id="m1")

    assert (outcome.status, outcome.reason) == ("refused", audit.REASONS["mode"])
    assert switched.calls == 0 and switched.lookups == 1


@pytest.mark.integration
def test_an_approval_for_a_hold_never_runs_an_invite(conn: psycopg.Connection) -> None:
    """A guest added since the approval turns the hold into an invite. It is
    refused on the action itself, with the decision it belongs to."""
    approval = _approved(conn)

    outcome = _registry(conn, Writer(dry_run=True)).execute(
        INVITE, _args(attendees=["sara@example.com"]), approval=approval, message_id="m1"
    )

    assert (outcome.status, outcome.reason) == ("refused", audit.REASONS["mismatch"])
    assert _action(conn)[0] == "refused"
    recorded = conn.execute(
        "SELECT decision_id IS NOT NULL, args_hash IS NOT NULL FROM audit_log"
        " WHERE message_id = 'm1' AND kind = 'action_refused'"
    ).fetchone()
    assert recorded == (True, True)


@pytest.mark.integration
def test_a_write_begun_is_finished_whatever_the_current_code_builds(
    conn: psycopg.Connection,
) -> None:
    """A deploy that now reads a guest into the extraction builds an invite;
    the write begun as a hold is still finished as stored (D3)."""
    approval = _approved(conn)
    conn.execute("UPDATE outbound_actions SET dry_run = false")
    writer = Writer(fail_before=True)
    with pytest.raises(ConnectionError):
        _registry(conn, writer).execute(HOLD, _args(), approval=approval, message_id="m1")

    writer.fail_before = False
    outcome = _registry(conn, writer).execute(
        INVITE, _args(attendees=["sara@example.com"]), approval=approval, message_id="m1"
    )

    assert outcome.status == "created"
    [body] = writer.events.values()
    assert "attendees" not in body


@pytest.mark.integration
def test_a_request_google_refuses_fails_at_once(conn: psycopg.Connection) -> None:
    """A `400` would be refused again on every attempt."""
    approval = _approved(conn)
    conn.execute("UPDATE outbound_actions SET dry_run = false")

    outcome = _registry(conn, Writer(reject=True)).execute(
        HOLD, _args(), approval=approval, message_id="m1"
    )

    assert (outcome.status, outcome.reason) == ("refused", audit.REASONS["provider"])
    assert _action(conn) == (
        "failed",
        event_id_for(KEY, "m1", _hash(conn)),
        CALENDAR,
        False,
        audit.REASONS["provider"],
    )
    assert _last_audit(conn) == ("action_failed", audit.REASONS["provider"])


def _hash(conn: psycopg.Connection) -> str:
    row = conn.execute("SELECT args_hash FROM outbound_actions WHERE message_id = 'm1'").fetchone()
    assert row is not None
    return str(row[0])


# --- what other connections see (Postgres, committed) -----------------------------


@dataclass
class Peeking(Writer):
    """Reads the action from another connection at the moment Google is
    called: what a crash at that instant would leave behind."""

    database_url: str = ""
    seen: tuple[Any, ...] | None = None

    def insert(self, calendar_id: str, body: dict[str, Any], *, event_id: str) -> str | None:
        with psycopg.connect(self.database_url) as other:
            row = other.execute(
                "SELECT status, calendar_id, request IS NOT NULL FROM outbound_actions"
                " WHERE event_id = %s",
                (event_id,),
            ).fetchone()
        self.seen = None if row is None else tuple(row)
        return super().insert(calendar_id, body, event_id=event_id)


@pytest.fixture
def committed_approval(migrated_database: str) -> Iterator[tuple[str, Approval]]:
    """A Confirm recorded for real, approved live, on a message of its own."""
    message_id = f"commit-{uuid.uuid4().hex[:12]}"
    with psycopg.connect(migrated_database, autocommit=True) as setup:
        approval = _approved(setup, message_id=message_id)
        setup.execute(
            "UPDATE outbound_actions SET dry_run = false WHERE message_id = %s", (message_id,)
        )
    yield message_id, approval
    with psycopg.connect(migrated_database, autocommit=True) as cleanup:
        # Approvals and decisions refuse cascading deletes, so they go first.
        cleanup.execute("DELETE FROM outbound_actions WHERE message_id = %s", (message_id,))
        cleanup.execute("DELETE FROM decisions WHERE message_id = %s", (message_id,))
        cleanup.execute("DELETE FROM processed_messages WHERE gmail_message_id = %s", (message_id,))


@pytest.mark.integration
def test_the_start_is_committed_before_google_is_called(
    migrated_database: str, committed_approval: tuple[str, Approval]
) -> None:
    """If the process died during the call, the next attempt would know which
    event to look for, and on which calendar."""
    message_id, approval = committed_approval
    writer = Peeking(database_url=migrated_database)

    with psycopg.connect(migrated_database, autocommit=True) as conn:
        outcome = Registry(conn, writer, key=KEY).execute(
            HOLD, _args(message_id=message_id), approval=approval, message_id=message_id
        )

    assert outcome.status == "created"
    assert writer.seen == ("executing", CALENDAR, True)
