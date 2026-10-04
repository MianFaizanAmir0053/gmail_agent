"""The tool-routing eval (M19, D10): whether the planner picks the right
operations, and whether running its plan ends where it should.

Each case in `data/planner/routing/` is the owner's typed request, any pasted
text, and what should come of it. Every case shares one world -- a mailbox, a
calendar, the confirmed contacts and the moment the owner asked -- in
`data/planner/world.json`. A sampled plan is checked by `to_plan` and run by
the real executor against fakes of that world, and scored three ways, by what
the run did rather than by how the plan is spelled:

- **operations:** the plan's operations, as a multiset, are one of the
  case's, so renumbered references do not matter;
- **outcome:** the slot proposed, each guest's mailbox and how it was named,
  the messages found, the events listed, the message read and what was asked
  of it are what the case expects, and nothing else was refused or parked.
  Free text, such as search words and titles, is never compared;
- **kind:** the reply is an answer, an ask or a decline, as the case says.
  Asking is a clarify plan, or a step that ends the turn with a question.

The bar (decision 7): the right operations on at least 95% of samples, the
right outcome on at least 90%, and every case that must ask or decline
handled right on every sample. Five samples a case.

    .\\tasks.ps1 planner-eval                # five samples a case
    .\\tasks.ps1 planner-eval --baseline     # and write the committed baseline

The baseline is tied by a hash to what shapes the planner's call, the world,
the cases and the model (`code_hash`), and CI checks it. It spends on the
development key, metered by the gate on `DATABASE_URL`: one call a sample.
Results name cases and fixed reasons, never a request's text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast
from zoneinfo import ZoneInfo

from app.config import WorkingHours
from app.eval.mailbox import matches, terms
from app.google.calendar import BusyInterval, CalendarEvent, CalendarEvents
from app.google.gmail import SYNC_HEADERS, MessageMeta
from app.planner.execute import (
    Busy,
    Events,
    Found,
    FreeSlot,
    Outcome,
    Proposal,
    Tools,
    Turn,
    execute,
)
from app.planner.ops import FoundMessage
from app.planner.origins import PastedText
from app.planner.plan import Ask, InvalidPlanError, Kind, Plan, PlanPayload, Slot, to_plan

REPO = Path(__file__).resolve().parents[2]
RESULTS_DIR = REPO / "results"
BASELINE = RESULTS_DIR / "planner-baseline.json"
WORLD_FILE = REPO / "data" / "planner" / "world.json"
CASES_DIR = REPO / "data" / "planner" / "routing"
SAMPLES = 5

SHAPING = (
    "app/planner/plan.py",
    "app/planner/prompts.py",
    "app/extraction/llm.py",
    "app/extraction/payloads.py",
    "app/policy/models.py",
)
"""The code that shapes the planner's call: the schema and its check, the
prompt, how a call is sent, the schema's cleaner and the metered client. With
the world, every case and the planner's model, it is what the baseline's hash
covers (D10)."""

OPS_BAR = 0.95
OUTCOME_BAR = 0.90
ASKS_AND_DECLINES_BAR = 1.0

KINDS = frozenset({"answer", "ask", "decline"})
CATEGORIES = frozenset(
    {"single", "lookalike", "multistep", "irrelevance", "missing", "unsupported", "email"}
)
"""D10's categories: each operation on its own, look-alike operations,
scheduling in several steps, irrelevance, missing details, unsupported
actions, and what an email says."""

PLAN_REFUSED = "the plan was refused"
WRONG_KIND = "the wrong kind of reply"
STEP_REFUSED = "a step was refused"
MISSED_THE_TIME = "free/busy missed the time asked about"
WRONG_SLOT = "the wrong free slot"
WRONG_EVENTS = "the wrong events"
WRONG_MESSAGES = "the wrong messages"
WRONG_READS = "the wrong message read"
WRONG_PROPOSALS = "the wrong proposal"
REASONS = frozenset(
    {
        PLAN_REFUSED,
        WRONG_KIND,
        STEP_REFUSED,
        MISSED_THE_TIME,
        WRONG_SLOT,
        WRONG_EVENTS,
        WRONG_MESSAGES,
        WRONG_READS,
        WRONG_PROPOSALS,
    }
)
"""Every reason a sample's outcome can fail for: fixed, so a result never
quotes a case."""

LONGEST_TIME_READ = timedelta(days=1)
"""A free/busy read for one time may cover at most a day around it."""


# --- the world and the cases --------------------------------------------------


@dataclass(frozen=True)
class MailRecord:
    id: str
    thread_id: str
    labels: frozenset[str]
    at: datetime
    headers: Mapping[str, str]


@dataclass(frozen=True)
class World:
    owner: str
    aliases: tuple[str, ...]
    zone: ZoneInfo
    asked_at: datetime
    hours: WorkingHours
    contacts: tuple[str, ...]
    events: tuple[CalendarEvent, ...]
    messages: tuple[MailRecord, ...]

    def instant(self, wall: str) -> datetime:
        """A wall-clock time in the world's zone, as UTC."""
        return datetime.fromisoformat(wall).replace(tzinfo=self.zone).astimezone(UTC)

    def wall(self, instant: datetime) -> str:
        return instant.astimezone(self.zone).strftime("%Y-%m-%dT%H:%M")


def load_world(path: Path = WORLD_FILE) -> World:
    raw = json.loads(path.read_text("utf-8"))
    zone = ZoneInfo(raw["zone"])

    def instant(wall: str) -> datetime:
        return datetime.fromisoformat(wall).replace(tzinfo=zone).astimezone(UTC)

    owner = str(raw["owner"])
    return World(
        owner=owner,
        aliases=tuple(raw["aliases"]),
        zone=zone,
        asked_at=instant(raw["asked_at"]),
        hours=WorkingHours.parse(raw["working_hours"]),
        contacts=tuple(raw["contacts"]),
        events=tuple(
            CalendarEvent(
                title=event["title"],
                start=instant(event["start"]),
                end=instant(event["end"]),
                all_day=False,
                organizer=event.get("organizer", owner),
                by_owner="organizer" not in event,
            )
            for event in raw["events"]
        ),
        messages=tuple(_record(message, instant, owner) for message in raw["messages"]),
    )


def _record(raw: Mapping[str, Any], instant: Callable[[str], datetime], owner: str) -> MailRecord:
    headers = {"From": raw["from"], "To": raw.get("to", owner), "Subject": raw["subject"]}
    if raw.get("dmarc"):
        domain = parseaddr(raw["from"])[1].rpartition("@")[2]
        headers["Authentication-Results"] = f"mx.google.com; dmarc=pass header.from={domain}"
    return MailRecord(
        id=raw["id"],
        thread_id=f"t-{raw['id']}",
        labels=frozenset(raw.get("labels", ["INBOX"])),
        at=instant(raw["at"]),
        headers=MappingProxyType(headers),
    )


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    typed: str
    pasted: str | None
    expect: Mapping[str, Any]
    ideal: Mapping[str, Any]
    """The plan a perfect planner writes. The harness's own tests run it."""

    @property
    def kinds(self) -> frozenset[str]:
        kind = self.expect["kind"]
        return frozenset([kind] if isinstance(kind, str) else kind)

    @property
    def must_ask_or_decline(self) -> bool:
        return "answer" not in self.kinds

    @property
    def ops(self) -> list[Counter[str]]:
        """The multisets of operations that are right. A case that must ask or
        decline runs none, unless it lists the steps that end in a question."""
        return [Counter(ops) for ops in self.expect.get("ops", [[]])]


def load_cases(directory: Path = CASES_DIR) -> list[Case]:
    return [_case(json.loads(path.read_text("utf-8"))) for path in sorted(directory.glob("*.json"))]


def _case(raw: Mapping[str, Any]) -> Case:
    case = Case(
        id=raw["id"],
        category=raw["category"],
        typed=raw["typed"],
        pasted=raw.get("pasted"),
        expect=raw["expect"],
        ideal=raw["ideal"],
    )
    if case.category not in CATEGORIES or not case.kinds or not case.kinds <= KINDS:
        raise ValueError(f"{case.id}: an unknown category or kind")
    if "answer" in case.kinds and "ops" not in case.expect:
        raise ValueError(f"{case.id}: a case that may answer lists its operations")
    return case


# --- fakes of the world -----------------------------------------------------------


class CaseMail:
    """The world's mailbox, searched as Gmail reads the planner's queries."""

    def __init__(self, messages: Iterable[MailRecord]) -> None:
        self._messages = {message.id: message for message in messages}

    def search(self, query: str, limit: int = 200) -> list[str]:
        wanted = terms(query)
        found = sorted(
            (
                message
                for message in self._messages.values()
                if matches(wanted, labels=message.labels, at=message.at, headers=message.headers)
            ),
            key=lambda message: (message.at, message.id),
            reverse=True,
        )
        return [message.id for message in found[:limit]]

    def message_metadata(
        self, message_id: str, headers: tuple[str, ...] = SYNC_HEADERS
    ) -> MessageMeta:
        message = self._messages[message_id]
        return MessageMeta(
            id=message.id,
            thread_id=message.thread_id,
            label_ids=message.labels,
            internal_date=message.at,
            headers=MappingProxyType(
                {name: message.headers[name] for name in headers if name in message.headers}
            ),
        )


class CaseCalendar:
    """The world's primary calendar, every event of it busy time."""

    def __init__(self, events: Iterable[CalendarEvent]) -> None:
        self._events = tuple(events)

    def _during(self, start: datetime, end: datetime) -> tuple[CalendarEvent, ...]:
        return tuple(event for event in self._events if event.start < end and start < event.end)

    def freebusy(self, start_utc: datetime, end_utc: datetime) -> list[BusyInterval]:
        return [BusyInterval(event.start, event.end) for event in self._during(start_utc, end_utc)]

    def list_events(self, start_utc: datetime, end_utc: datetime) -> CalendarEvents:
        return CalendarEvents(events=self._during(start_utc, end_utc), more=False)


@dataclass
class _Witness:
    """What the run read and parked: a reader that answers nothing, and a
    queue that keeps what it is handed."""

    reads: list[tuple[str, str]] = field(default_factory=list)
    parked: list[Proposal] = field(default_factory=list)

    def read(self, source: FoundMessage | PastedText, ask: Ask) -> object:
        name = "$paste" if isinstance(source, PastedText) else source.id
        self.reads.append((name, ask.value))
        return {"read": name}

    def park(self, proposal: Proposal) -> str:
        self.parked.append(proposal)
        return f"chat-eval-{proposal.step}"


# --- one sample -------------------------------------------------------------------

Planner = Callable[[Case], PlanPayload]


@dataclass(frozen=True, slots=True)
class Score:
    ops: bool
    outcome: bool
    kind: bool
    reason: str | None = None
    """Why the outcome was wrong: one of `REASONS`."""


def sample(case: Case, world: World, planner: Planner) -> Score:
    """One plan for `case`, checked, run on the world's fakes and scored."""
    payload = planner(case)
    ops = Counter(step.op.value for step in payload.steps)
    ops_right = ops in case.ops
    try:
        plan = to_plan(payload, pasted=bool(case.pasted))
    except InvalidPlanError:
        return Score(ops_right, outcome=False, kind=False, reason=PLAN_REFUSED)

    outcome, witness = play(case, world, plan)
    kind = "ask" if plan.kind is Kind.CLARIFY or outcome.question else plan.kind.value
    if kind not in case.kinds:
        return Score(ops_right, outcome=False, kind=False, reason=WRONG_KIND)
    if kind != "answer":
        # Asking or declining runs to no card.
        missed = WRONG_PROPOSALS if witness.parked else None
    else:
        missed = _missed(case.expect, world, outcome, witness)
    return Score(ops_right, outcome=missed is None, kind=True, reason=missed)


def play(case: Case, world: World, plan: Plan) -> tuple[Outcome, _Witness]:
    """Run a checked plan for `case` on the world's fakes, as production runs
    one: the real executor, the real search query, the real slot finder."""
    witness = _Witness()
    tools = Tools(
        gmail=CaseMail(world.messages),
        calendar=CaseCalendar(world.events),
        contacts=lambda: world.contacts,
        read=witness.read,
        park=witness.park,
        hours=world.hours,
    )
    turn = Turn(
        typed=case.typed,
        pasted=case.pasted,
        owner=frozenset({world.owner, *world.aliases}),
        zone=world.zone,
    )
    return execute(plan, turn, tools, now=world.asked_at), witness


def _missed(
    expect: Mapping[str, Any], world: World, outcome: Outcome, witness: _Witness
) -> str | None:
    """The first way the run's outcome differs from the case's, or None."""
    if outcome.refused:
        return STEP_REFUSED
    results = [ran.result for ran in outcome.ran]
    if "busy_at" in expect:
        at = world.instant(expect["busy_at"])
        if not any(
            isinstance(result, Busy)
            and result.start <= at < result.end
            and result.end - result.start <= LONGEST_TIME_READ
            for result in results
        ):
            return MISSED_THE_TIME
    if "free_slot" in expect:
        want = Slot(world.instant(expect["free_slot"][0]), world.instant(expect["free_slot"][1]))
        if [result.slot for result in results if isinstance(result, FreeSlot)] != [want]:
            return WRONG_SLOT
    if "events" in expect:
        listed = [
            event.title
            for result in results
            if isinstance(result, Events)
            for event in result.events.events
        ]
        if sorted(listed) != sorted(expect["events"]):
            return WRONG_EVENTS
    found = [result.messages for result in results if isinstance(result, Found)]
    if "messages" in expect and [m.id for ms in found for m in ms] != expect["messages"]:
        return WRONG_MESSAGES
    if "newest" in expect and [ms[0].id for ms in found if ms][:1] != [expect["newest"]]:
        return WRONG_MESSAGES
    if witness.reads != [tuple(read) for read in expect.get("reads", [])]:
        return WRONG_READS
    if [_card(proposal, world) for proposal in witness.parked] != expect.get("proposals", []):
        return WRONG_PROPOSALS
    return None


def _card(proposal: Proposal, world: World) -> dict[str, Any]:
    """A proposal as a case writes it: wall-clock times, guests by address and source."""
    return {
        "start": world.wall(proposal.start),
        "end": world.wall(proposal.end),
        "guests": sorted([guest.address, guest.source] for guest in proposal.guests),
    }


# --- the run ----------------------------------------------------------------------


@dataclass
class CaseResult:
    category: str
    must_ask_or_decline: bool
    samples: int = 0
    ops: int = 0
    outcome: int = 0
    kind: int = 0
    reasons: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)
    """Exception types: a sample that raised proves nothing either way."""


def run(
    cases: Iterable[Case],
    world: World,
    planner: Planner,
    *,
    samples: int = SAMPLES,
    progress: Callable[[str], None] = print,
) -> dict[str, CaseResult]:
    results: dict[str, CaseResult] = {}
    for case in cases:
        result = CaseResult(case.category, case.must_ask_or_decline)
        for _ in range(samples):
            try:
                score = sample(case, world, planner)
            except Exception as exc:  # one bad sample must not void the run
                result.errors.append(type(exc).__name__)
                continue
            result.samples += 1
            result.ops += score.ops
            result.outcome += score.outcome
            result.kind += score.kind
            if score.reason is not None:
                result.reasons.add(score.reason)
        results[case.id] = result
        progress(
            f"  {case.id}  operations {result.ops}/{result.samples}"
            f"  outcome {result.outcome}/{result.samples}"
            + (f"  {len(result.errors)} raised" if result.errors else "")
        )
    return results


@dataclass(frozen=True, slots=True)
class Rate:
    right: int
    samples: int

    @property
    def share(self) -> float:
        return self.right / self.samples if self.samples else 0.0

    def interval(self, z: float = 1.96) -> tuple[float, float]:
        """The Wilson interval: below a few hundred samples, the share alone
        overstates what is known (D10's research)."""
        if not self.samples:
            return (0.0, 1.0)
        n, share = self.samples, self.share
        middle = (share + z * z / (2 * n)) / (1 + z * z / n)
        spread = z * math.sqrt(share * (1 - share) / n + z * z / (4 * n * n)) / (1 + z * z / n)
        return (max(0.0, middle - spread), min(1.0, middle + spread))


@dataclass(frozen=True, slots=True)
class Rates:
    ops: Rate
    outcome: Rate
    asks_and_declines: Rate
    """Samples of the cases that must ask or decline, handled right: the right
    kind of reply, and no card."""

    @property
    def meets_bar(self) -> bool:
        return (
            self.ops.samples > 0
            and self.ops.share >= OPS_BAR
            and self.outcome.share >= OUTCOME_BAR
            and self.asks_and_declines.share >= ASKS_AND_DECLINES_BAR
        )


def rates(results: Mapping[str, CaseResult]) -> Rates:
    every = list(results.values())
    must = [result for result in every if result.must_ask_or_decline]
    samples = sum(result.samples for result in every)
    return Rates(
        ops=Rate(sum(result.ops for result in every), samples),
        outcome=Rate(sum(result.outcome for result in every), samples),
        asks_and_declines=Rate(
            sum(result.outcome for result in must), sum(result.samples for result in must)
        ),
    )


def to_dict(
    results: Mapping[str, CaseResult], *, model: str, samples: int, code: str
) -> dict[str, Any]:
    """The run's record. `code` is the hash taken when the run began, so an
    edit made while it ran is not credited to it."""
    measured = rates(results)
    return {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "code_hash": code,
        "model": model,
        "samples_per_case": samples,
        "rates": {
            name: {
                "right": rate.right,
                "samples": rate.samples,
                "share": round(rate.share, 4),
                "interval": [round(bound, 4) for bound in rate.interval()],
            }
            for name, rate in (
                ("ops", measured.ops),
                ("outcome", measured.outcome),
                ("asks_and_declines", measured.asks_and_declines),
            )
        },
        "bar": {"ops": OPS_BAR, "outcome": OUTCOME_BAR, "asks_and_declines": ASKS_AND_DECLINES_BAR},
        "meets_bar": measured.meets_bar,
        "errors": sum(len(result.errors) for result in results.values()),
        "cases": {
            case_id: {
                "category": result.category,
                "must_ask_or_decline": result.must_ask_or_decline,
                "samples": result.samples,
                "ops": result.ops,
                "outcome": result.outcome,
                "kind": result.kind,
                "reasons": sorted(result.reasons),
                "errors": sorted(result.errors),
            }
            for case_id, result in sorted(results.items())
        },
    }


def is_baseline(data: Mapping[str, Any], case_ids: Sequence[str]) -> bool:
    """A run fit to be the baseline: every case at five samples, no error,
    and the bar met."""
    cases = data.get("cases", {})
    return (
        data.get("errors") == 0
        and data.get("meets_bar") is True
        and sorted(cases) == sorted(case_ids)
        and all(case["samples"] >= SAMPLES for case in cases.values())
    )


def production_model() -> str:
    """The planner's model in production: the setting's default, which nothing
    in production overrides, whatever this machine's environment names."""
    from app.config import Settings

    return str(Settings.model_fields["planner_model"].default)


def code_hash(root: Path = REPO) -> str:
    """SHA-256 over what shapes the planner's call, the world, every case and
    the planner's model. Line endings are folded, so a checkout on any system
    hashes the same."""
    data = root / WORLD_FILE.relative_to(REPO)
    cases = sorted((root / CASES_DIR.relative_to(REPO)).glob("*.json"))
    digest = hashlib.sha256()
    for path in [*(root / name for name in SHAPING), data, *cases]:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
        digest.update(b"\0")
    digest.update(production_model().encode())
    return digest.hexdigest()


def production_planner(world: World) -> Planner:
    """The real planner, on this machine's development key and metered by its
    gate, with production's model whatever the environment names."""
    from app.config import get_settings
    from app.extraction.llm import GenaiLike
    from app.planner.plan import make_plan
    from app.policy import models

    settings = get_settings()
    client = cast(GenaiLike, models.client(settings, models.local_gate(settings)))
    model = production_model()

    def plan(case: Case) -> PlanPayload:
        return make_plan(
            client,
            model=model,
            typed=case.typed,
            pasted=bool(case.pasted),
            asked_at=world.asked_at,
            zone=world.zone.key,
        ).parsed

    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description="Score the planner on the routing golden set.")
    parser.add_argument("--samples", type=int, default=SAMPLES)
    parser.add_argument("--case", action="append", help="run only this case (repeatable)")
    parser.add_argument(
        "--baseline", action="store_true", help="also write the baseline, if the run meets the bar"
    )
    parser.add_argument("--no-save", action="store_true", help="print only; write no file")
    args = parser.parse_args()
    # Refused before anything is spent: a baseline covers every case at five
    # samples, and it is a file.
    if args.baseline and (args.case or args.samples < SAMPLES or args.no_save):
        raise SystemExit("--baseline runs every case at five samples, and writes the file.")

    world = load_world()
    every = load_cases()
    cases = [case for case in every if not args.case or case.id in args.case]
    if not cases:
        raise SystemExit("No case matched.")
    code = code_hash()
    model = production_model()
    print(f"{len(cases)} cases, {args.samples} samples each, on {model}")
    results = run(cases, world, production_planner(world), samples=args.samples)
    data = to_dict(results, model=model, samples=args.samples, code=code)
    shares = {name: rate["share"] for name, rate in data["rates"].items()}
    print(f"{shares}; meets the bar: {data['meets_bar']}; {data['errors']} sample(s) raised")

    if not args.no_save:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = RESULTS_DIR / f"planner-{stamp}.json"
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {path}")
    if args.baseline:
        if not is_baseline(data, [case.id for case in every]):
            raise SystemExit(
                "Not written: a baseline covers every case, five samples each, and meets the bar."
            )
        BASELINE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {BASELINE}")


if __name__ == "__main__":
    main()
