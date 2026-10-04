"""Scheduled retrieval ingestion.

    python -m app.jobs.ingest_job              # incremental, the scheduled shape
    python -m app.jobs.ingest_job --backfill   # everything in the window

Two modes over one pipeline. **Incremental** narrows the Gmail query to the last
few days and runs on a timer; **backfill** widens it and is run by hand. They
differ only in query and limit, because M10's content-hash dedupe already makes
re-running free -- the second pass over an overlapping window costs one SELECT
and embeds nothing. Without that property these would need to be separate
pipelines with a watermark between them.

APScheduler, not Airflow. Airflow needs a scheduler, a webserver and its own
metadata database to orchestrate one job that runs once a day over a few dozen
emails; it would take a week and teach the installation rather than the
pipeline. The interesting sentence is "I looked at Airflow and it was wrong for
this scale", not the line on a CV.
"""

from __future__ import annotations

import argparse
import logging

from app.config import Settings, get_settings
from app.obs.redact import error_text
from app.policy.budget import SPENDING_STOPPED
from app.rag.ingest import DEFAULT_QUERY, Stats, ingest

log = logging.getLogger(__name__)


def incremental_query(window_days: int) -> str:
    """The scheduled query: the standing filter, narrowed to recent mail.

    `newer_than` rather than a stored watermark. A cursor would have to be
    updated transactionally with the inserts to be correct, and it buys nothing
    here -- dedupe already makes an overlapping window cheap, and an overlap is
    what keeps a missed run from leaving a permanent hole.
    """
    return f"{DEFAULT_QUERY} newer_than:{window_days}d"


def run_ingest(settings: Settings, *, backfill: bool = False) -> Stats:
    from app.google.auth import build_service, load_credentials
    from app.google.gmail import GmailClient
    from app.policy import control, models
    from app.store.db import connect

    query = DEFAULT_QUERY if backfill else incremental_query(settings.ingest_window_days)
    limit = settings.ingest_backfill_limit if backfill else settings.ingest_limit

    # The spend is recorded on a connection of its own, committed call by
    # call: a run that fails half way has still spent what it spent.
    meter = models.local_gate(settings)
    try:
        if control.is_paused(meter.conn):
            # The owner paused the agent (M17, D6): no batch is claimed, and
            # Gmail is not asked.
            log.info("ingest skipped: the agent is paused")
            return Stats()
        mailbox = GmailClient(build_service("gmail", "v1", load_credentials(settings)))
        client = models.client(settings, meter)
        with connect(settings.database_url) as conn:
            return ingest(
                conn,
                mailbox,
                client,
                settings=settings,
                query=query,
                limit=limit,
                # Before each batch: the spending cap (D5), and a pause (D6).
                may_continue=lambda: meter.allows_new_work() and not control.is_paused(meter.conn),
            )
    finally:
        meter.conn.close()


def scheduled_ingest(settings: Settings) -> bool:
    """The scheduler's entry point. Never raises; alerts on failure.

    Returns whether the run succeeded, for the tick record.
    """
    try:
        stats = run_ingest(settings)
    except SPENDING_STOPPED as exc:
        # The spend gate said no mid-batch (M17, D5): not a failure worth an
        # alert, and `/health` reports a model with no price. A later run's
        # dedupe skips what this one embedded; a stop longer than the window
        # leaves a gap that `--backfill` fills.
        log.info("ingest stopped by the spend gate: %s", type(exc).__name__)
        return True
    except Exception as exc:
        log.exception("ingest failed")
        _alert(
            settings,
            f"⚠️ Retrieval ingestion failed.\n<code>{error_text(exc)}</code>",
        )
        return False

    log.info(
        "ingest: %d messages, %d chunks, %d inserted, ~$%s",
        stats.messages_seen,
        stats.chunks_produced,
        stats.chunks_inserted,
        stats.estimated_cost_usd,
    )
    return True


def _alert(settings: Settings, text: str) -> None:
    """Best-effort Telegram notice. A broken alert channel must not turn a
    recoverable job failure into an unhandled exception in the scheduler."""
    from app.telegram.client import TelegramClient
    from app.telegram.notify import admin_chat_id

    chat_id = admin_chat_id(settings.allowed_chat_ids)
    if chat_id is None or settings.telegram_bot_token is None:
        log.warning("no alert channel configured; message was: %s", text)
        return

    try:
        TelegramClient(settings.telegram_bot_token.get_secret_value()).send_message(chat_id, text)
    except Exception:
        log.exception("could not send ingest failure alert")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run retrieval ingestion once.")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Widen the query to the full standing filter and raise the limit.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    stats = run_ingest(get_settings(), backfill=args.backfill)

    print(f"\n  messages seen      {stats.messages_seen}")
    print(f"  chunks produced    {stats.chunks_produced}")
    print(f"  already stored     {stats.chunks_duplicate}")
    print(f"  inserted           {stats.chunks_inserted}")
    print(f"  estimated cost     ${stats.estimated_cost_usd}")


if __name__ == "__main__":
    main()
