"""Scheduler configuration and failure isolation."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import SecretStr

from app.channel.reconcile import ReconcileResult
from app.config import Settings
from app.jobs.ingest_job import incremental_query, scheduled_ingest
from app.jobs.poll import PollResult
from app.jobs.scheduler import (
    build_scheduler,
    check_token,
    record_tick,
    run_ingest,
    run_poll,
    run_purge,
    run_reconcile,
)


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
    }


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


def test_token_check_survives_an_unreadable_token() -> None:
    check_token(_settings())  # no FERNET_KEY configured; must not raise


def test_token_warning_needs_an_alert_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no bot token or allowlist there is nowhere to send the warning --
    that must degrade to a log line, not a crash in a background thread."""

    class _Health:
        days_remaining = 1.0
        needs_reauth_soon = True

    monkeypatch.setattr(
        "app.jobs.scheduler.token_store",
        lambda settings: type("S", (), {"health": lambda self: _Health()})(),
    )

    check_token(_settings(allowed_chat_ids=[], telegram_bot_token=None))


def test_token_warning_is_sent_when_a_channel_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    class _Health:
        days_remaining = 1.5
        needs_reauth_soon = True

    monkeypatch.setattr(
        "app.jobs.scheduler.token_store",
        lambda settings: type("S", (), {"health": lambda self: _Health()})(),
    )
    monkeypatch.setattr(
        "app.jobs.scheduler.TelegramClient",
        lambda token: type(
            "C", (), {"send_message": lambda self, chat, text, **kw: sent.append(text)}
        )(),
    )

    check_token(_settings(allowed_chat_ids=[4242], telegram_bot_token=SecretStr("123:abc")))

    assert sent and "1.5 days" in sent[0]


def test_healthy_token_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    class _Health:
        days_remaining = 6.0
        needs_reauth_soon = False

    monkeypatch.setattr(
        "app.jobs.scheduler.token_store",
        lambda settings: type("S", (), {"health": lambda self: _Health()})(),
    )
    monkeypatch.setattr(
        "app.jobs.scheduler.TelegramClient",
        lambda token: type(
            "C", (), {"send_message": lambda self, chat, text, **kw: sent.append(text)}
        )(),
    )

    check_token(_settings(allowed_chat_ids=[4242], telegram_bot_token=SecretStr("123:abc")))

    assert sent == []


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
