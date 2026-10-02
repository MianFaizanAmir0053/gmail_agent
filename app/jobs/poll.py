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
from app.google.gmail import MessageGoneError
from app.graph.runner import GraphSession, graph_session
from app.mail import feed
from app.policy import audit, control
from app.policy.budget import SPENDING_STOPPED, MessageTooCostlyError
from app.store.db import connect
from app.store.ledger import MessageLedger, MessageStatus, SyncCursor

CLAIMED_NOT_RUN = "claimed but graph did not complete"

STOPPING = threading.Event()
"""Set when the process starts shutting down (`app.api.lifespan`).

A pass in progress finishes the message it holds and claims no more. A claim
taken after this point could be killed mid-graph at the platform's kill
timeout, leaving a `claimed` row that nothing ever offers again.
"""


HELD_PAUSED = "the agent is paused"
HELD_BY_GATE = "new work is stopped: the spending cap, or a model with no price"


class PollResult(NamedTuple):
    seen: int
    """Messages the pass looked at: the feed's candidates, or before the first
    sync run the newest unread page."""

    started: int
    """Messages it claimed and ran through the graph."""

    failed: int
    """Of those, how many failed: dead-lettered as FAILED, or parked without
    their records being written (reconciliation writes those later)."""
    held: str | None = None
    """Why the pass stopped short: the owner paused the agent, or the spend
    gate stopped new work (M17). Nothing more was claimed; not a failure."""


def poll_once(
    session: GraphSession,
    limit: int,
    *,
    stop: threading.Event | None = None,
    show_titles: bool = True,
    announce: Announce | None = None,
) -> PollResult:
    """One pass over the feed's candidates (M20, D4): new Primary mail, read or
    not. Before the first sync run, over the newest unread mail, as before.

    `announce` tells the owner about each proposal that parks: every configured
    channel, in production (`app.channel.channels`).

    `show_titles=False` is for production, where this output lands in hosted
    logs that sit outside the database's controls: message ids and statuses
    are enough to trace a run, and an extracted title is mail content.
    """
    ledger = MessageLedger(session.conn)
    if control.is_paused(session.conn):
        # The owner paused the agent (M17, D6): nothing is claimed, and mail
        # waits in the feed. Not a failure: the tick records as successful.
        print("  paused: nothing is claimed")
        return PollResult(seen=0, started=0, failed=0, held=HELD_PAUSED)

    candidates, seen, from_feed = _candidates(session, ledger, limit)
    started = 0
    failed = 0
    stopped = False
    held: str | None = None

    for message_id in candidates:
        if stop is not None and stop.is_set():
            stopped = True
            break
        if control.is_paused(session.conn):
            # Paused during the pass: the rest wait (M17, D6).
            stopped, held = True, HELD_PAUSED
            break
        if session.gate is not None and not session.gate.allows_new_work():
            # The spending cap, or a model with no price (M17, D5): the rest
            # wait in the feed until spending is allowed again. Not a
            # failure: the tick succeeds.
            print(f"  {HELD_BY_GATE}: nothing more is claimed")
            stopped, held = True, HELD_BY_GATE
            break
        # The one place a message is claimed. The candidates are only a cheap
        # pre-filter -- another run can insert between that query and this
        # one, so `claim` remains the authority.
        if not ledger.claim(message_id, message_id):
            continue
        started += 1

        try:
            session.start(message_id, message_id)
        except MessageGoneError:
            # Deleted before its turn (M20, D4): a fixed reason, not a failure.
            ledger.mark(message_id, MessageStatus.SKIPPED, error=feed.GONE)
            print(f"  {message_id}  SKIPPED  {feed.GONE}")
            continue
        except SPENDING_STOPPED:
            # Stopped mid-run by the spend gate (M17, D5): released, so it is
            # offered again and run from the start once spending is allowed.
            # It never becomes FAILED.
            session.checkpointer.delete_thread(message_id)
            ledger.release(message_id)
            print(f"  {message_id}  RELEASED  spending stopped")
            stopped, held = True, HELD_BY_GATE
            break
        except MessageTooCostlyError:
            with session.conn.transaction():
                ledger.mark(message_id, MessageStatus.SKIPPED, error=audit.REASONS["too_costly"])
                audit.record(
                    session.conn,
                    "message_too_costly",
                    message_id=message_id,
                    reason=audit.REASONS["too_costly"],
                )
            print(f"  {message_id}  SKIPPED  {audit.REASONS['too_costly']}")
            continue
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

    if not stopped and not from_feed:
        # Left where it was on an early stop: unprocessed mail is still
        # behind it, and moving it forward would skip that mail for good.
        # Once the feed has started, it is where the sync's first run began,
        # and nothing moves it again.
        SyncCursor(session.conn).set(session.deps.gmail.current_history_id())
    return PollResult(seen=seen, started=started, failed=failed, held=held)


def _candidates(
    session: GraphSession, ledger: MessageLedger, limit: int
) -> tuple[list[str], int, bool]:
    """What this pass may claim, how many it looked at, and whether they came
    from the feed (M20, D4).

    Once the first sync run has made a cursor, candidates come from the
    synced rows, and candidates over seven days old are recorded SKIPPED
    first, with no model call. Until then, the newest unread page, as before.
    """
    if feed.active(session.conn):
        too_old = feed.record_too_old(session.conn)
        for message_id in too_old:
            print(f"  {message_id}  SKIPPED  {feed.TOO_OLD}")
        found = feed.candidates(session.conn, limit)
        return found, len(found), True
    message_ids = session.deps.gmail.list_unread(max_results=limit)
    return ledger.unseen(message_ids), len(message_ids), False


def reset(conn: psycopg.Connection, *, app_env: str) -> int:
    """Delete every ledger row, and the proposals and decisions hanging off
    them, so messages poll again. Development only.

    Decisions are M24's evidence and refuse cascading deletes, so they are
    removed here explicitly -- which is why production is refused outright.
    Their approvals (M17) refuse cascades too, and go first.
    """
    if app_env == "prod":
        raise SystemExit(
            "Refusing to reset the production ledger: its decisions are M24's evidence."
        )
    conn.execute("DELETE FROM outbound_actions")
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
    if result.held:
        print(f"Nothing more was claimed: {result.held}.")
    elif result.started == 0 and result.seen:
        print("Nothing new -- idempotency holding.")


if __name__ == "__main__":
    main()
