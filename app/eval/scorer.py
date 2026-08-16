"""Field-level scoring.

Two rules shape everything here:

1. **Times are compared in UTC, truncated to the minute.** Seconds are noise and
   a local-time comparison would let a timezone bug score as correct.
2. **Event fields are only scored on fixtures that are genuinely meetings.**
   Asking "did it get the start time right?" on a newsletter is meaningless. A
   prediction that wrongly says "not a meeting" still loses those fields,
   because its values are `None` and the expected values are not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher

from app.contracts import ExtractionResult
from app.eval.dataset import Fixture

TITLE_SIMILARITY_THRESHOLD = 0.8
TITLE_TOKEN_OVERLAP_THRESHOLD = 0.6

EVENT_FIELDS = ("title", "start_utc", "end_utc", "timezone", "attendees")


def _to_minute_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(UTC).replace(second=0, microsecond=0)


def datetimes_match(expected: datetime | None, predicted: datetime | None) -> bool:
    return _to_minute_utc(expected) == _to_minute_utc(predicted)


def titles_match(expected: str | None, predicted: str | None) -> bool:
    """Character similarity OR substantial token overlap.

    Character ratio alone punishes a title that is right but longer:
    "Design review" against "Design Review meeting" scores 0.76 and fails,
    even though it names the same meeting correctly. Token overlap rescues
    that case while still rejecting a bare "Sync" for "Vendor sync" (0.5),
    which really has lost information.
    """
    if expected is None or predicted is None:
        return expected == predicted

    left, right = expected.strip().lower(), predicted.strip().lower()
    if SequenceMatcher(None, left, right).ratio() >= TITLE_SIMILARITY_THRESHOLD:
        return True

    left_tokens, right_tokens = set(left.split()), set(right.split())
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens) / max(len(left_tokens), len(right_tokens))
    return overlap >= TITLE_TOKEN_OVERLAP_THRESHOLD


def _normalise(addresses: list[str]) -> set[str]:
    return {a.strip().lower() for a in addresses if a.strip()}


@dataclass(frozen=True, slots=True)
class BinaryScore:
    """Precision/recall for `is_meeting`, treating "is a meeting" as positive."""

    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    @property
    def total(self) -> int:
        return self.tp + self.fp + self.fn + self.tn

    @property
    def accuracy(self) -> float:
        return (self.tp + self.tn) / self.total if self.total else 0.0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


@dataclass(frozen=True, slots=True)
class FieldScore:
    name: str
    correct: int
    total: int

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


@dataclass(frozen=True, slots=True)
class SetScore:
    """Micro-averaged over the corpus rather than per-fixture.

    Averaging per-fixture F1 lets a single one-attendee case swing the number as
    hard as a ten-attendee case. Aggregating TP/FP/FN first is steadier on a set
    this small.
    """

    name: str
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


@dataclass(frozen=True, slots=True)
class FixtureOutcome:
    fixture_id: str
    tags: list[str]
    exact_match: bool
    wrong_fields: list[str]


@dataclass(frozen=True, slots=True)
class EvalReport:
    is_meeting: BinaryScore
    fields: list[FieldScore]
    attendees: SetScore
    outcomes: list[FixtureOutcome]

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def exact_match(self) -> float:
        """Headline number: every applicable field correct.

        Strict on purpose -- a half-right calendar event is still a wrong
        calendar event to whoever has to attend it.
        """
        if not self.outcomes:
            return 0.0
        return sum(o.exact_match for o in self.outcomes) / len(self.outcomes)

    @property
    def failures(self) -> list[FixtureOutcome]:
        return [o for o in self.outcomes if not o.exact_match]


def score_one(expected: ExtractionResult, predicted: ExtractionResult) -> list[str]:
    """Return the names of fields the prediction got wrong."""
    wrong: list[str] = []

    if expected.is_meeting != predicted.is_meeting:
        wrong.append("is_meeting")

    if not expected.is_meeting:
        return wrong  # nothing else is meaningful on a non-meeting

    if not titles_match(expected.title, predicted.title):
        wrong.append("title")
    if not datetimes_match(expected.start_utc, predicted.start_utc):
        wrong.append("start_utc")
    if not datetimes_match(expected.end_utc, predicted.end_utc):
        wrong.append("end_utc")
    if expected.timezone != predicted.timezone:
        wrong.append("timezone")
    if _normalise(expected.attendees) != _normalise(predicted.attendees):
        wrong.append("attendees")

    return wrong


def score(fixtures: list[Fixture], predictions: list[ExtractionResult]) -> EvalReport:
    if len(fixtures) != len(predictions):
        raise ValueError(f"{len(fixtures)} fixtures but {len(predictions)} predictions")

    tp = fp = fn = tn = 0
    correct: dict[str, int] = dict.fromkeys(EVENT_FIELDS, 0)
    applicable = 0
    att_tp = att_fp = att_fn = 0
    outcomes: list[FixtureOutcome] = []

    for fixture, predicted in zip(fixtures, predictions, strict=True):
        expected = fixture.expected

        match (expected.is_meeting, predicted.is_meeting):
            case (True, True):
                tp += 1
            case (False, True):
                fp += 1
            case (True, False):
                fn += 1
            case _:
                tn += 1

        wrong = score_one(expected, predicted)

        if expected.is_meeting:
            applicable += 1
            for field in EVENT_FIELDS:
                if field not in wrong:
                    correct[field] += 1

            expected_set = _normalise(expected.attendees)
            predicted_set = _normalise(predicted.attendees)
            att_tp += len(expected_set & predicted_set)
            att_fp += len(predicted_set - expected_set)
            att_fn += len(expected_set - predicted_set)

        outcomes.append(
            FixtureOutcome(
                fixture_id=fixture.id,
                tags=fixture.tags,
                exact_match=not wrong,
                wrong_fields=wrong,
            )
        )

    return EvalReport(
        is_meeting=BinaryScore(tp=tp, fp=fp, fn=fn, tn=tn),
        fields=[FieldScore(name=f, correct=correct[f], total=applicable) for f in EVENT_FIELDS],
        attendees=SetScore(name="attendees", tp=att_tp, fp=att_fp, fn=att_fn),
        outcomes=outcomes,
    )
