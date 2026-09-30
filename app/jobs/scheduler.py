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

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import Settings
from app.google.auth import token_store
from app.graph.runner import graph_session
from app.jobs.ingest_job import scheduled_ingest
from app.jobs.poll import STOPPING, poll_once
from app.telegram.client import TelegramClient
from app.telegram.notify import admin_chat_id

log = logging.getLogger(__name__)


def run_poll(settings: Settings) -> None:
    try:
        with graph_session(settings) as session:
            seen, started = poll_once(session, settings.poll_batch_size, stop=STOPPING)
        log.info("poll: saw %d unread, started %d", seen, started)
    except Exception:
        # A scheduled job that raises kills nothing but itself, and APScheduler
        # would swallow the traceback. Log it loudly; the next tick retries.
        log.exception("poll failed")


def check_token(settings: Settings) -> None:
    """Warn before the seven-day refresh token expires, not after.

    Google gives no signal that this is coming, and the failure mode is silence
    -- the agent simply stops processing mail. Being told two days early is the
    difference between a known limitation and a week of unexplained downtime.
    """
    try:
        health = token_store(settings).health()
    except Exception:
        log.exception("token health check failed")
        return

    log.info("google token: %.1f days remaining", health.days_remaining)
    if not health.needs_reauth_soon:
        return

    chat_id = admin_chat_id(settings.allowed_chat_ids)
    if chat_id is None or settings.telegram_bot_token is None:
        log.warning(
            "google token expires in %.1f days and no alert channel is configured",
            health.days_remaining,
        )
        return

    try:
        TelegramClient(settings.telegram_bot_token.get_secret_value()).send_message(
            chat_id,
            f"⚠️ Google token expires in {health.days_remaining:.1f} days.\n"
            "Run <code>python -m app.google.reauth</code> or mail stops being processed.",
        )
    except Exception:
        log.exception("could not send token expiry alert")


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

    scheduler.add_job(
        check_token,
        "interval",
        hours=12,
        args=[settings],
        id="token_health",
        max_instances=1,
        coalesce=True,
    )

    if settings.ingest_enabled:
        scheduler.add_job(
            scheduled_ingest,
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
