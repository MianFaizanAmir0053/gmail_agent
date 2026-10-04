"""The routing eval's harness, with fake planners (M19, D10)."""

from __future__ import annotations

import copy
import inspect
import json
import re
import shutil
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.eval import planner as harness
from app.eval.planner import (
    PLAN_REFUSED,
    REASONS,
    SAMPLES,
    WRONG_KIND,
    Case,
    CaseResult,
    Rate,
    Rates,
    code_hash,
    is_baseline,
    load_cases,
    load_world,
    rates,
    run,
    sample,
    to_dict,
)
from app.planner.plan import PLAN_THINKING, PlanPayload, make_plan
from app.planner.prompts import PLAN_SYSTEM, plan_user

WORLD = load_world()
CASES = load_cases()
REF = re.compile(r"\$s(\d+)")


def _quiet(line: str) -> None:
    pass


def _ideal(case: Case) -> PlanPayload:
    return PlanPayload.model_validate(case.ideal)


def _scores(
    planner: Callable[[Case], PlanPayload], cases: list[Case] = CASES
) -> dict[str, CaseResult]:
    return run(cases, WORLD, planner, samples=1, progress=_quiet)


# --- the golden set ---------------------------------------------------------------


def test_the_golden_set_covers_every_category() -> None:
    assert len(CASES) >= 60
    assert {case.category for case in CASES} == harness.CATEGORIES
    assert len({case.id for case in CASES}) == len(CASES)


def test_each_case_is_named_for_its_file() -> None:
    stems = sorted(path.stem for path in harness.CASES_DIR.glob("*.json"))
    assert stems == sorted(case.id for case in CASES)


def test_a_case_that_may_answer_must_say_which_operations() -> None:
    raw = {"id": "x", "category": "single", "typed": "t", "expect": {"kind": "answer"}, "ideal": {}}
    with pytest.raises(ValueError, match="lists its operations"):
        harness._case(raw)
    with pytest.raises(ValueError, match="unknown"):
        harness._case(raw | {"category": "other", "expect": {"kind": "answer", "ops": []}})


# --- scoring --------------------------------------------------------------------


def test_the_ideal_plans_score_full_marks() -> None:
    results = _scores(_ideal)
    for case_id, result in results.items():
        assert (result.ops, result.outcome, result.kind) == (1, 1, 1), case_id
    assert rates(results).meets_bar


def _renumbered(case: Case) -> PlanPayload | None:
    """The ideal plan with its steps in another order the references allow,
    and every reference renumbered to match; None if no other order exists."""
    steps = case.ideal["steps"]
    needs = [{int(n) - 1 for n in REF.findall(json.dumps(step))} for step in steps]
    order: list[int] = []
    while len(order) < len(steps):
        ready = [i for i in range(len(steps)) if i not in order and needs[i] <= set(order)]
        order.append(max(ready))  # the latest step that can run, so the order moves
    if order == sorted(order):
        return None
    new = {old + 1: position + 1 for position, old in enumerate(order)}
    text = json.dumps([steps[i] for i in order])
    text = REF.sub(lambda match: f"$s{new[int(match[1])]}", text)
    return PlanPayload.model_validate({**case.ideal, "steps": json.loads(text)})


def test_renumbered_references_still_score() -> None:
    moved = {case.id: plan for case in CASES if (plan := _renumbered(case)) is not None}
    assert len(moved) >= 3

    results = _scores(lambda case: moved[case.id], [c for c in CASES if c.id in moved])
    for case_id, result in results.items():
        assert (result.ops, result.outcome, result.kind) == (1, 1, 1), case_id


def _later(case: Case) -> PlanPayload | None:
    """The ideal plan with a wrong slot: a literal one an hour later, or a
    free slot looked for at a different length."""
    ideal = copy.deepcopy(dict(case.ideal))
    changed = False
    for step in ideal["steps"]:
        if step["op"].startswith("calendar.propose") and "start" in step:
            for edge in ("start", "end"):
                moved = datetime.fromisoformat(step[edge]) + timedelta(hours=1)
                step[edge] = moved.isoformat()
            changed = True
        if step["op"] == "slots.first_free":
            step["minutes"] += 15
            changed = True
    return PlanPayload.model_validate(ideal) if changed else None


def test_a_wrong_slot_fails_the_outcome_but_not_the_operations() -> None:
    wrong = {
        case.id: plan for case in CASES if not case.must_ask_or_decline and (plan := _later(case))
    }
    assert len(wrong) >= 10

    results = _scores(lambda case: wrong[case.id], [c for c in CASES if c.id in wrong])
    for case_id, result in results.items():
        assert (result.ops, result.outcome) == (1, 0), case_id


def test_asking_when_it_should_act_is_the_wrong_kind() -> None:
    def always_asks(case: Case) -> PlanPayload:
        return PlanPayload.model_validate(
            {"kind": "clarify", "timezone": "Asia/Karachi", "steps": [], "reply": {"question": "?"}}
        )

    results = _scores(always_asks)
    for case in CASES:
        result = results[case.id]
        assert result.kind == ("ask" in case.kinds), case.id
        if "ask" not in case.kinds:
            assert result.reasons == {WRONG_KIND}, case.id
    assert not rates(results).meets_bar


def test_a_plan_the_check_refuses_fails_the_sample() -> None:
    case = next(case for case in CASES if case.id == "landlord-asks")
    forward = copy.deepcopy(dict(case.ideal))
    forward["steps"] = list(reversed(forward["steps"]))  # the read now points forward

    score = sample(case, WORLD, lambda _: PlanPayload.model_validate(forward))

    assert (score.ops, score.outcome, score.kind, score.reason) == (
        True,
        False,
        False,
        PLAN_REFUSED,
    )


def test_a_sample_that_raises_is_counted_as_an_error_not_a_score() -> None:
    def broken(case: Case) -> PlanPayload:
        raise TimeoutError

    results = _scores(broken, CASES[:2])
    assert all(r.samples == 0 and r.errors == ["TimeoutError"] for r in results.values())


def test_every_reason_is_one_of_the_fixed_ones() -> None:
    results = _scores(lambda case: _later(case) or _ideal(case))
    assert set().union(*(result.reasons for result in results.values())) <= REASONS


# --- the bar and the record -----------------------------------------------------------


def test_the_interval_matches_decision_seven() -> None:
    """ "48 right out of 50 shows a true rate anywhere from 86.5% to 98.9%." """
    low, high = Rate(48, 50).interval()
    assert (round(low, 3), round(high, 3)) == (0.865, 0.989)


@pytest.mark.parametrize(
    ("ops", "outcome", "asks", "meets"),
    [
        ((95, 100), (90, 100), (20, 20), True),
        ((94, 100), (90, 100), (20, 20), False),
        ((95, 100), (89, 100), (20, 20), False),
        ((95, 100), (90, 100), (19, 20), False),
        ((0, 0), (0, 0), (0, 0), False),
    ],
)
def test_the_bar(
    ops: tuple[int, int], outcome: tuple[int, int], asks: tuple[int, int], meets: bool
) -> None:
    assert Rates(Rate(*ops), Rate(*outcome), Rate(*asks)).meets_bar is meets


def _record(
    planner: Callable[[Case], PlanPayload] = _ideal, samples: int = SAMPLES
) -> dict[str, Any]:
    results = run(CASES, WORLD, planner, samples=samples, progress=_quiet)
    return to_dict(results, model="a-model", samples=samples, code="abc")


def test_a_clean_run_of_every_case_is_a_baseline() -> None:
    data = _record()
    ids = [case.id for case in CASES]
    assert is_baseline(data, ids)
    assert not is_baseline(data, [*ids, "a-case-not-run"])
    assert not is_baseline(_record(samples=SAMPLES - 1), ids)
    assert not is_baseline({**data, "errors": 1}, ids)


def test_a_run_below_the_bar_is_no_baseline() -> None:
    data = _record(lambda case: _later(case) or _ideal(case))
    assert data["meets_bar"] is False
    assert not is_baseline(data, [case.id for case in CASES])


def test_the_record_never_quotes_a_request() -> None:
    text = json.dumps(_record(samples=1))
    for case in CASES:
        assert case.typed not in text
        assert not case.pasted or case.pasted not in text


# --- the hash ---------------------------------------------------------------------------


def _copy(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for name in harness.SHAPING:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(harness.REPO / name, root / name)
    shutil.copytree(harness.REPO / "data" / "planner", root / "data" / "planner")
    return root


def test_the_hash_covers_the_prompt_the_world_and_every_case(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    before = code_hash(root)
    assert before == code_hash(harness.REPO)

    for path in (
        root / "app/planner/prompts.py",
        root / "data/planner/world.json",
        root / "data/planner/routing/joke.json",
    ):
        original = path.read_bytes()
        path.write_bytes(original + b" ")
        assert code_hash(root) != before, path.name
        path.write_bytes(original.replace(b"\n", b"\r\n"))
        assert code_hash(root) == before, path.name
        path.write_bytes(original)


def test_the_hash_covers_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    before = code_hash()
    monkeypatch.setattr(harness, "production_model", lambda: "another-model")
    assert code_hash() != before


# --- the command ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--baseline", "--case", "joke"],
        ["--baseline", "--samples", "1"],
        ["--baseline", "--no-save"],
    ],
)
def test_a_baseline_is_refused_before_anything_is_spent(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def spend(world: Any) -> Any:
        raise AssertionError("the planner was built")

    monkeypatch.setattr(harness, "production_planner", spend)
    monkeypatch.setattr("sys.argv", ["planner", *argv])
    with pytest.raises(SystemExit, match="five samples"):
        harness.main()


# --- the planner's call -----------------------------------------------------------------


def test_the_planner_is_never_handed_the_pasted_text() -> None:
    """It is told only that there is some (D5): neither function can take it."""
    assert inspect.signature(make_plan).parameters["pasted"].annotation in (bool, "bool")
    assert inspect.signature(plan_user).parameters["pasted"].annotation in (bool, "bool")


def test_the_user_turn_holds_the_moment_the_zone_and_the_typed_request() -> None:
    asked = datetime(2026, 10, 7, 5, tzinfo=UTC)
    plain = plan_user("Am I free tomorrow?", pasted=False, asked_at=asked, zone="Asia/Karachi")
    with_paste = plan_user("Summarize this", pasted=True, asked_at=asked, zone="Asia/Karachi")

    assert "2026-10-07 10:00, a Wednesday, in Asia/Karachi" in plain
    assert plain.endswith("Am I free tomorrow?")
    assert "$paste" not in plain and "$paste" in with_paste


class _Models:
    def __init__(self) -> None:
        self.sent: list[Mapping[str, Any]] = []

    def generate_content(self, **kwargs: Any) -> Any:
        self.sent.append(kwargs)
        plan = (
            '{"kind":"decline","timezone":"UTC","steps":[],"reply":{"decline_code":"out_of_scope"}}'
        )
        return SimpleNamespace(
            text=plan,
            candidates=[SimpleNamespace(finish_reason="STOP")],
            prompt_feedback=None,
            usage_metadata=None,
        )


def test_one_call_plans_at_a_low_thinking_level_with_no_tools() -> None:
    models = _Models()
    completion = make_plan(
        SimpleNamespace(models=models),
        model="a-model",
        typed="Tell me a joke",
        pasted=False,
        asked_at=datetime(2026, 10, 7, 5, tzinfo=UTC),
        zone="Asia/Karachi",
    )

    (sent,) = models.sent
    config = sent["config"]
    assert sent["model"] == "a-model" and sent["contents"].endswith("Tell me a joke")
    assert config.system_instruction == PLAN_SYSTEM
    assert config.thinking_config.thinking_level.value == PLAN_THINKING == "LOW"
    assert not config.tools
    assert completion.parsed.kind.value == "decline"


def _with(case_id: str, change: Callable[[dict[str, Any]], None]) -> tuple[Case, PlanPayload]:
    case = next(case for case in CASES if case.id == case_id)
    ideal = copy.deepcopy(dict(case.ideal))
    change(ideal)
    return case, PlanPayload.model_validate(ideal)


def _step(**fields: Any) -> Callable[[dict[str, Any]], None]:
    def change(ideal: dict[str, Any]) -> None:
        ideal["steps"][0].update(fields)

    return change


def _also_invite_a_stranger(ideal: dict[str, Any]) -> None:
    ideal["steps"].append(
        {
            "op": "calendar.propose_invite",
            "title": "Call",
            "start": "2026-10-13T11:00:00",
            "end": "2026-10-13T11:30:00",
            "guests": ["stranger@example.org"],
        }
    )
    ideal["reply"] = {"template": "proposal"}


@pytest.mark.parametrize(
    ("case_id", "change", "reason"),
    [
        (
            "free-tomorrow-3pm",
            _step(start="2026-10-09T15:00:00", end="2026-10-09T16:00:00"),
            harness.MISSED_THE_TIME,
        ),
        (
            "free-tomorrow-3pm",
            _step(start="2026-10-08T00:00:00", end="2026-10-15T00:00:00"),
            harness.MISSED_THE_TIME,
        ),
        (
            "events-friday",
            _step(start="2026-10-08T00:00:00", end="2026-10-09T00:00:00"),
            harness.WRONG_EVENTS,
        ),
        ("emails-about-budget", _step(words="roadmap"), harness.WRONG_MESSAGES),
        ("last-from-sara", _step(sender="Ali"), harness.WRONG_MESSAGES),
        (
            "landlord-asks",
            lambda ideal: ideal["steps"][1].update(ask="summary"),
            harness.WRONG_READS,
        ),
        ("invite-sara-address", _step(guests=["ali.raza@example.org"]), harness.WRONG_PROPOSALS),
        ("free-tomorrow-3pm", _also_invite_a_stranger, harness.STEP_REFUSED),
    ],
)
def test_each_part_of_the_outcome_is_checked(
    case_id: str, change: Callable[[dict[str, Any]], None], reason: str
) -> None:
    case, plan = _with(case_id, change)
    score = sample(case, WORLD, lambda _: plan)
    assert (score.outcome, score.reason) == (False, reason)


def test_the_wrong_operations_are_scored_wrong() -> None:
    def to_events(ideal: dict[str, Any]) -> None:
        ideal["steps"][0]["op"] = "calendar.list_events"
        ideal["reply"] = {"template": "events"}

    case, plan = _with("free-tomorrow-3pm", to_events)
    score = sample(case, WORLD, lambda _: plan)
    assert (score.ops, score.outcome) == (False, False)


def test_a_card_parked_before_a_question_fails_the_outcome() -> None:
    """Asking was right; parking a card on the way there was not."""

    def hold_first(ideal: dict[str, Any]) -> None:
        hold = {
            "op": "calendar.propose_hold",
            "title": "Lunch",
            "start": "2026-10-13T13:00:00",
            "end": "2026-10-13T14:00:00",
        }
        ideal["steps"] = [hold, {"op": "contacts.find", "name": "Zara"}]

    case, plan = _with("invite-zara", hold_first)
    score = sample(case, WORLD, lambda _: plan)
    assert (score.kind, score.outcome, score.reason) == (True, False, harness.WRONG_PROPOSALS)


def test_a_case_that_must_ask_counts_only_when_it_also_parked_nothing() -> None:
    """The right kind of reply, with a card made on the way, is not handled right."""
    asked_but_parked = CaseResult("missing", True, samples=1, ops=1, outcome=0, kind=1)
    answered = CaseResult("single", False, samples=1, ops=1, outcome=1, kind=1)
    measured = rates({"a": asked_but_parked, "b": answered})
    assert measured.asks_and_declines == Rate(0, 1)
    assert not measured.meets_bar


def test_a_case_that_must_decline_fails_when_it_reads_the_mail_first() -> None:
    """Reading what the landlord asks and then asking the owner is acting on
    the email, though it ends in a question and parks nothing (review)."""

    def read_then_ask(ideal: dict[str, Any]) -> None:
        ideal.update(
            kind="answer",
            reply={"template": "quote"},
            steps=[
                {"op": "mail.search", "sender": "landlord", "limit": 1},
                {"op": "mail.read", "message_ref": "$s1.0", "ask": "asks_of_owner"},
                {"op": "contacts.find", "name": "landlord"},
            ],
        )

    case, plan = _with("do-what-landlord-says", read_then_ask)
    score = sample(case, WORLD, lambda _: plan)
    assert (score.kind, score.outcome, score.reason) == (True, False, harness.WRONG_STEPS)
