from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.contracts import EmailMessage, ExtractionResult
from app.eval.dataset import Fixture
from app.eval.scorer import datetimes_match, score, score_one, titles_match

NOW = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
START = datetime(2026, 8, 19, 11, 0, tzinfo=UTC)
END = START + timedelta(hours=1)


def _meeting(**overrides: object) -> ExtractionResult:
    base: dict[str, object] = {
        "is_meeting": True,
        "title": "Design review",
        "start_utc": START,
        "end_utc": END,
        "timezone": "Asia/Karachi",
        "attendees": ["sara@example.com"],
        "confidence": 0.9,
        "reasoning": "",
    }
    return ExtractionResult.model_validate(base | overrides)


def _not_meeting() -> ExtractionResult:
    return ExtractionResult(is_meeting=False, confidence=0.9, reasoning="")


def _fixture(fixture_id: str, expected: ExtractionResult, tags: list[str] | None = None) -> Fixture:
    return Fixture(
        id=fixture_id,
        tags=tags or [],
        now_utc=NOW,
        user_timezone="Asia/Karachi",
        email=EmailMessage(
            id=fixture_id,
            thread_id=fixture_id,
            subject="s",
            body_text="b",
            sender="sara@example.com",
            recipients=["me@example.com"],
            received_at=NOW,
        ),
        expected=expected,
    )


# --- comparison primitives -------------------------------------------------


def test_datetimes_match_ignores_seconds() -> None:
    assert datetimes_match(START, START.replace(second=45))


def test_datetimes_match_across_equivalent_offsets() -> None:
    """11:00Z and 16:00+05:00 are the same instant and must score equal."""
    karachi = timezone(timedelta(hours=5))
    assert datetimes_match(START, START.astimezone(karachi))


def test_datetimes_differ_by_an_hour_is_a_miss() -> None:
    assert not datetimes_match(START, START + timedelta(hours=1))


def test_titles_match_is_fuzzy_but_not_blind() -> None:
    assert titles_match("Design review", "design  review")
    assert titles_match("Design review", "Design Review meeting")  # extra qualifier is fine
    assert not titles_match("Design review", "Vendor sync")


def test_title_dropping_the_distinguishing_word_is_a_miss() -> None:
    """ "Sync" for "Vendor sync" has lost the only word that identified it."""
    assert not titles_match("Vendor sync", "Sync")


def test_title_none_only_matches_none() -> None:
    assert titles_match(None, None)
    assert not titles_match("Design review", None)


# --- per-fixture scoring ---------------------------------------------------


def test_perfect_prediction_has_no_wrong_fields() -> None:
    assert score_one(_meeting(), _meeting()) == []


def test_non_meeting_only_scores_the_flag() -> None:
    """Event fields are meaningless on a newsletter; a stray title must not count."""
    predicted = ExtractionResult(is_meeting=False, title="ignored", confidence=0.1, reasoning="")
    assert score_one(_not_meeting(), predicted) == []


def test_missing_meeting_loses_every_event_field() -> None:
    wrong = score_one(_meeting(), _not_meeting())
    assert wrong == ["is_meeting", "title", "start_utc", "end_utc", "timezone", "attendees"]


def test_attendee_comparison_is_case_insensitive_and_unordered() -> None:
    predicted = _meeting(attendees=["SARA@Example.com "])
    assert score_one(_meeting(), predicted) == []


def test_wrong_timezone_is_caught_even_when_instant_is_right() -> None:
    """The instant can be correct while the stored zone is wrong -- still a bug."""
    wrong = score_one(_meeting(), _meeting(timezone="America/Los_Angeles"))
    assert wrong == ["timezone"]


# --- corpus scoring --------------------------------------------------------


def test_headline_is_strict_exact_match() -> None:
    fixtures = [_fixture("a", _meeting()), _fixture("b", _meeting())]
    predictions = [_meeting(), _meeting(title="Totally different")]

    report = score(fixtures, predictions)

    assert report.exact_match == pytest.approx(0.5)
    assert [o.fixture_id for o in report.failures] == ["b"]


def test_classification_counts() -> None:
    fixtures = [
        _fixture("a", _meeting()),
        _fixture("b", _meeting()),
        _fixture("c", _not_meeting()),
    ]
    predictions = [_meeting(), _not_meeting(), _meeting()]

    clf = score(fixtures, predictions).is_meeting

    assert (clf.tp, clf.fn, clf.fp, clf.tn) == (1, 1, 1, 0)
    assert clf.precision == pytest.approx(0.5)
    assert clf.recall == pytest.approx(0.5)


def test_field_totals_count_only_meeting_fixtures() -> None:
    fixtures = [_fixture("a", _meeting()), _fixture("b", _not_meeting())]
    report = score(fixtures, [_meeting(), _not_meeting()])

    assert all(f.total == 1 for f in report.fields)
    assert report.exact_match == pytest.approx(1.0)


def test_attendee_micro_f1_aggregates_across_fixtures() -> None:
    fixtures = [
        _fixture("a", _meeting(attendees=["x@example.com", "y@example.com"])),
        _fixture("b", _meeting(attendees=["z@example.com"])),
    ]
    predictions = [
        _meeting(attendees=["x@example.com"]),  # 1 tp, 1 fn
        _meeting(attendees=["z@example.com", "w@example.com"]),  # 1 tp, 1 fp
    ]

    att = score(fixtures, predictions).attendees

    assert (att.tp, att.fp, att.fn) == (2, 1, 1)
    assert att.f1 == pytest.approx(2 / 3)


def test_length_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="predictions"):
        score([_fixture("a", _meeting())], [])


def test_empty_corpus_scores_zero_not_crash() -> None:
    assert score([], []).exact_match == 0.0
