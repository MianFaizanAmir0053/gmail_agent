"""Graph routing and durability.

Everything the graph touches is faked except the checkpointer, which is the
component actually under test in the durability case.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.contracts import EmailMessage, ExtractionResult
from app.extraction.payloads import ClassifyPayload
from app.graph.build import build_graph
from app.graph.nodes import CARRIED_A_CODE, NOT_A_MEETING, Deps
from app.policy.registry import Approval, Outcome
from app.policy.scrub import CREDENTIAL_NOTICE
from app.store.ledger import MessageStatus

NOW = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
START = datetime(2026, 8, 19, 11, 0, tzinfo=UTC)

EMAIL = EmailMessage(
    id="m1",
    thread_id="t1",
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
    """The email, and its thread as the recipient rule reads it: by default
    the owner wrote to Sara, so she is a participant (M17, D4)."""

    def __init__(self, thread: dict[str, Any] | None = None, *, down: bool = False) -> None:
        self.thread = (
            thread
            if thread is not None
            else {
                "messages": [
                    {
                        "labelIds": ["SENT"],
                        "payload": {"headers": [{"name": "To", "value": "sara@example.com"}]},
                    }
                ]
            }
        )
        self.down = down

    def get_message(self, message_id: str) -> EmailMessage:
        return EMAIL

    def thread_headers(self, thread_id: str) -> dict[str, Any]:
        if self.down:
            raise ConnectionError("Gmail unavailable")
        # Only the email's own thread is known: the graph must ask for it.
        return self.thread if thread_id == EMAIL.thread_id else {"messages": []}


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
    calendar_id: str = "test-calendar"
    busy: list[Any] = field(default_factory=list)

    def freebusy(self, start: datetime, end: datetime) -> list[Any]:
        return self.busy


@dataclass
class FakeRegistry:
    """Stands in for the registry (M17): records what `act` asked it to run,
    and answers as the registry would."""

    dry_run: bool = False
    refuse: str | None = None
    executed: list[tuple[str, Any, Approval | None]] = field(default_factory=list)

    def execute(
        self, tool: str, args: Any, *, approval: Approval | None, message_id: str
    ) -> Outcome:
        self.executed.append((tool, args, approval))
        if self.refuse is not None:
            return Outcome("refused", reason=self.refuse)
        if self.dry_run:
            return Outcome("dry_run")
        return Outcome("created", event_id="evt_123")

    @property
    def titles(self) -> list[str]:
        return [args.title for _, args, _ in self.executed]


@dataclass
class FakeLedger:
    marks: list[tuple[str, MessageStatus, str | None]] = field(default_factory=list)
    errors: list[str | None] = field(default_factory=list)

    def mark(
        self,
        message_id: str,
        status: MessageStatus,
        *,
        calendar_event_id: str | None = None,
        error: str | None = None,
    ) -> None:
        self.marks.append((message_id, status, calendar_event_id))
        self.errors.append(error)

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
        "registry": FakeRegistry(),
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


def test_a_meeting_with_a_start_and_no_end_is_not_parked() -> None:
    """No Confirm could ever bind it: the hash needs both times (M17, D2). It
    is skipped, rather than left "not ready" for good."""
    from app.graph.build import _has_event

    no_end = _meeting().model_copy(update={"end_utc": None})

    assert _has_event(cast(Any, {"extraction": no_end})) == "skip"
    assert _has_event(cast(Any, {"extraction": _meeting()})) == "conflicts"


def test_meeting_parks_at_approval_without_writing_anything() -> None:
    """Nothing irreversible happens before a human decides. That is the guardrail."""
    registry = FakeRegistry()
    deps = _deps(registry=registry)
    graph, config, _ = _run(deps)

    payload = _interrupt_payload(graph, config)
    assert payload is not None
    assert payload["proposed"]["title"] == "Design review"
    assert registry.executed == []


def test_non_meeting_skips_extraction_and_ends() -> None:
    ledger = FakeLedger()
    deps = _deps(pipeline=FakePipeline(is_meeting=False), ledger=ledger)
    graph, config, _ = _run(deps)

    assert _interrupt_payload(graph, config) is None
    assert ledger.statuses == [MessageStatus.SKIPPED]


def test_a_classification_skip_records_a_fixed_phrase_not_the_models_reasoning() -> None:
    """The reasoning can quote the email, and since M20 read mail -- one-time
    codes included -- reaches the classifier (D4)."""
    ledger = FakeLedger()
    _run(_deps(pipeline=FakePipeline(is_meeting=False), ledger=ledger))

    assert ledger.errors == ["not a meeting"]


# --- credential mail (M18, decision 2) ------------------------------------------------


class CredentialGmail(FakeGmail):
    """Serves the message as the client leaves credential mail: flagged, its
    body the fixed notice."""

    def get_message(self, message_id: str) -> EmailMessage:
        return EMAIL.model_copy(
            update={
                "subject": "Your Acme verification code",
                "body_text": CREDENTIAL_NOTICE,
                "credential": True,
            }
        )


class NoModel:
    """A pipeline whose every call fails the test: no model may read the mail."""

    def classify(self, email: EmailMessage, **kwargs: Any) -> ClassifyPayload:
        raise AssertionError("credential mail reached the classifier")

    def extract(self, email: EmailMessage, **kwargs: Any) -> ExtractionResult:
        raise AssertionError("credential mail reached the extractor")


def test_credential_mail_is_skipped_with_the_fixed_reason_before_any_model() -> None:
    ledger = FakeLedger()
    graph, config, _ = _run(_deps(gmail=CredentialGmail(), pipeline=NoModel(), ledger=ledger))

    assert ledger.statuses == [MessageStatus.SKIPPED]
    assert ledger.errors == [CARRIED_A_CODE]
    assert _interrupt_payload(graph, config) is None


def test_credential_mails_checkpoint_holds_only_the_notice() -> None:
    graph, config, _ = _run(_deps(gmail=CredentialGmail(), pipeline=NoModel()))

    values = graph.get_state(config).values
    assert values["email"].body_text == CREDENTIAL_NOTICE
    assert "extraction" not in values


def test_mail_without_the_flag_still_reaches_the_classifier() -> None:
    pipeline = FakePipeline(is_meeting=False)
    ledger = FakeLedger()
    _run(_deps(pipeline=pipeline, ledger=ledger))

    assert ledger.errors == [NOT_A_MEETING]


@dataclass
class FailingGmail:
    """Fails every fetch with `error`, counting the attempts."""

    error: Exception
    calls: int = 0

    def get_message(self, message_id: str) -> EmailMessage:
        self.calls += 1
        raise self.error


def test_a_message_gone_before_its_turn_is_not_retried() -> None:
    from app.google.gmail import MessageGoneError

    gmail = FailingGmail(MessageGoneError("m1"))

    with pytest.raises(MessageGoneError):
        _run(_deps(gmail=gmail))

    assert gmail.calls == 1


def test_the_fetch_adds_no_retries_to_the_gmail_clients_own() -> None:
    """The client retries a 5xx for at most 30 seconds in all (M20, D3); a
    node retry on top would multiply that towards the kill timeout."""
    from types import SimpleNamespace

    from googleapiclient.errors import HttpError

    gmail = FailingGmail(HttpError(SimpleNamespace(status=503, reason="x"), b"{}"))

    with pytest.raises(HttpError):
        _run(_deps(gmail=gmail))

    assert gmail.calls == 1
    # Asked of the graph itself: a policy whose rule skipped this error would
    # pass the count above, and still retry what it did catch.
    graph = build_graph(_deps(), InMemorySaver())
    assert graph.nodes["fetch"].retry_policy is None
    assert graph.nodes["classify"].retry_policy is not None  # the check can see one


def test_confirm_creates_the_event_and_records_the_id() -> None:
    registry = FakeRegistry()
    ledger = FakeLedger()
    graph, config, _ = _run(_deps(registry=registry, ledger=ledger))

    from langgraph.types import Command

    graph.invoke(Command(resume={"action": "confirm"}), config)

    assert len(registry.executed) == 1
    assert ledger.marks[-1] == ("m1", MessageStatus.CREATED, "evt_123")


def test_a_confirm_carries_its_approval_to_the_registry() -> None:
    """The action `decide()` recorded, and its nonce (M17, D2). An invite,
    because the extraction has a guest."""
    registry = FakeRegistry()
    graph, config, _ = _run(_deps(registry=registry))

    from langgraph.types import Command

    graph.invoke(
        Command(resume={"action": "confirm", "approval": {"action_id": 7, "nonce": "ab" * 16}}),
        config,
    )

    [(tool, args, approval)] = registry.executed
    assert tool == "calendar.create_invite"
    assert args.description == "Created by mailagent from message m1."
    assert approval == Approval(action_id=7, nonce="ab" * 16)


def test_a_refusal_fails_the_message_with_the_registrys_reason() -> None:
    """Final: a refused approval is never retried."""
    ledger = FakeLedger()
    graph, config, _ = _run(
        _deps(registry=FakeRegistry(refuse="arguments differ from the approval"), ledger=ledger)
    )

    from langgraph.types import Command

    state = graph.invoke(Command(resume={"action": "confirm"}), config)

    assert ledger.statuses == [MessageStatus.FAILED]
    assert state["action"].error == "arguments differ from the approval"
    assert _interrupt_payload(graph, config) is None


@dataclass
class CountingGmail(FakeGmail):
    reads: int = 0

    def get_message(self, message_id: str) -> EmailMessage:
        self.reads += 1
        return super().get_message(message_id)


def test_await_approval_calls_neither_gmail_nor_the_database() -> None:
    """`interrupt` runs the node again on resume (M17, D2). Anything it
    called would run, and could fail, at every resume."""
    from langgraph.types import Command

    gmail = CountingGmail()
    ledger = FakeLedger()
    graph, config, _ = _run(_deps(gmail=gmail, ledger=ledger))
    assert (gmail.reads, ledger.marks) == (1, [])  # `fetch` read the email

    graph.invoke(Command(resume={"action": "confirm"}), config)

    assert gmail.reads == 1
    assert ledger.statuses == [MessageStatus.CREATED]  # `act`'s mark, and no other


def test_cancel_creates_nothing() -> None:
    registry = FakeRegistry()
    ledger = FakeLedger()
    graph, config, _ = _run(_deps(registry=registry, ledger=ledger))

    from langgraph.types import Command

    graph.invoke(Command(resume={"action": "cancel"}), config)

    assert registry.executed == []
    assert ledger.statuses == [MessageStatus.REJECTED]


def test_dry_run_never_records_created() -> None:
    """CREATED without an event id violates the ledger's CHECK constraint."""
    ledger = FakeLedger()
    graph, config, _ = _run(_deps(registry=FakeRegistry(dry_run=True), ledger=ledger))

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

    registry = FakeRegistry()
    ledger = FakeLedger()
    pipeline = FakePipeline(extractions=[_meeting(), _meeting("Corrected review")])
    graph, config, _ = _run(_deps(registry=registry, ledger=ledger, pipeline=pipeline))

    graph.invoke(Command(resume={"action": "edit", "correction": "4pm not 3pm"}), config)
    graph.invoke(Command(resume={"action": "cancel"}), config)

    assert _interrupt_payload(graph, config) is None
    assert len(pipeline.corrections) == 2  # the edit's re-extraction, and no third
    assert ledger.statuses == [MessageStatus.REJECTED]
    assert registry.executed == []


def test_confirm_after_an_edit_creates_the_corrected_event_once() -> None:
    from langgraph.types import Command

    registry = FakeRegistry()
    ledger = FakeLedger()
    pipeline = FakePipeline(extractions=[_meeting(), _meeting("Corrected review")])
    graph, config, _ = _run(_deps(registry=registry, ledger=ledger, pipeline=pipeline))

    graph.invoke(Command(resume={"action": "edit", "correction": "4pm not 3pm"}), config)
    graph.invoke(Command(resume={"action": "confirm"}), config)

    assert _interrupt_payload(graph, config) is None
    assert len(pipeline.corrections) == 2
    assert registry.titles == ["Corrected review"]
    assert ledger.marks[-1] == ("m1", MessageStatus.CREATED, "evt_123")


def test_a_parked_proposal_records_the_dry_run_it_was_made_under() -> None:
    """M17 refuses to act on a proposal parked under a different DRY_RUN."""
    graph, config, _ = _run(_deps(calendar=FakeCalendar(dry_run=True)))

    payload = _interrupt_payload(graph, config)
    assert payload is not None
    assert payload["dry_run"] is True


def test_a_sweep_is_not_recorded_as_a_human_decline() -> None:
    """M24 counts a human's cancellations against the agent; a sweep is not one."""
    from langgraph.types import Command

    registry = FakeRegistry()
    ledger = FakeLedger()
    graph, config, _ = _run(_deps(registry=registry, ledger=ledger))

    state = graph.invoke(Command(resume={"action": "sweep"}), config)

    assert registry.executed == []
    assert ledger.statuses == [MessageStatus.REJECTED]
    assert state["action"].error == "swept: observe mode ended"


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


# --- what M16 records at every park ----------------------------------------


def _hold() -> ExtractionResult:
    return _meeting().model_copy(update={"attendees": []})


def test_a_parked_proposal_carries_what_m24_counts_by() -> None:
    """The payload is the single source for what a proposal is (M16, D2)."""
    reviewer = FakeReviewer(verdicts=["revise"] * 3)
    graph, config, _ = _run(_deps(reviewer=reviewer, pipeline_version="v-test"))

    payload = _interrupt_payload(graph, config)
    assert payload is not None
    assert payload["action_type"] == "calendar_invite"
    assert payload["pipeline_version"] == "v-test"
    # The reviewer's last objections travel with the proposal onto the card.
    assert payload["review_issues"] == ["the stated zone was ignored"]


def test_a_proposal_without_reviewer_objections_carries_none() -> None:
    graph, config, _ = _run(_deps())

    payload = _interrupt_payload(graph, config)
    assert payload is not None
    assert payload["review_issues"] == []


def test_an_edit_that_adds_a_guest_turns_a_hold_into_an_invite() -> None:
    """An invite emails other people, so it is a different action from a hold."""
    from langgraph.types import Command

    pipeline = FakePipeline(extractions=[_hold(), _meeting()])
    graph, config, _ = _run(_deps(pipeline=pipeline))

    first = _interrupt_payload(graph, config)
    assert first is not None
    assert first["action_type"] == "calendar_hold"

    graph.invoke(Command(resume={"action": "edit", "correction": "invite Sara"}), config)

    second = _interrupt_payload(graph, config)
    assert second is not None
    assert second["action_type"] == "calendar_invite"


# --- the session's view of a thread (M16) ----------------------------------


def _session(deps: Deps) -> Any:
    from app.graph.runner import GraphSession

    return GraphSession(deps=deps, conn=cast(Any, None), checkpointer=InMemorySaver(), trace=False)


class _RecordingGraph:
    """A built graph whose `invoke` keyword arguments are written down."""

    def __init__(self, graph: Any, seen: list[Any]) -> None:
        self._graph = graph
        self._seen = seen

    def invoke(self, payload: Any, config: Any, **kwargs: Any) -> Any:
        self._seen.append(kwargs.get("durability"))
        return self._graph.invoke(payload, config, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._graph, name)


def test_every_invocation_writes_its_checkpoints_before_moving_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LangGraph's default writes checkpoints in the background. A crash inside
    `act` could then lose the step that recorded the approval, and the thread
    would look parked and be resumed again (M17 D3)."""
    from app.graph.runner import GraphSession

    seen: list[Any] = []
    build = GraphSession._graph
    monkeypatch.setattr(
        GraphSession,
        "_graph",
        lambda self, tracer=None: _RecordingGraph(build(self, tracer), seen),
    )
    session = _session(_deps())

    session.start("m1", "m1")
    session.resume("m1", {"action": "edit", "correction": "make it 5pm"})
    session.redrive("m1")

    assert seen == ["sync", "sync", "sync"]


def test_the_revision_comes_from_the_threads_own_counter() -> None:
    session = _session(_deps())
    session.start("m1", "m1")
    assert session.revision("m1") == 1

    session.resume("m1", {"action": "edit", "correction": "make it 5pm"})

    assert session.pending("m1") is not None
    assert session.revision("m1") == 2


@dataclass
class FlakyPipeline(FakePipeline):
    """Fails the extraction calls whose (1-based) numbers are listed."""

    failing_calls: set[int] = field(default_factory=set)
    calls: int = 0

    def extract(self, email: EmailMessage, *, extra: str = "", **kwargs: Any) -> ExtractionResult:
        self.calls += 1
        if self.calls in self.failing_calls:
            # Not retried by the node's policy, so the test does not sleep
            # through its back-off.
            raise RuntimeError("model unavailable")
        return super().extract(email, extra=extra, **kwargs)


def test_redrive_finishes_an_edit_whose_extraction_failed() -> None:
    """The decision was consumed; the thread stopped mid-graph, not parked."""
    pipeline = FlakyPipeline(failing_calls={2})
    session = _session(_deps(pipeline=pipeline))
    session.start("m1", "m1")

    with pytest.raises(RuntimeError):
        session.resume("m1", {"action": "edit", "correction": "make it 5pm"})
    assert session.pending("m1") is None

    session.redrive("m1")

    assert session.pending("m1") is not None
    assert session.revision("m1") == 2
    # The re-driven extraction still saw the owner's correction.
    assert "make it 5pm" in pipeline.corrections[-1]


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

    registry = FakeRegistry()
    ledger = FakeLedger()
    deps = _deps(registry=registry, ledger=ledger)
    config = {"configurable": {"thread_id": "durable-m1"}}

    with postgres_checkpointer(migrated_database) as saver:
        build_graph(deps, saver).invoke(
            {"message_id": "durable-m1", "thread_id": "durable-m1"}, config
        )

    assert registry.executed == []  # parked, nothing written

    # Everything above is now out of scope: connection closed, graph discarded.
    with postgres_checkpointer(migrated_database) as saver:
        resumed = build_graph(deps, saver)
        assert _interrupt_payload(resumed, config) is not None
        resumed.invoke(
            Command(resume={"action": "confirm", "approval": {"action_id": 7, "nonce": "c" * 32}}),
            config,
        )

    # The approval went through the checkpoint, under the strict serializer.
    assert [approval for _, _, approval in registry.executed] == [Approval(7, "c" * 32)]
    assert ledger.marks[-1] == ("durable-m1", MessageStatus.CREATED, "evt_123")


@pytest.mark.integration
def test_every_state_type_is_registered_for_checkpointing(migrated_database: str) -> None:
    """A model added to GraphState but not to CHECKPOINT_TYPES breaks resume."""
    from app.graph.checkpointer import CHECKPOINT_TYPES

    registered = {name for _, name in CHECKPOINT_TYPES}
    assert {"EmailMessage", "ExtractionResult", "ActionResult"} <= registered


def test_an_expiry_ends_the_thread_with_its_own_reason() -> None:
    """Not the M15 sweep's: M24 must not count a mode change as observe mode
    ending (M17, D2)."""
    from langgraph.types import Command

    ledger = FakeLedger()
    graph, config, _ = _run(_deps(ledger=ledger))

    graph.invoke(Command(resume={"action": "sweep", "reason": "made under another mode"}), config)

    assert ledger.statuses == [MessageStatus.REJECTED]
    assert ledger.errors == ["made under another mode"]


def test_a_sweep_that_gives_no_reason_keeps_the_m15_one() -> None:
    from langgraph.types import Command

    from app.graph.nodes import SWEEP_REASON

    ledger = FakeLedger()
    graph, config, _ = _run(_deps(ledger=ledger))

    graph.invoke(Command(resume={"action": "sweep"}), config)

    assert ledger.errors == [SWEEP_REASON]


# --- guests outside the thread (M17, D4) ------------------------------------------------


def test_a_guest_the_thread_does_not_know_is_marked_outside() -> None:
    graph, config, _ = _run(_deps(gmail=FakeGmail({"messages": []})))

    payload = _interrupt_payload(graph, config)
    assert payload is not None
    assert payload["outside_guests"] == ["sara@example.com"]


def test_a_guest_the_owner_wrote_to_is_not_outside() -> None:
    graph, config, _ = _run(_deps())

    payload = _interrupt_payload(graph, config)
    assert payload is not None
    assert payload["outside_guests"] == []


def test_a_thread_gmail_cannot_read_marks_every_guest_and_still_parks() -> None:
    """A Gmail outage never fails a park, or an edit's re-park."""
    graph, config, _ = _run(_deps(gmail=FakeGmail(down=True)))

    payload = _interrupt_payload(graph, config)
    assert payload is not None
    assert payload["outside_guests"] == ["sara@example.com"]


def test_an_outsider_added_by_an_edit_is_marked() -> None:
    """The re-park reads the thread again (M17, D4): a guest the edit added,
    whom the thread does not know, is marked for the owner."""
    from langgraph.types import Command

    added = _meeting().model_copy(update={"attendees": ["sara@example.com", "new@example.net"]})
    pipeline = FakePipeline(extractions=[_meeting(), added])
    graph, config, _ = _run(_deps(pipeline=pipeline))
    first = _interrupt_payload(graph, config)
    assert first is not None and first["outside_guests"] == []

    graph.invoke(Command(resume={"action": "edit", "correction": "add new@example.net"}), config)

    second = _interrupt_payload(graph, config)
    assert second is not None
    assert second["outside_guests"] == ["new@example.net"]


def test_a_run_marks_the_message_its_model_calls_serve() -> None:
    """Each call counts against that message's ceiling (M17, D5); outside a
    run, a call serves no message."""
    from app.policy import models

    seen: list[str | None] = []

    @dataclass
    class Watching(FakePipeline):
        def extract(self, email: EmailMessage, **kwargs: Any) -> ExtractionResult:
            seen.append(models.CURRENT_MESSAGE.get())
            return super().extract(email, **kwargs)

    session = _session(_deps(pipeline=Watching()))
    session.start("m1", "m1")

    assert seen == ["m1"]
    assert models.CURRENT_MESSAGE.get() is None
