"""In-process scheduling.

The poller and the token check run inside the web process rather than as
separate workers. Telegram uses a webhook (no long-poll loop to host) and the
schedule is two jobs a few minutes apart, so a second deployable would buy
nothing and add a second thing to monitor, restart, and pay for.

If throughput ever outgrows this, that is the moment to split it out -- not
before.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler

from app.channel.alerts import send_token_alerts, token_alerts
from app.channel.channels import configured_channels
from app.channel.reconcile import reconcile
from app.channel.worker import apply_open
from app.config import Settings
from app.google.auth import standby_token_store, token_store
from app.graph.runner import graph_session
from app.jobs.ingest_job import scheduled_ingest
from app.jobs.poll import STOPPING, poll_once
from app.jobs.purge import PurgeResult, purge
from app.obs.liveness import LIVENESS, TOKEN_EVIDENCE
from app.obs.token_report import standby_state, token_report
from app.store.db import connect
from app.store.job_runs import JobRuns

log = logging.getLogger(__name__)

RECORD_CONNECT_TIMEOUT = 5
"""Seconds `record_tick` waits for the database before giving up on a record."""

DECISIONS_EVERY = timedelta(seconds=15)
"""How often the worker looks for queued decisions. A decision made in the web
process wakes it at once; this bounds the wait for one made anywhere else."""

DECISION_FAILURES_RECORDED_EVERY = timedelta(minutes=5)
"""The decisions job runs every fifteen seconds, so a failing one is recorded
in `job_runs` at most this often. `/health` reports a stuck queue itself."""

_decisions_failure_recorded_at: datetime | None = None

_active: BackgroundScheduler | None = None
"""The scheduler this process runs, for `wake_decisions`."""


def record_tick(settings: Settings, job: str, started_at: datetime, **fields: Any) -> None:
    """Write one `job_runs` row. Never raises.

    A database outage should cost the record of a tick, not the scheduler:
    the next tick still fires, and the missing row shows up as a gap.
    """
    try:
        # Bounded: an unreachable database must cost seconds of the scheduler
        # thread, not minutes of it.
        with connect(settings.database_url, connect_timeout=RECORD_CONNECT_TIMEOUT) as conn:
            JobRuns(conn).record(job, started_at, datetime.now(UTC), **fields)
    except Exception:
        log.exception("could not record %s tick", job)


def run_poll(settings: Settings) -> None:
    started_at = datetime.now(UTC)
    try:
        with graph_session(settings) as session:
            result = poll_once(
                session,
                settings.poll_batch_size,
                stop=STOPPING,
                show_titles=settings.app_env != "prod",
                announce=configured_channels(settings).announce,
            )
    except Exception as exc:
        # A scheduled job that raises kills nothing but itself, and APScheduler
        # would swallow the traceback. Log it loudly; the next tick retries.
        log.exception("poll failed")
        LIVENESS.poll_finished(ok=False, at=datetime.now(UTC))
        # The type only: exception text can carry message content or URLs.
        record_tick(settings, "poll", started_at, ok=False, error=type(exc).__name__)
        return

    log.info(
        "poll: saw %d unread, started %d, failed %d", result.seen, result.started, result.failed
    )
    LIVENESS.poll_finished(ok=result.failed == 0, at=datetime.now(UTC))
    record_tick(
        settings,
        "poll",
        started_at,
        ok=result.failed == 0,
        seen=result.seen,
        started=result.started,
        failed=result.failed,
        error=None if result.failed == 0 else f"{result.failed} message(s) failed",
    )


def run_ingest(settings: Settings) -> None:
    started_at = datetime.now(UTC)
    record_tick(settings, "ingest", started_at, ok=scheduled_ingest(settings))


def open_decisions(settings: Settings) -> tuple[datetime | None, bool]:
    """When the oldest open decision was made, and whether any is due now."""
    with connect(settings.database_url, connect_timeout=RECORD_CONNECT_TIMEOUT) as conn:
        return decisions_status(conn)


def decisions_status(conn: psycopg.Connection) -> tuple[datetime | None, bool]:
    row = conn.execute(
        """
        SELECT min(decided_at),
               coalesce(bool_or(next_attempt_at <= now()
                                AND (lease_until IS NULL OR lease_until < now())), false)
          FROM decisions
         WHERE outcome IS NULL
        """
    ).fetchone()
    assert row is not None
    return row[0], bool(row[1])


def run_decisions(settings: Settings) -> None:
    """The worker (M16, D1): apply queued decisions. The only thing that resumes a thread."""
    global _decisions_failure_recorded_at
    started_at = datetime.now(UTC)
    try:
        oldest, due = open_decisions(settings)
        LIVENESS.decisions_checked(oldest)
        # Only a due decision is worth a graph session, which loads
        # credentials and builds clients; most ticks find nothing.
        if not due or STOPPING.is_set():
            return
        with graph_session(settings) as session:
            applied = apply_open(
                session, announce=configured_channels(settings).announce, stop=STOPPING
            )
    except Exception as exc:
        log.exception("decisions job failed")
        last = _decisions_failure_recorded_at
        if last is None or started_at - last >= DECISION_FAILURES_RECORDED_EVERY:
            _decisions_failure_recorded_at = started_at
            record_tick(settings, "decisions", started_at, ok=False, error=type(exc).__name__)
        return

    if not applied:
        return
    errors = sum(1 for _, outcome in applied if outcome == "error")
    log.info("decisions: %s", ", ".join(f"{mid} {outcome}" for mid, outcome in applied))
    record_tick(
        settings,
        "decisions",
        started_at,
        ok=errors == 0,
        started=len(applied),
        failed=errors,
        error=None if errors == 0 else f"{errors} decision(s) could not be applied",
    )


def activate(scheduler: BackgroundScheduler) -> None:
    """Start the scheduler, and make it the one `wake_decisions` reaches."""
    global _active
    scheduler.start()
    _active = scheduler


def decision_recorded() -> None:
    """A decision was recorded in this process: start the stuck-queue clock and
    wake the worker, so the owner's tap does not wait for the next tick."""
    LIVENESS.decision_recorded(datetime.now(UTC))
    wake_decisions()


def wake_decisions() -> bool:
    """Run the decisions job now, if this process runs the scheduler.

    Called after a decision is recorded in the web process, so a tap does not
    wait for the next tick. Returns False when there is nothing to wake -- a
    CLI in another process, or a scheduler that is off -- and the next tick
    then finds the decision.
    """
    scheduler = _active
    if scheduler is None or not scheduler.running:
        return False
    try:
        scheduler.modify_job("decisions", next_run_time=datetime.now(UTC))
    except JobLookupError:
        return False
    return True


def run_reconcile(settings: Settings) -> None:
    """D3: record parked threads that have no row, close rows whose message is final."""
    started_at = datetime.now(UTC)
    try:
        with graph_session(settings) as session:
            result = reconcile(session, announce=configured_channels(settings).announce)
    except Exception as exc:
        log.exception("reconcile failed")
        record_tick(settings, "reconcile", started_at, ok=False, error=type(exc).__name__)
        return
    record_tick(
        settings,
        "reconcile",
        started_at,
        ok=result.errors == 0,
        error=None if result.errors == 0 else f"{result.errors} thread(s) could not be reconciled",
    )


def purge_once(settings: Settings) -> PurgeResult:
    with connect(settings.database_url) as conn:
        return purge(conn, settings.database_url)


def run_purge(settings: Settings) -> None:
    started_at = datetime.now(UTC)
    try:
        result = purge_once(settings)
    except Exception as exc:
        log.exception("purge failed")
        record_tick(settings, "purge", started_at, ok=False, error=type(exc).__name__)
        return
    if result is not None:
        log.info(
            "purge: %d thread(s) cleared, %d reason(s) trimmed",
            result.threads,
            result.reasons_cleared,
        )
    record_tick(settings, "purge", started_at, ok=True)


def check_token(settings: Settings) -> None:
    """Hourly: alert on a token state that needs the owner, once per change.

    Google gives no signal before a Testing token expires, and the failure
    mode is silence -- the agent simply stops processing mail. The state is
    read exactly as `/health` reads it, and each alert is recorded only once a
    channel delivered it (`app/channel/alerts.py`). The same pass refreshes
    the push-subscription count `/health` shows, so zero subscriptions is
    visible rather than silent.
    """
    _count_subscriptions(settings)
    try:
        primary = token_store(settings)
        state, health = token_report(primary, TOKEN_EVIDENCE)
        issued_at = primary.metadata().issued_at
        standby = standby_state(standby_token_store(settings), TOKEN_EVIDENCE)
    except Exception:
        log.exception("token health check failed")
        return

    log.info("google token: %s, %.1f days on the Testing clock", state, health.days_remaining)
    alerts = token_alerts(issued_at, state, health, standby=standby)
    if not alerts:
        return
    try:
        with connect(settings.database_url, connect_timeout=RECORD_CONNECT_TIMEOUT) as conn:
            send_token_alerts(conn, configured_channels(settings), alerts)
    except Exception:
        log.exception("could not send token alerts")


def _count_subscriptions(settings: Settings) -> None:
    try:
        with connect(settings.database_url, connect_timeout=RECORD_CONNECT_TIMEOUT) as conn:
            row = conn.execute("SELECT count(*) FROM push_subscriptions").fetchone()
    except Exception:
        log.exception("could not count push subscriptions")
        return
    LIVENESS.subscriptions_counted(int(row[0]) if row else 0)


def build_scheduler(settings: Settings) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")

    scheduler.add_job(
        run_poll,
        "interval",
        minutes=settings.poll_interval_minutes,
        args=[settings],
        id="poll",
        # A slow run must not stack up behind itself: overlapping polls would
        # race on the same messages, and while `claim` makes that safe it also
        # makes it pointless work.
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )

    # Hourly, so a body read by a poll that ended in `skip` is gone within the
    # hour rather than kept for as long as the database lives.
    scheduler.add_job(
        run_purge,
        "interval",
        hours=1,
        args=[settings],
        id="purge",
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        check_token,
        "interval",
        hours=1,
        args=[settings],
        id="token_health",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(UTC),
    )

    # One worker: the queue's safety rests on nothing else resuming a thread,
    # so this job never overlaps itself.
    scheduler.add_job(
        run_decisions,
        "interval",
        seconds=int(DECISIONS_EVERY.total_seconds()),
        args=[settings],
        id="decisions",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=int(DECISIONS_EVERY.total_seconds()),
    )

    # At start, then hourly. A parked thread without a row is invisible to the
    # owner, so the first pass after a deploy should not wait an hour. By the
    # time this runs, boot has already failed stranded claims.
    scheduler.add_job(
        run_reconcile,
        "interval",
        hours=1,
        args=[settings],
        id="reconcile",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(UTC),
    )

    if settings.ingest_enabled:
        scheduler.add_job(
            run_ingest,
            "interval",
            hours=settings.ingest_interval_hours,
            args=[settings],
            id="ingest",
            max_instances=1,
            coalesce=True,
            # No grace period worth speaking of: a missed daily ingest is caught
            # by the next run's overlapping window, so hurrying to catch up
            # after a restart would only race the poller for the same quota.
            misfire_grace_time=3600,
        )

    return scheduler
