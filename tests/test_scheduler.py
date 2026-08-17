"""Scheduler configuration and failure isolation."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.jobs.ingest_job import incremental_query, scheduled_ingest
from app.jobs.scheduler import build_scheduler, check_token, run_poll


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "database_url": "postgresql://localhost/test",
        "gemini_api_key": "test-key",
    }
    return Settings(**(base | overrides))


def test_both_jobs_are_registered() -> None:
    scheduler = build_scheduler(_settings())
    assert {job.id for job in scheduler.get_jobs()} == {"poll", "token_health"}


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
