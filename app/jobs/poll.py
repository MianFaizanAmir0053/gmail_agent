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
from typing import Any

import psycopg

from app.config import get_settings
from app.graph.runner import GraphSession, graph_session
from app.store.db import connect
from app.store.ledger import MessageLedger, MessageStatus, SyncCursor
from app.telegram.client import TelegramClient
from app.telegram.notify import admin_chat_id, send_approval_card

CLAIMED_NOT_RUN = "claimed but graph did not complete"


def poll_once(session: GraphSession, limit: int) -> tuple[int, int]:
    """Returns `(seen, started)`."""
    ledger = MessageLedger(session.conn)

    message_ids = session.deps.gmail.list_unread(max_results=limit)
    started = 0

    for message_id in ledger.unseen(message_ids):
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
            continue

        pending = session.pending(message_id)
        if pending is None:
            entry = ledger.get(message_id)
            print(f"  {message_id}  {entry.status if entry else '?'}")
        else:
            ledger.mark(message_id, MessageStatus.AWAITING_APPROVAL)
            proposed = pending["proposed"]
            print(f"  {message_id}  AWAITING APPROVAL  {proposed.get('title')}")
            _notify(pending, message_id)

    SyncCursor(session.conn).set(session.deps.gmail.current_history_id())
    return len(message_ids), started


def _notify(pending: dict[str, Any], message_id: str) -> None:
    """Push the card to Telegram, if configured.

    Failing to notify must not fail the run: the proposal is already durably
    parked, and `app.jobs.approve --list` can still act on it.
    """
    settings = get_settings()
    chat_id = admin_chat_id(settings.allowed_chat_ids)
    if chat_id is None or settings.telegram_bot_token is None:
        return

    try:
        send_approval_card(
            TelegramClient(settings.telegram_bot_token.get_secret_value()),
            chat_id,
            message_id,
            pending,
            zone=settings.user_timezone,
        )
    except Exception as exc:
        print(f"    (telegram notify failed: {exc})")


def reset(conn: psycopg.Connection) -> int:
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
            print(f"Deleted {reset(conn)} ledger row(s).")
        return

    with graph_session(settings) as session:
        seen, started = poll_once(session, args.limit)

    print(f"\nSaw {seen} unread, started {started} new.")
    if started == 0 and seen:
        print("Nothing new -- idempotency holding.")


if __name__ == "__main__":
    main()
