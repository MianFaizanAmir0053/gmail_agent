"""Graph routing and durability.

Everything the graph touches is faked except the checkpointer, which is the
component actually under test in the durability case.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.contracts import EmailMessage, ExtractionResult
from app.extraction.payloads import ClassifyPayload
from app.graph.build import build_graph
from app.graph.nodes import Deps
from app.store.ledger import MessageStatus

NOW = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
START = datetime(2026, 8, 19, 11, 0, tzinfo=UTC)

EMAIL = EmailMessage(
    id="m1",
    thread_id="m1",
    subject="Design review",
    body_text="Wednesday 4pm",
    sender="sara@example.com",
    recipients=["me@example.com"],
    received_at=NOW,
)


def _meeting(title: str = "Design review") -> ExtractionResult:
    return ExtractionResult(
        is_meeting=True,
        title=title,
        start_utc=START,
        end_utc=START + timedelta(hours=1),
        timezone="Asia/Karachi",
        attendees=["sara@example.com"],
        confidence=0.9,
        reasoning="",
    )


class FakeGmail:
    def get_message(self, message_id: str) -> EmailMessage:
        return EMAIL


@dataclass
class FakePipeline:
    is_meeting: bool = True
    extractions: list[ExtractionResult] = field(default_factory=list)
    corrections: list[str] = field(default_factory=list)

    def classify(self, email: EmailMessage, **kwargs: Any) -> ClassifyPayload:
        return ClassifyPayload(
            is_meeting=self.is_meeting,
            confidence=0.9,
            reasoning="explicit time" if self.is_meeting else "job alert",
        )

    def extract(self, email: EmailMessage, *, extra: str = "", **kwargs: Any) -> ExtractionResult:
        self.corrections.append(extra)
        if self.extractions:
            return self.extractions.pop(0)
        return _meeting()


@dataclass
class FakeCalendar:
    dry_run: bool = False
    busy: list[Any] = field(default_factory=list)
    created: list[Any] = field(default_factory=list)

    def freebusy(self, start: datetime, end: datetime) -> list[Any]:
        return self.busy

    def create_event(self, **kwargs: Any) -> str | None:
        if self.dry_run:
            return None
        self.created.append(kwargs)
        return "evt_123"


@dataclass
class FakeLedger:
    marks: list[tuple[str, MessageStatus, str | None]] = field(default_factory=list)

    def mark(
        self,
        message_id: str,
        status: MessageStatus,
        *,
        calendar_event_id: str | None = None,
        error: str | None = None,
    ) -> None:
        self.marks.append((message_id, status, calendar_event_id))

    @property
    def statuses(self) -> list[MessageStatus]:
        return [m[1] for m in self.marks]


def _deps(**overrides: Any) -> Deps:
    defaults: dict[str, Any] = {
        "gmail": FakeGmail(),
        "pipeline": FakePipeline(),
        "calendar": FakeCalendar(),
        "ledger": FakeLedger(),
        "user_timezone": "Asia/Karachi",
    }
    return Deps(**(defaults | overrides))


def _run(deps: Deps, saver: Any = None) -> tuple[Any, dict[str, Any], Any]:
    saver = saver or InMemorySaver()
    graph = build_graph(deps, saver)
    config = {"configurable": {"thread_id": "m1"}}
    state = graph.invoke({"message_id": "m1", "thread_id": "m1"}, config)
    return graph, config, state


def _interrupt_payload(graph: Any, config: dict[str, Any]) -> dict[str, Any] | None:
    for task in graph.get_state(config).tasks:
        for interrupt_ in task.interrupts:
            payload: dict[str, Any] = interrupt_.value
            return payload
    return None


@dataclass
class FakeReviewer:
    """Returns a fixed sequence of verdicts, then approves."""

    verdicts: list[str] = field(default_factory=list)
    seen: list[ExtractionResult] = field(default_factory=list)

    def __call__(self, email: EmailMessage, extraction: ExtractionResult, **kwargs: Any) -> Any:
        from app.agents.reviewer import ReviewVerdict

        self.seen.append(extraction)
        decision = self.verdicts.pop(0) if self.verdicts else "approve"
        return ReviewVerdict(
            decision=decision,
            issues=[] if decision == "approve" else ["the stated zone was ignored"],
            confidence=0.8,
            reasoning="checked",
        )


# --- routing ---------------------------------------------------------------


def test_meeting_parks_at_approval_without_writing_anything() -> None:
    """Nothing irreversible happens before a human decides. That is the guardrail."""
    calendar = FakeCalendar()
    deps = _deps(calendar=calendar)
    graph, config, _ = _run(deps)

    payload = _interrupt_payload(graph, config)
    assert payload is not None
    assert payload["proposed"]["title"] == "Design review"
    assert calendar.created == []


def test_non_meeting_skips_extraction_and_ends() -> None:
    ledger = FakeLedger()
    deps = _deps(pipeline=FakePipeline(is_meeting=False), ledger=ledger)
    graph, config, _ = _run(deps)

    assert _interrupt_payload(graph, config) is None
    assert ledger.statuses == [MessageStatus.SKIPPED]


def test_confirm_creates_the_event_and_records_the_id() -> None:
    calendar = FakeCalendar()
    ledger = FakeLedger()
    graph, config, _ = _run(_deps(calendar=calendar, ledger=ledger))

    from langgraph.types import Command

    graph.invoke(Command(resume={"action": "confirm"}), config)

    assert len(calendar.created) == 1
    assert ledger.marks[-1] == ("m1", MessageStatus.CREATED, "evt_123")


def test_cancel_creates_nothing() -> None:
    calendar = FakeCalendar()
    ledger = FakeLedger()
    graph, config, _ = _run(_deps(calendar=calendar, ledger=ledger))

    from langgraph.types import Command

    graph.invoke(Command(resume={"action": "cancel"}), config)

    assert calendar.created == []
    assert ledger.statuses == [MessageStatus.REJECTED]


def test_dry_run_never_records_created() -> None:
    """CREATED without an event id violates the ledger's CHECK constraint."""
    ledger = FakeLedger()
    graph, config, _ = _run(_deps(calendar=FakeCalendar(dry_run=True), ledger=ledger))

    from langgraph.types import Command

    graph.invoke(Command(resume={"action": "confirm"}), config)

    assert ledger.statuses[-1] is MessageStatus.SKIPPED
    assert ledger.marks[-1][2] is None


def test_edit_re_extracts_with_the_correction() -> None:
    pipeline = FakePipeline(extractions=[_meeting(), _meeting("Corrected review")])
    graph, config, _ = _run(_deps(pipeline=pipeline))

    from langgraph.types import Command

    graph.invoke(Command(resume={"action": "edit", "correction": "4pm not 3pm"}), config)

    # Labelled by the node rather than the pipeline: by M13 there are two
    # possible sources of guidance, and the extractor is told which is which.
    assert pipeline.corrections[0] == ""
    assert "4pm not 3pm" in pipeline.corrections[1]
    assert "from the user" in pipeline.corrections[1]

    payload = _interrupt_payload(graph, config)
    assert payload is not None
    assert payload["proposed"]["title"] == "Corrected review"


def test_cancel_after_an_edit_ends_the_proposal() -> None:
    """A correction must not outlive the decision that follows it.

    `correction` stays in state after the re-extraction it asked for. If the
    next answer does not clear it, `_decision` still sees an edit in progress
    and routes a Cancel straight back to `extract` -- the proposal parks again
    instead of ending, and the ledger never hears about it.
    """
    from langgraph.types import Command

    calendar = FakeCalendar()
    ledger = FakeLedger()
    pipeline = FakePipeline(extractions=[_meeting(), _meeting("Corrected review")])
    graph, config, _ = _run(_deps(calendar=calendar, ledger=ledger, pipeline=pipeline))

    graph.invoke(Command(resume={"action": "edit", "correction": "4pm not 3pm"}), config)
    graph.invoke(Command(resume={"action": "cancel"}), config)

    assert _interrupt_payload(graph, config) is None
    assert len(pipeline.corrections) == 2  # the edit's re-extraction, and no third
    assert ledger.statuses == [MessageStatus.REJECTED]
    assert calendar.created == []


def test_confirm_after_an_edit_creates_the_corrected_event_once() -> None:
    from langgraph.types import Command

    calendar = FakeCalendar()
    ledger = FakeLedger()
    pipeline = FakePipeline(extractions=[_meeting(), _meeting("Corrected review")])
    graph, config, _ = _run(_deps(calendar=calendar, ledger=ledger, pipeline=pipeline))

    graph.invoke(Command(resume={"action": "edit", "correction": "4pm not 3pm"}), config)
    graph.invoke(Command(resume={"action": "confirm"}), config)

    assert _interrupt_payload(graph, config) is None
    assert len(pipeline.corrections) == 2
    assert [event["title"] for event in calendar.created] == ["Corrected review"]
    assert ledger.marks[-1] == ("m1", MessageStatus.CREATED, "evt_123")


def test_revision_loop_is_bounded_by_state_not_by_the_prompt() -> None:
    """A prompt instruction is a suggestion; a counter in state is a guarantee."""
    from langgraph.types import Command

    graph, config, _ = _run(_deps())

    for _ in range(4):
        if _interrupt_payload(graph, config) is None:
            break
        graph.invoke(Command(resume={"action": "edit", "correction": "again"}), config)

    assert _interrupt_payload(graph, config) is None


# --- the reviewer (M13) ----------------------------------------------------


def test_without_a_reviewer_the_graph_is_unchanged() -> None:
    graph, config, state = _run(_deps())

    assert state.get("review_decision") == "approve"
    assert _interrupt_payload(graph, config) is not None


def test_an_approving_reviewer_lets_the_proposal_through() -> None:
    reviewer = FakeReviewer()
    graph, config, _ = _run(_deps(reviewer=reviewer))

    assert len(reviewer.seen) == 1
    assert _interrupt_payload(graph, config) is not None


def test_a_revision_sends_the_extraction_round_again() -> None:
    pipeline = FakePipeline(extractions=[_meeting(), _meeting("Design review (PT)")])
    reviewer = FakeReviewer(verdicts=["revise"])

    graph, config, _ = _run(_deps(pipeline=pipeline, reviewer=reviewer))

    assert len(pipeline.corrections) == 2
    assert "reviewer" in pipeline.corrections[1].lower()
    payload = _interrupt_payload(graph, config)
    assert payload is not None
    assert payload["proposed"]["title"] == "Design review (PT)"


def test_the_reviewer_loop_terminates_however_stubborn_it_is() -> None:
    """The cap is compared in the router, so no verdict sequence can outlast it."""
    reviewer = FakeReviewer(verdicts=["revise"] * 10)
    graph, config, _ = _run(_deps(reviewer=reviewer))

    # It parked at approval rather than looping, which is the whole claim.
    assert _interrupt_payload(graph, config) is not None
    # Three verdicts, two re-extractions: the budget is on revisions, not on
    # opinions. The eval wrapper in app/eval/reviewed.py spends exactly the same.
    assert len(reviewer.seen) == 3


def test_a_rejecting_reviewer_stops_before_a_human_is_asked() -> None:
    ledger = FakeLedger()
    reviewer = FakeReviewer(verdicts=["reject"])

    graph, config, state = _run(_deps(ledger=ledger, reviewer=reviewer))

    assert _interrupt_payload(graph, config) is None
    assert ledger.statuses[-1] is MessageStatus.REJECTED
    assert state["action"].status == "rejected"


def test_a_reviewer_rejection_is_not_recorded_as_a_human_decision() -> None:
    """The failures view must not report an agent's call as a person's."""
    ledger = FakeLedger()
    _, _, state = _run(_deps(ledger=ledger, reviewer=FakeReviewer(verdicts=["reject"])))

    assert "declined by user" not in (state["action"].error or "")
    assert "zone" in (state["action"].error or "")


def test_a_non_meeting_never_reaches_the_reviewer() -> None:
    reviewer = FakeReviewer()
    _run(_deps(pipeline=FakePipeline(is_meeting=False), reviewer=reviewer))

    assert reviewer.seen == []


def test_conflicts_reach_the_approval_card() -> None:
    from app.google.calendar import BusyInterval

    calendar = FakeCalendar(
        busy=[BusyInterval(start=START + timedelta(minutes=30), end=START + timedelta(hours=2))]
    )
    graph, config, _ = _run(_deps(calendar=calendar))

    payload = _interrupt_payload(graph, config)
    assert payload is not None
    assert payload["conflicts"] and "Overlaps" in payload["conflicts"][0]


def test_malformed_extraction_routes_to_skip_not_to_the_calendar() -> None:
    broken = ExtractionResult(is_meeting=False, confidence=0.0, reasoning="Discarded malformed")
    ledger = FakeLedger()
    deps = _deps(pipeline=FakePipeline(extractions=[broken]), ledger=ledger)
    graph, config, _ = _run(deps)

    assert _interrupt_payload(graph, config) is None
    assert ledger.statuses == [MessageStatus.SKIPPED]


# --- durability ------------------------------------------------------------


@pytest.mark.integration
def test_approval_survives_losing_the_process(
    migrated_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The M05 exit criterion.

    Two graph instances with two independent checkpointer connections, sharing
    nothing but Postgres -- which is what a redeploy between proposal and
    approval actually looks like.

    `LANGGRAPH_STRICT_MSGPACK` is forced on so the run fails if any state type
    is missing from `CHECKPOINT_TYPES`. Without it the serializer merely warns,
    and the allowlist would silently rot until a dependency bump turned the
    warning into a hard failure across every parked approval at once.
    """
    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "true")

    from langgraph.types import Command

    from app.graph.checkpointer import postgres_checkpointer

    calendar = FakeCalendar()
    ledger = FakeLedger()
    deps = _deps(calendar=calendar, ledger=ledger)
    config = {"configurable": {"thread_id": "durable-m1"}}

    with postgres_checkpointer(migrated_database) as saver:
        build_graph(deps, saver).invoke(
            {"message_id": "durable-m1", "thread_id": "durable-m1"}, config
        )

    assert calendar.created == []  # parked, nothing written

    # Everything above is now out of scope: connection closed, graph discarded.
    with postgres_checkpointer(migrated_database) as saver:
        resumed = build_graph(deps, saver)
        assert _interrupt_payload(resumed, config) is not None
        resumed.invoke(Command(resume={"action": "confirm"}), config)

    assert len(calendar.created) == 1
    assert ledger.marks[-1] == ("durable-m1", MessageStatus.CREATED, "evt_123")


@pytest.mark.integration
def test_every_state_type_is_registered_for_checkpointing(migrated_database: str) -> None:
    """A model added to GraphState but not to CHECKPOINT_TYPES breaks resume."""
    from app.graph.checkpointer import CHECKPOINT_TYPES

    registered = {name for _, name in CHECKPOINT_TYPES}
    assert {"EmailMessage", "ExtractionResult", "ActionResult"} <= registered
