"""What /health knows about the poller, without asking the database."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from app.obs.liveness import Liveness, configure_logging

BOOT = datetime(2026, 10, 6, 0, 0, tzinfo=UTC)
TEN = timedelta(minutes=10)


def test_a_fresh_process_is_not_overdue_during_its_grace() -> None:
    """The first tick only fires one interval after boot."""
    assert not Liveness(booted_at=BOOT).poll_overdue(BOOT + timedelta(minutes=40), TEN)


def test_no_success_three_intervals_after_the_first_tick_is_overdue() -> None:
    assert Liveness(booted_at=BOOT).poll_overdue(BOOT + timedelta(minutes=41), TEN)


def test_a_recent_success_is_not_overdue() -> None:
    live = Liveness(booted_at=BOOT)
    live.poll_finished(ok=True, at=BOOT + timedelta(hours=5))

    assert not live.poll_overdue(BOOT + timedelta(hours=5, minutes=30), TEN)


def test_failed_polls_do_not_reset_the_clock() -> None:
    live = Liveness(booted_at=BOOT)
    live.poll_finished(ok=True, at=BOOT + TEN)
    for minutes in (20, 30, 40, 50):
        live.poll_finished(ok=False, at=BOOT + timedelta(minutes=minutes))

    assert live.poll_overdue(BOOT + timedelta(minutes=41, seconds=1), TEN)


def test_no_open_decision_is_never_stuck() -> None:
    assert not Liveness(booted_at=BOOT).decision_stuck(BOOT + timedelta(days=3))


def test_a_decision_open_for_over_an_hour_is_stuck() -> None:
    live = Liveness(booted_at=BOOT)
    live.decisions_checked(BOOT)

    assert not live.decision_stuck(BOOT + timedelta(minutes=59))
    assert live.decision_stuck(BOOT + timedelta(minutes=61))


def test_the_job_seeing_an_empty_queue_clears_the_clock() -> None:
    live = Liveness(booted_at=BOOT)
    live.decisions_checked(BOOT)
    live.decisions_checked(None)

    assert not live.decision_stuck(BOOT + timedelta(hours=2))


def test_a_decision_recorded_here_starts_the_clock_even_if_the_job_is_wedged() -> None:
    """The job refreshes the record; a hung job never would. A decision
    recorded in this process starts the clock anyway, so the hang shows."""
    live = Liveness(booted_at=BOOT)
    live.decision_recorded(BOOT)
    live.decision_recorded(BOOT + timedelta(minutes=30))  # an older one is already open

    assert live.decision_stuck(BOOT + timedelta(minutes=61))


def test_logging_makes_the_apps_info_lines_visible() -> None:
    """Under uvicorn nothing configures the root logger, so without this the
    scheduler's INFO lines -- the record of every poll -- went nowhere."""
    app_logger = logging.getLogger("app")
    level, handlers = app_logger.level, list(app_logger.handlers)
    try:
        configure_logging()
        configure_logging()  # idempotent: a second call adds no second handler

        assert logging.getLogger("app.jobs.scheduler").getEffectiveLevel() == logging.INFO
        assert len(app_logger.handlers) == len(handlers) + 1
    finally:
        app_logger.setLevel(level)
        app_logger.handlers[:] = handlers
