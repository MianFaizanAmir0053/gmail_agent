"""The tick record that proves unattended running (M15). Needs Postgres."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from app.store.job_runs import JobRuns

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 6, 0, 0, tzinfo=UTC)


@pytest.fixture
def runs(conn: psycopg.Connection) -> JobRuns:
    return JobRuns(conn)


def _tick(runs: JobRuns, minutes: int, *, ok: bool = True, job: str = "poll") -> None:
    at = T0 + timedelta(minutes=minutes)
    runs.record(job, at, at + timedelta(seconds=5), ok=ok)


def test_steady_ticks_leave_only_the_interval_as_the_longest_gap(runs: JobRuns) -> None:
    for minute in range(0, 61, 10):
        _tick(runs, minute)

    gap = runs.longest_gap("poll", T0, T0 + timedelta(minutes=60, seconds=5))

    assert gap <= timedelta(minutes=10)


def test_a_silent_stretch_in_the_middle_is_found(runs: JobRuns) -> None:
    for minute in (0, 10, 20, 70, 80):
        _tick(runs, minute)

    gap = runs.longest_gap("poll", T0, T0 + timedelta(minutes=80, seconds=5))

    assert gap == timedelta(minutes=50)


def test_silence_at_the_start_of_the_window_counts(runs: JobRuns) -> None:
    """A machine that only came up late must not pass by having no gaps *between* ticks."""
    for minute in (60, 70):
        _tick(runs, minute)

    gap = runs.longest_gap("poll", T0, T0 + timedelta(minutes=70, seconds=5))

    assert gap >= timedelta(minutes=60)


def test_silence_at_the_end_of_the_window_counts(runs: JobRuns) -> None:
    """A machine dead on the last day must not pass."""
    for minute in (0, 10):
        _tick(runs, minute)

    gap = runs.longest_gap("poll", T0, T0 + timedelta(minutes=100))

    assert gap >= timedelta(minutes=89)


def test_failed_ticks_do_not_close_a_gap(runs: JobRuns) -> None:
    _tick(runs, 0)
    for minute in range(10, 60, 10):
        _tick(runs, minute, ok=False)
    _tick(runs, 60)

    gap = runs.longest_gap("poll", T0, T0 + timedelta(minutes=60, seconds=5))

    assert gap == timedelta(minutes=60)


def test_other_jobs_do_not_count(runs: JobRuns) -> None:
    _tick(runs, 0)
    _tick(runs, 30, job="ingest")
    _tick(runs, 60)

    gap = runs.longest_gap("poll", T0, T0 + timedelta(minutes=60, seconds=5))

    assert gap == timedelta(minutes=60)
