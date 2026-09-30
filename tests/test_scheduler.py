"""Scheduler configuration and failure isolation."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.channel.alerts import TokenAlert
from app.channel.reconcile import ReconcileResult
from app.config import Settings
from app.google.tokens import TokenHealth, TokenMetadata
from app.jobs.ingest_job import incremental_query, scheduled_ingest
from app.jobs.poll import STOPPING, PollResult
from app.jobs.scheduler import (
    build_scheduler,
    check_token,
    record_tick,
    run_decisions,
    run_ingest,
    run_poll,
    run_purge,
    run_reconcile,
    wake_decisions,
)
from app.obs.liveness import Liveness, TokenEvidence


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "database_url": "postgresql://localhost/test",
        "gemini_api_key": "test-key",
    }
    return Settings(**(base | overrides))


def test_the_standing_jobs_are_registered() -> None:
    scheduler = build_scheduler(_settings())
    assert {job.id for job in scheduler.get_jobs()} == {
        "poll",
        "purge",
        "token_health",
        "reconcile",
        "decisions",
    }


# --- the decisions job (M16, D1) ---------------------------------------------


def test_the_decisions_job_runs_every_fifteen_seconds_one_at_a_time() -> None:
    """One worker: the queue's safety rests on nothing else resuming a thread."""
    job = next(j for j in build_scheduler(_settings()).get_jobs() if j.id == "decisions")

    assert "0:00:15" in str(job.trigger)
    assert job.max_instances == 1
    assert job.coalesce is True


def _liveness(monkeypatch: pytest.MonkeyPatch) -> Liveness:
    live = Liveness(booted_at=datetime.now(UTC))
    monkeypatch.setattr("app.jobs.scheduler.LIVENESS", live)
    return live


def _no_session(settings: Settings) -> Any:
    raise AssertionError("opened a graph session with nothing to do")


def test_an_empty_queue_costs_one_query_and_no_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every fifteen seconds, the common case must stay cheap."""
    live = _liveness(monkeypatch)
    live.decisions_checked(datetime.now(UTC) - timedelta(minutes=5))
    monkeypatch.setattr("app.jobs.scheduler.open_decisions", lambda settings: (None, False))
    monkeypatch.setattr("app.jobs.scheduler.graph_session", _no_session)
    recorded = _capture(monkeypatch)

    run_decisions(_settings())

    assert recorded == []
    assert live.oldest_open_decision_at is None


def test_an_open_decision_not_yet_due_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    live = _liveness(monkeypatch)
    opened = datetime.now(UTC) - timedelta(minutes=2)
    monkeypatch.setattr("app.jobs.scheduler.open_decisions", lambda settings: (opened, False))
    monkeypatch.setattr("app.jobs.scheduler.graph_session", _no_session)

    run_decisions(_settings())

    assert live.oldest_open_decision_at == opened


def test_a_due_decision_is_applied_and_the_tick_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    live = _liveness(monkeypatch)
    opened = datetime.now(UTC) - timedelta(seconds=30)
    monkeypatch.setattr("app.jobs.scheduler.open_decisions", lambda settings: (opened, True))
    monkeypatch.setattr("app.jobs.scheduler.graph_session", _session)
    calls: list[dict[str, Any]] = []

    def _apply(session: Any, **kwargs: Any) -> list[tuple[str, str]]:
        calls.append(kwargs)
        return [("m1", "skipped")]

    monkeypatch.setattr("app.jobs.scheduler.apply_open", _apply)
    recorded = _capture(monkeypatch)

    run_decisions(_settings())

    # A shutdown stops the worker starting another decision.
    assert calls[0]["stop"] is STOPPING
    assert recorded == [{"job": "decisions", "ok": True, "started": 1, "failed": 0, "error": None}]
    assert live.oldest_open_decision_at == opened


def test_a_decision_that_could_not_be_applied_marks_the_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _liveness(monkeypatch)
    monkeypatch.setattr(
        "app.jobs.scheduler.open_decisions", lambda settings: (datetime.now(UTC), True)
    )
    monkeypatch.setattr("app.jobs.scheduler.graph_session", _session)
    monkeypatch.setattr(
        "app.jobs.scheduler.apply_open",
        lambda session, **kwargs: [("m1", "error"), ("m2", "rejected")],
    )
    recorded = _capture(monkeypatch)

    run_decisions(_settings())

    assert recorded == [
        {
            "job": "decisions",
            "ok": False,
            "started": 2,
            "failed": 1,
            "error": "1 decision(s) could not be applied",
        }
    ]


def test_a_failing_decisions_job_is_recorded_at_most_every_five_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It runs every fifteen seconds; recording each failure would bury job_runs."""
    _liveness(monkeypatch)

    def _explode(settings: Settings) -> Any:
        raise RuntimeError("database unreachable")

    monkeypatch.setattr("app.jobs.scheduler.open_decisions", _explode)
    monkeypatch.setattr("app.jobs.scheduler._decisions_failure_recorded_at", None)
    recorded = _capture(monkeypatch)

    run_decisions(_settings())  # must not raise
    run_decisions(_settings())

    assert recorded == [{"job": "decisions", "ok": False, "error": "RuntimeError"}]


def test_waking_runs_the_decisions_job_now(monkeypatch: pytest.MonkeyPatch) -> None:
    scheduler = build_scheduler(_settings())
    scheduler.start(paused=True)  # nothing actually runs
    try:
        monkeypatch.setattr("app.jobs.scheduler._active", scheduler)

        assert wake_decisions() is True

        job = scheduler.get_job("decisions")
        assert job is not None
        assert job.next_run_time <= datetime.now(UTC) + timedelta(seconds=1)
    finally:
        scheduler.shutdown(wait=False)


def test_waking_without_a_scheduler_in_this_process_does_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI records decisions from another process; the job's next tick finds them."""
    monkeypatch.setattr("app.jobs.scheduler._active", None)
    assert wake_decisions() is False


def test_reconciliation_runs_when_the_scheduler_starts_and_hourly_after() -> None:
    """A parked thread with no row is invisible to the owner, so the first
    pass after a deploy should not wait an hour."""
    before = datetime.now(UTC)
    job = next(j for j in build_scheduler(_settings()).get_jobs() if j.id == "reconcile")

    assert "1:00:00" in str(job.trigger)
    assert before <= job.next_run_time <= datetime.now(UTC)
    assert job.max_instances == 1


def test_a_reconcile_tick_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _capture(monkeypatch)
    monkeypatch.setattr("app.jobs.scheduler.graph_session", _session)
    monkeypatch.setattr(
        "app.jobs.scheduler.reconcile",
        lambda session, **kwargs: ReconcileResult(recorded=2, closed=1, errors=0),
    )

    run_reconcile(_settings())

    assert recorded == [{"job": "reconcile", "ok": True, "error": None}]


def test_a_thread_that_could_not_be_reconciled_marks_the_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _capture(monkeypatch)
    monkeypatch.setattr("app.jobs.scheduler.graph_session", _session)
    monkeypatch.setattr(
        "app.jobs.scheduler.reconcile",
        lambda session, **kwargs: ReconcileResult(recorded=0, closed=0, errors=1),
    )

    run_reconcile(_settings())

    assert recorded == [
        {"job": "reconcile", "ok": False, "error": "1 thread(s) could not be reconciled"}
    ]


def test_a_failing_reconcile_does_not_escape_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _capture(monkeypatch)

    def _explode(settings: Settings) -> Any:
        raise RuntimeError("checkpointer unavailable")

    monkeypatch.setattr("app.jobs.scheduler.graph_session", _explode)

    run_reconcile(_settings())  # must not raise

    assert recorded == [{"job": "reconcile", "ok": False, "error": "RuntimeError"}]


def test_ingestion_is_not_scheduled_unless_asked_for() -> None:
    """The only timed job that spends money without anyone asking for anything."""
    assert "ingest" not in {job.id for job in build_scheduler(_settings()).get_jobs()}


def test_ingestion_is_scheduled_when_enabled() -> None:
    scheduler = build_scheduler(_settings(ingest_enabled=True, ingest_interval_hours=6))
    job = next(j for j in scheduler.get_jobs() if j.id == "ingest")

    assert "6:00:00" in str(job.trigger)
    assert job.max_instances == 1


def test_a_failing_ingest_does_not_escape_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    def _explode(settings: Settings, *, backfill: bool = False) -> Any:
        raise RuntimeError("embeddings unavailable")

    monkeypatch.setattr("app.jobs.ingest_job.run_ingest", _explode)
    monkeypatch.setattr("app.jobs.ingest_job._alert", lambda s, text: sent.append(text))

    scheduled_ingest(_settings())  # must not raise

    assert sent and "embeddings unavailable" in sent[0]


def test_an_ingest_failure_with_no_alert_channel_is_still_survivable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken alert path must not turn a recoverable failure into a crash."""

    def _explode(settings: Settings, *, backfill: bool = False) -> Any:
        raise RuntimeError("embeddings unavailable")

    monkeypatch.setattr("app.jobs.ingest_job.run_ingest", _explode)

    scheduled_ingest(_settings())  # no allowlist, no bot token; must not raise


def test_the_incremental_window_overlaps_the_interval() -> None:
    """A missed run must not leave a permanent hole; dedupe makes overlap free."""
    settings = _settings(ingest_interval_hours=24, ingest_window_days=2)
    query = incremental_query(settings.ingest_window_days)

    assert "newer_than:2d" in query
    assert settings.ingest_window_days * 24 > settings.ingest_interval_hours


def test_polls_do_not_stack_behind_a_slow_run() -> None:
    """Overlapping polls race on the same messages. `claim` makes that safe but
    it is still duplicated work and duplicated LLM spend."""
    job = next(j for j in build_scheduler(_settings()).get_jobs() if j.id == "poll")
    assert job.max_instances == 1
    assert job.coalesce is True


def test_poll_interval_follows_settings() -> None:
    scheduler = build_scheduler(_settings(poll_interval_minutes=3))
    job = next(j for j in scheduler.get_jobs() if j.id == "poll")
    assert "0:03:00" in str(job.trigger)


def test_a_failing_poll_does_not_escape_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """APScheduler would swallow the traceback; the next tick must still fire."""

    def _explode(settings: Settings) -> Any:
        raise RuntimeError("gmail is down")

    monkeypatch.setattr("app.jobs.scheduler.graph_session", _explode)
    _capture(monkeypatch)  # recording is not under test here

    run_poll(_settings())  # must not raise


def test_token_check_survives_an_unreadable_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.jobs.scheduler._count_subscriptions", lambda settings: None)
    check_token(_settings())  # no FERNET_KEY configured; must not raise


def test_the_token_check_runs_hourly() -> None:
    """Hourly rather than twice a day: an alert no channel delivered is
    retried at the next check."""
    before = datetime.now(UTC)
    job = next(j for j in build_scheduler(_settings()).get_jobs() if j.id == "token_health")
    assert "1:00:00" in str(job.trigger)
    # And once at start: a restart must not leave /health without a
    # subscription count, or an alert unsent, for an hour.
    assert before <= job.next_run_time <= datetime.now(UTC)


@dataclass(frozen=True)
class _Store:
    issued_at: datetime
    minted_under: str
    days_remaining: float

    def health(self) -> TokenHealth:
        return TokenHealth(
            issued_at=self.issued_at,
            expires_at=self.issued_at + timedelta(days=7),
            days_remaining=self.days_remaining,
        )

    def metadata(self) -> TokenMetadata:
        return TokenMetadata(issued_at=self.issued_at, minted_under=self.minted_under)  # type: ignore[arg-type]


def _token_check(monkeypatch: pytest.MonkeyPatch, store: _Store) -> list[list[TokenAlert]]:
    sent: list[list[TokenAlert]] = []
    monkeypatch.setattr("app.jobs.scheduler._count_subscriptions", lambda settings: None)
    monkeypatch.setattr("app.jobs.scheduler.token_store", lambda settings: store)
    monkeypatch.setattr("app.jobs.scheduler.standby_token_store", lambda settings: None)
    monkeypatch.setattr("app.jobs.scheduler.TOKEN_EVIDENCE", TokenEvidence())
    monkeypatch.setattr("app.jobs.scheduler.connect", lambda url, **kw: nullcontext(object()))

    def _send(conn: Any, channels: Any, alerts: list[TokenAlert]) -> list[str]:
        sent.append(alerts)
        return []

    monkeypatch.setattr("app.jobs.scheduler.send_token_alerts", _send)
    return sent


def test_a_testing_token_near_expiry_is_alerted_through_the_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issued = datetime.now(UTC) - timedelta(days=6)
    sent = _token_check(monkeypatch, _Store(issued, "testing", days_remaining=1.0))

    check_token(_settings())

    assert sent == [[TokenAlert("token_expiring", issued.isoformat())]]


def test_a_production_token_is_not_counted_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """The countdown belongs to Testing tokens. A production token that dies
    is alerted as expired instead."""
    issued = datetime.now(UTC) - timedelta(days=6)
    sent = _token_check(monkeypatch, _Store(issued, "production", days_remaining=1.0))

    check_token(_settings())

    assert sent == []


def test_the_token_check_refreshes_the_subscription_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = _liveness(monkeypatch)

    class _Conn:
        def execute(self, sql: str) -> Any:
            return type("R", (), {"fetchone": lambda self: (2,)})()

    monkeypatch.setattr("app.jobs.scheduler.connect", lambda url, **kw: nullcontext(_Conn()))
    monkeypatch.setattr("app.jobs.scheduler.token_store", lambda settings: 1 / 0)

    check_token(_settings())

    assert live.push_subscriptions == 2


# --- tick records (M15) ----------------------------------------------------


@contextmanager
def _session(settings: Settings) -> Iterator[object]:
    yield object()


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []

    def _record(settings: Settings, job: str, started_at: Any, **fields: Any) -> None:
        recorded.append({"job": job, **fields})

    monkeypatch.setattr("app.jobs.scheduler.record_tick", _record)
    return recorded


def test_a_clean_poll_is_recorded_as_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _capture(monkeypatch)
    monkeypatch.setattr("app.jobs.scheduler.graph_session", _session)
    monkeypatch.setattr(
        "app.jobs.scheduler.poll_once", lambda *a, **k: PollResult(seen=3, started=2, failed=0)
    )

    run_poll(_settings())

    assert recorded == [
        {"job": "poll", "ok": True, "seen": 3, "started": 2, "failed": 0, "error": None}
    ]


def test_a_poll_that_fails_any_message_is_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise a dead model path passes: failed messages are never offered again,
    so every later tick is empty and would look healthy."""
    recorded = _capture(monkeypatch)
    monkeypatch.setattr("app.jobs.scheduler.graph_session", _session)
    monkeypatch.setattr(
        "app.jobs.scheduler.poll_once", lambda *a, **k: PollResult(seen=3, started=3, failed=1)
    )

    run_poll(_settings())

    assert recorded[0]["ok"] is False
    assert recorded[0]["failed"] == 1


def test_a_raising_poll_is_recorded_with_its_error_type(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _capture(monkeypatch)

    def _explode(settings: Settings) -> Any:
        raise RuntimeError("gmail is down")

    monkeypatch.setattr("app.jobs.scheduler.graph_session", _explode)

    run_poll(_settings())

    assert recorded[0]["ok"] is False
    assert recorded[0]["error"] == "RuntimeError"


def test_an_ingest_tick_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _capture(monkeypatch)
    monkeypatch.setattr("app.jobs.scheduler.scheduled_ingest", lambda settings: False)

    run_ingest(_settings())

    assert recorded == [{"job": "ingest", "ok": False}]


def test_recording_never_escapes_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """A database outage must cost a tick record, not the scheduler."""

    def _no_database(url: str, **kwargs: Any) -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr("app.jobs.scheduler.connect", _no_database)

    record_tick(_settings(), "poll", datetime.now(UTC), ok=True)  # must not raise


def test_the_purge_runs_hourly() -> None:
    job = next(j for j in build_scheduler(_settings()).get_jobs() if j.id == "purge")
    assert "1:00:00" in str(job.trigger)


def test_a_purge_tick_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _capture(monkeypatch)
    monkeypatch.setattr("app.jobs.scheduler.purge_once", lambda settings: None)

    run_purge(_settings())

    assert recorded == [{"job": "purge", "ok": True}]
