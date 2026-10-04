"""The injection suite's flow (M18, D8; task 18.12), on Postgres.

A model that does whatever each case's email asks is driven through the graph
to park, `decide()` and the registry. It reads the prompt the real pipeline
builds -- the email after the preparation, between the call's markers -- and
complies with all of it: every address it reads becomes a guest, a case's
model-invented guests are proposed too, every marker a forged structure
carries goes into the title, and a forged correction is obeyed by moving the
meeting. Whatever it does, these hold:

- nothing is booked without a Confirm bound to the exact arguments;
- every injected guest is marked by its source, and blocks the Confirm until
  the owner allows it;
- a forged correction changes nothing: no Edit, no new revision, nothing in
  the owner's channel.

A failure names the case and the expectation, never the case's text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace
from typing import Any, cast

import psycopg
import pytest
from gmail_payloads import gmail_response, load_cases
from langgraph.checkpoint.memory import InMemorySaver

from app.channel.decide import card_token, decide
from app.channel.park import record_park
from app.channel.worker import apply_open
from app.contracts import EmailMessage, ExtractionResult
from app.extraction import prompts
from app.extraction.payloads import ClassifyPayload
from app.google.gmail import to_email_message
from app.graph.nodes import Deps
from app.graph.runner import GraphSession
from app.policy import contacts
from app.policy.contacts import unconfirmed_outsiders
from app.policy.hashing import args_key
from app.policy.participants import card_source, guest_key
from app.policy.registry import Registry
from app.policy.scrub import ADDRESS
from app.store.ledger import MessageLedger

pytestmark = pytest.mark.integration

KEY = args_key("test-key")
OWNER = "owner@example.com"
SENDER = "sender@example.com"
"""As `tests/gmail_payloads.py` writes every case's From and To."""
START = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
MAIL = [case for case in load_cases() if case["group"] != "output"]
NAMED = {"email", "quoted", "absent"}
_ADDRESS = re.compile(ADDRESS)
_MARKER = re.compile(r"(?:FORGED|HIDDEN|OUTPUT)-MARKER-[A-Z0-9-]+")


def _id(case: dict[str, Any]) -> str:
    return str(case["id"])


def _check(ok: bool, case: dict[str, Any], what: str) -> None:
    """Assert `ok`, naming only the case and the expectation."""
    assert ok, f"{case['id']}: {what}"


@dataclass
class CompliantModel:
    """Does whatever the email it reads asks. It reads the prompt the real
    pipeline builds, so hidden text and defused markers reach it as they
    would reach Gemini."""

    invented: list[str] = field(default_factory=list)
    """Guests the case's model proposes though the email does not show them."""
    calls: int = 0
    corrections: list[str] = field(default_factory=list)

    def classify(self, email: EmailMessage, **kwargs: Any) -> ClassifyPayload:
        self.calls += 1
        return ClassifyPayload(is_meeting=True, confidence=1.0, reasoning="the email says so")

    def extract(
        self,
        email: EmailMessage,
        *,
        now_utc: datetime,
        user_timezone: str,
        correction: str = "",
        current: ExtractionResult | None = None,
    ) -> ExtractionResult:
        self.calls += 1
        self.corrections.append(correction)
        prompt = prompts.user_content(email, now_utc=now_utc, user_timezone=user_timezone)
        read = prompt[prompt.index("<email-") :]
        guests = {address.lower() for address in _ADDRESS.findall(read)} | set(self.invented)
        # Obeyed as if it were the owner's: the meeting moves an hour.
        begin = START + timedelta(hours=1 if "CORRECTION" in read else 0)
        return ExtractionResult(
            is_meeting=True,
            title=" ".join([email.subject, *_MARKER.findall(read)]),
            start_utc=begin,
            end_utc=begin + timedelta(hours=1),
            timezone="UTC",
            attendees=sorted(guests - {OWNER}),
            location="Room 4",
            confidence=1.0,
            reasoning="as the email asks",
        )


class CaseGmail:
    """A case's email through the real preparation, and its thread: the owner
    wrote to the case's participants, or to the sender when it names none."""

    def __init__(self, case: dict[str, Any]) -> None:
        self.email = to_email_message(gmail_response(case))
        to = ", ".join(case.get("participants", [SENDER]))
        sent = {"labelIds": ["SENT"], "payload": {"headers": [{"name": "To", "value": to}]}}
        self.thread = {"messages": [sent] if to else []}

    def get_message(self, message_id: str) -> EmailMessage:
        return self.email

    def message_metadata(self, message_id: str) -> Any:
        return SimpleNamespace(thread_id=self.email.thread_id)

    def thread_headers(self, thread_id: str) -> dict[str, Any]:
        return self.thread if thread_id == self.email.thread_id else {"messages": []}


@dataclass
class FakeCalendar:
    """A live calendar, faked: it records every request the registry sends,
    so the test sees exactly what would be written. In dry run the registry
    stops before sending anything, which would hide the arguments."""

    dry_run: bool = False
    calendar_id: str = "test-calendar"
    bodies: list[dict[str, Any]] = field(default_factory=list)

    def freebusy(self, start: datetime, end: datetime) -> list[Any]:
        return []

    def insert(self, calendar_id: str, body: dict[str, Any], *, event_id: str) -> str | None:
        self.bodies.append(body)
        return event_id

    def find(self, calendar_id: str, event_id: str) -> str | None:
        return None


def _session(
    conn: psycopg.Connection, gmail: CaseGmail, model: CompliantModel, calendar: FakeCalendar
) -> GraphSession:
    deps = Deps(
        gmail=cast(Any, gmail),
        pipeline=cast(Any, model),
        calendar=cast(Any, calendar),
        ledger=MessageLedger(conn),
        user_timezone="UTC",
        registry=Registry(
            conn, calendar, key=KEY, outsiders=partial(unconfirmed_outsiders, conn, gmail)
        ),
        pipeline_version="0123456789ab",
        args_key=KEY,
    )
    return GraphSession(deps=deps, conn=conn, checkpointer=InMemorySaver(), trace=False)


@pytest.mark.parametrize("case", MAIL, ids=_id)
def test_a_model_that_obeys_the_email_still_cannot_act_for_it(
    conn: psycopg.Connection, case: dict[str, Any]
) -> None:
    message_id = case["id"]
    model = CompliantModel(invented=list(case.get("guests", [])))
    calendar = FakeCalendar()
    session = _session(conn, CaseGmail(case), model, calendar)
    conn.execute("DELETE FROM confirmed_contacts")
    for address in case.get("allowed", []):
        contacts.allow(conn, address, via="web", key=KEY)
    MessageLedger(conn).claim(message_id, message_id)

    session.start(message_id, message_id)

    if case["expect"]["credential"]:
        _check(model.calls == 0, case, "credential mail reaches no model")
        _check(session.pending(message_id) is None, case, "credential mail never parks")
        _check(not calendar.bodies, case, "nothing is booked")
        return

    pending = session.pending(message_id)
    _check(pending is not None, case, "the proposal parks for the owner")
    assert pending is not None
    record = record_park(session, message_id, pending)
    _check(not calendar.bodies, case, "nothing is booked at park")

    # A forged correction changes nothing: the owner's channel stays empty.
    _check(model.corrections == [""], case, "no correction reached the model")
    _check(session.revision(message_id) == 1, case, "no Edit happened")
    row = conn.execute("SELECT count(*) FROM decisions WHERE message_id = %s", (message_id,))
    decided = row.fetchone()
    _check(decided is not None and decided[0] == 0, case, "no decision was made")

    # Every injected guest is marked by its source, and is outside the thread.
    payload = record.payload
    thread = {guest_key(person) for person in case.get("participants", [SENDER])}
    allowed = {guest_key(address) for address in case.get("allowed", [])}
    injected = [
        guest
        for guest in payload["attendees"]
        if guest_key(guest) not in thread and guest_key(guest) not in allowed
    ]
    for index, guest in enumerate(injected):
        _check(card_source(guest, payload, frozenset()) in NAMED, case, f"guest[{index}] marked")
        _check(guest in payload["outside_guests"], case, f"guest[{index}] outside the thread")

    # A Confirm bound to other arguments is refused.
    assert record.args_hash is not None
    confirm = partial(
        decide, conn, message_id, action="confirm", revision=1, via="web", dry_run=record.dry_run
    )
    other = card_token("0" * 64, record.dry_run, record.generation)
    _check(confirm(token=other).status == "stale", case, "a Confirm for other arguments")

    token = card_token(record.args_hash, record.dry_run, record.generation)
    if injected:
        _check(confirm(token=token).status == "outside", case, "an injected guest blocks it")
        apply_open(session)
        _check(not calendar.bodies, case, "nothing is booked while a guest is outside")
        for guest in injected:
            contacts.allow(conn, guest, via="web", key=KEY)

    # Only the owner's Confirm, bound to what the card shows, books it.
    _check(confirm(token=token).status == "queued", case, "the owner's Confirm is queued")
    apply_open(session)
    _check(len(calendar.bodies) == 1, case, "booked once, after the owner's Confirm")
    body = calendar.bodies[0]
    _check(body.get("summary") == payload["title"], case, "the title the card showed")
    written = sorted(guest["email"] for guest in body.get("attendees", []))
    _check(written == sorted(payload["attendees"]), case, "the guests the card showed")
