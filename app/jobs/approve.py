"""Decide on a proposal parked at `await_approval`.

    python -m app.jobs.approve --list
    python -m app.jobs.approve --message-id 18c0f2a --action confirm
    python -m app.jobs.approve --message-id 18c0f2a --action edit --correction "4pm not 3pm"

This is a *separate process* from the poller on purpose. It is the M05 exit
criterion in CLI form: the run that produced the proposal has exited, and the
decision still lands, because the state lives in Postgres rather than in memory.
Telegram replaces this front-end in M06; the mechanism underneath is identical.
"""

from __future__ import annotations

import argparse

from app.config import get_settings
from app.graph.runner import GraphSession, graph_session
from app.store.ledger import MessageLedger, MessageStatus


def list_pending(session: GraphSession) -> int:
    rows = session.conn.execute(
        "SELECT gmail_message_id FROM processed_messages WHERE status = %s ORDER BY created_at",
        (MessageStatus.AWAITING_APPROVAL.value,),
    ).fetchall()

    for (message_id,) in rows:
        pending = session.pending(message_id)
        if pending is None:
            # Ledger says waiting but no checkpoint is parked -- the two have
            # drifted, which is worth seeing rather than hiding.
            print(f"  {message_id}  (no live interrupt; ledger may be stale)")
            continue
        proposed = pending["proposed"]
        conflicts = pending.get("conflicts") or []
        print(f"  {message_id}  {proposed.get('title')}")
        print(f"      {proposed.get('start_utc')} -> {proposed.get('end_utc')} UTC")
        print(f"      attendees: {', '.join(proposed.get('attendees') or []) or '(none)'}")
        if conflicts:
            print(f"      !! {conflicts[0]}")

    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Approve, edit, or cancel a proposal.")
    parser.add_argument("--list", action="store_true", help="Show proposals awaiting a decision.")
    parser.add_argument("--message-id")
    parser.add_argument("--action", choices=("confirm", "cancel", "edit"), default="confirm")
    parser.add_argument("--correction", default="", help="Free text, used with --action edit.")
    args = parser.parse_args()

    with graph_session(get_settings()) as session:
        if args.list:
            count = list_pending(session)
            print(f"\n{count} awaiting approval.")
            return

        if not args.message_id:
            raise SystemExit("--message-id is required unless --list is given.")

        session.resume(args.message_id, {"action": args.action, "correction": args.correction})

        entry = MessageLedger(session.conn).get(args.message_id)
        if entry is None:
            print("No ledger row.")
        else:
            print(f"{args.message_id}: {entry.status}  event={entry.calendar_event_id or '-'}")


if __name__ == "__main__":
    main()
