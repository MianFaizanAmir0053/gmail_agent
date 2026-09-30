"""Poll Gmail and run each unseen message through the graph.

    python -m app.jobs.poll --limit 5      # claim, run until approval is needed
    python -m app.jobs.approve --list      # see what is waiting
    python -m app.jobs.poll --reset        # clear the ledger (dev only)

Runs stop at `await_approval`, which persists a checkpoint and returns. Deciding
is a separate process (`app.jobs.approve`, and Telegram in M06) -- that split is
the point, not an inconvenience.
"""

from __future__ import annotations

import argparse
import threading
from typing import NamedTuple

import psycopg

from app.channel.channels import configured_channels
from app.channel.park import Announce, record_park
from app.config import get_settings
from app.graph.runner import GraphSession, graph_session
from app.store.db import connect
from app.store.ledger import MessageLedger, MessageStatus, SyncCursor

CLAIMED_NOT_RUN = "claimed but graph did not complete"

STOPPING = threading.Event()
"""Set when the process starts shutting down (`app.api.lifespan`).

A pass in progress finishes the message it holds and claims no more. A claim
taken after this point could be killed mid-graph at the platform's kill
timeout, leaving a `claimed` row that nothing ever offers again.
"""


class PollResult(NamedTuple):
    seen: int
    """Unread messages the pass looked at."""

    started: int
    """Messages it claimed and ran through the graph."""

    failed: int
    """Of those, how many failed: dead-lettered as FAILED, or parked without
    their records being written (reconciliation writes those later)."""


def poll_once(
    session: GraphSession,
    limit: int,
    *,
    stop: threading.Event | None = None,
    show_titles: bool = True,
    announce: Announce | None = None,
) -> PollResult:
    """One pass over the newest unread mail.

    `announce` tells the owner about each proposal that parks: every configured
    channel, in production (`app.channel.channels`).

    `show_titles=False` is for production, where this output lands in hosted
    logs that sit outside the database's controls: message ids and statuses
    are enough to trace a run, and an extracted title is mail content.
    """
    ledger = MessageLedger(session.conn)

    message_ids = session.deps.gmail.list_unread(max_results=limit)
    started = 0
    failed = 0
    stopped = False

    for message_id in ledger.unseen(message_ids):
        if stop is not None and stop.is_set():
            stopped = True
            break
        # `unseen` is only a cheap pre-filter -- another run can insert between
        # that query and this one, so `claim` remains the authority.
        if not ledger.claim(message_id, message_id):
            continue
        started += 1

        try:
            session.start(message_id, message_id)
        except Exception as exc:
            # Retries already happened inside the graph. Reaching here means the
            # failure survived them, so dead-letter it: FAILED is deliberately
            # non-terminal, and the message can be re-run once the cause is fixed.
            ledger.mark(message_id, MessageStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
            print(f"  {message_id}  FAILED  {exc}")
            failed += 1
            continue

        pending = session.pending(message_id)
        if pending is None:
            entry = ledger.get(message_id)
            print(f"  {message_id}  {entry.status if entry else '?'}")
        else:
            # The ledger mark and the proposal row, together.
            try:
                record_park(session, message_id, pending, announce=announce)
            except Exception as exc:
                # The thread is still parked, and reconciliation records it.
                # The rest of the batch should not wait an interval for that.
                print(f"  {message_id}  PARKED, NOT RECORDED  {type(exc).__name__}")
                failed += 1
                continue
            proposed = pending["proposed"]
            title = proposed.get("title") if show_titles else ""
            print(f"  {message_id}  AWAITING APPROVAL  {title}".rstrip())

    if not stopped:
        # Left where it was on an early stop: unprocessed mail is still
        # behind it, and moving it forward would skip that mail for good.
        SyncCursor(session.conn).set(session.deps.gmail.current_history_id())
    return PollResult(seen=len(message_ids), started=started, failed=failed)


def reset(conn: psycopg.Connection, *, app_env: str) -> int:
    """Delete every ledger row, and the proposals and decisions hanging off
    them, so messages poll again. Development only.

    Decisions are M24's evidence and refuse cascading deletes, so they are
    removed here explicitly -- which is why production is refused outright.
    """
    if app_env == "prod":
        raise SystemExit(
            "Refusing to reset the production ledger: its decisions are M24's evidence."
        )
    conn.execute("DELETE FROM decisions")
    deleted = conn.execute("DELETE FROM processed_messages").rowcount
    conn.commit()
    return deleted


def main() -> None:
    parser = argparse.ArgumentParser(description="Run unseen Gmail messages through the graph.")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument(
        "--reset", action="store_true", help="Delete every ledger row so messages re-poll."
    )
    args = parser.parse_args()

    settings = get_settings()

    if args.reset:
        with connect(settings.database_url) as conn:
            print(f"Deleted {reset(conn, app_env=settings.app_env)} ledger row(s).")
        return

    with graph_session(settings) as session:
        result = poll_once(session, args.limit, announce=configured_channels(settings).announce)

    print(f"\nSaw {result.seen} unread, started {result.started} new, {result.failed} failed.")
    if result.started == 0 and result.seen:
        print("Nothing new -- idempotency holding.")


if __name__ == "__main__":
    main()
