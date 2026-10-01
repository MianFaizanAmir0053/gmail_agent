"""Decide on a proposal from the command line.

    python -m app.jobs.approve --list
    python -m app.jobs.approve --message-id 18c0f2a --action confirm
    python -m app.jobs.approve --message-id 18c0f2a --action edit --correction "4pm not 3pm"
    python -m app.jobs.approve --sweep-all        # end observe mode (M15)
    python -m app.jobs.approve --reconcile        # repair missing proposal rows now (M16)

Since M16 this records decisions and waits for them; it never resumes a thread
itself. The worker in the app's scheduler is the only thing that does
(`docs/plans/M16-web-channel.md`, D1), so a decision made here is applied at
its next tick, within fifteen seconds. With no app running, nothing applies
it, and the wait says so rather than hanging.

It is still a *separate process* from the app on purpose: the M05 exit
criterion in CLI form. The decision lands because the state lives in Postgres,
not in any process's memory.
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable

import psycopg

from app.channel.channels import configured_channels
from app.channel.decide import Action, DecisionResult, card_token, decide
from app.channel.park import Announce
from app.channel.reconcile import reconcile
from app.config import get_settings
from app.graph.runner import GraphSession, graph_session
from app.store.db import connect_autocommit

WAIT_SECONDS = 180
"""Long enough for an edit's re-extraction and review, with a retry."""

POLL_EVERY = 2.0


def current_revision(conn: psycopg.Connection, message_id: str) -> int | None:
    row = conn.execute(
        "SELECT revision FROM proposals WHERE message_id = %s", (message_id,)
    ).fetchone()
    return None if row is None else int(row[0])


def decide_current(
    conn: psycopg.Connection,
    message_id: str,
    *,
    action: Action,
    correction: str = "",
    expect: str | None = None,
    dry_run: bool | None = None,
) -> DecisionResult:
    """Record a decision on the revision the proposal is showing now.

    The operator names a message, not a revision; the revision read here is
    what `decide()` then holds the claim to, so a proposal that moves in the
    meantime is refused as stale rather than decided blind.

    A Confirm also needs `expect`: the token `--list` printed for the proposal
    the operator read (M17, D2). It binds the hash, the mode and the
    generation they saw, exactly as a card's Confirm does.
    """
    revision = current_revision(conn, message_id)
    if revision is None:
        return DecisionResult("not_found", detail="no proposal for this message")
    return decide(
        conn,
        message_id,
        action=action,
        revision=revision,
        correction=correction,
        via="cli",
        token=expect,
        dry_run=dry_run,
    )


def sweep_all(conn: psycopg.Connection) -> list[int]:
    """Queue a sweep for every pending proposal. Returns the decisions queued.

    A proposal already being decided keeps that decision.
    """
    rows = conn.execute(
        "SELECT message_id, revision FROM proposals WHERE status = 'pending' ORDER BY parked_at"
    ).fetchall()
    queued: list[int] = []
    for message_id, revision in rows:
        result = decide(conn, message_id, action="sweep", revision=revision, via="sweep")
        if result.status == "queued" and result.decision_id is not None:
            queued.append(result.decision_id)
    return queued


def outcome_of(conn: psycopg.Connection, decision_id: int) -> str | None:
    row = conn.execute("SELECT outcome FROM decisions WHERE id = %s", (decision_id,)).fetchone()
    return None if row is None else row[0]


def still_open(conn: psycopg.Connection, decision_ids: list[int]) -> int:
    row = conn.execute(
        "SELECT count(*) FROM decisions WHERE id = ANY(%s) AND outcome IS NULL",
        (decision_ids,),
    ).fetchone()
    return 0 if row is None else int(row[0])


def wait_for(
    read: Callable[[], str | None],
    *,
    timeout: float,
    poll_every: float = POLL_EVERY,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str | None:
    """Poll `read` until it answers or `timeout` seconds pass. None on timeout."""
    deadline = clock() + timeout
    while True:
        answer = read()
        if answer is not None:
            return answer
        if clock() >= deadline:
            return None
        sleep(poll_every)


def list_pending(conn: psycopg.Connection) -> int:
    """Print the proposals waiting for a decision or being decided."""
    rows = conn.execute(
        """
        SELECT message_id, revision, status, dry_run, payload, args_hash, generation
          FROM proposals
         WHERE status IN ('pending', 'deciding')
         ORDER BY parked_at
        """
    ).fetchall()

    for message_id, revision, status, dry_run, payload, args_hash, generation in rows:
        card = payload or {}
        conflicts = card.get("conflicts") or []
        # The token a Confirm must name with --expect: what the operator read.
        token = card_token(args_hash, dry_run, generation) if args_hash else "not ready"
        print(f"  {message_id}  r{revision}  {status}  {token}  {card.get('title')}")
        print(f"      {card.get('start_utc')} -> {card.get('end_utc')} UTC")
        print(f"      attendees: {', '.join(card.get('attendees') or []) or '(none)'}")
        print(f"      dry_run when proposed: {dry_run}")
        if conflicts:
            print(f"      !! {conflicts[0]}")
    return len(rows)


def reconcile_now(session: GraphSession, *, announce: Announce | None = None) -> str:
    """Run reconciliation (M16, D3) at once, rather than at the next hourly pass."""
    result = reconcile(session, announce=announce)
    return (
        f"Recorded {result.recorded} parked thread(s); closed {result.closed} row(s); "
        f"{result.errors} error(s)."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Approve, edit, or cancel a proposal.")
    parser.add_argument("--list", action="store_true", help="Show proposals awaiting a decision.")
    parser.add_argument(
        "--sweep-all", action="store_true", help="End every pending proposal as swept."
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="Record parked threads that have no proposal row, and close rows whose "
        "message is final.",
    )
    parser.add_argument("--message-id")
    parser.add_argument(
        "--action", choices=("confirm", "cancel", "edit", "sweep"), default="confirm"
    )
    parser.add_argument("--correction", default="", help="Free text, used with --action edit.")
    parser.add_argument(
        "--expect",
        help="With --action confirm: the token --list printed, e.g. 3f9a1c07be42-dry-1.",
    )
    parser.add_argument(
        "--wait",
        type=int,
        default=WAIT_SECONDS,
        help="Seconds to wait for the worker to apply the decision.",
    )
    args = parser.parse_args()
    settings = get_settings()

    if args.reconcile or args.sweep_all:
        # A sweep must reach every parked thread, including one whose row is
        # missing, so reconciliation runs first.
        with graph_session(settings) as session:
            print(reconcile_now(session, announce=configured_channels(settings).announce))
        if args.reconcile:
            return

    # Autocommit: the worker, in another process, must see each decision the
    # moment it is recorded, while this one waits for the outcome.
    with connect_autocommit(settings.database_url) as conn:
        if args.list:
            print(f"\n{list_pending(conn)} awaiting or being decided.")
            return

        if args.sweep_all:
            queued = sweep_all(conn)
            print(f"Queued {len(queued)} sweep(s).")
            remaining = wait_for(
                lambda: "done" if still_open(conn, queued) == 0 else None, timeout=args.wait
            )
            if remaining is None:
                print(f"{still_open(conn, queued)} still open after {args.wait}s.")
            return

        if not args.message_id:
            raise SystemExit("--message-id is required unless --list or --sweep-all is given.")

        result = decide_current(
            conn,
            args.message_id,
            action=args.action,
            correction=args.correction,
            expect=args.expect,
            dry_run=settings.dry_run,
        )
        if result.status != "queued" or result.decision_id is None:
            raise SystemExit(f"{args.message_id}: {result.status}: {result.detail}")

        decision_id = result.decision_id
        outcome = wait_for(lambda: outcome_of(conn, decision_id), timeout=args.wait)
        if outcome is None:
            print(
                f"{args.message_id}: queued, but not applied within {args.wait}s. "
                "Is the app running? The worker applies it at its next tick."
            )
        else:
            print(f"{args.message_id}: {outcome}")


if __name__ == "__main__":
    main()
