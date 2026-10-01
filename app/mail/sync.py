"""Mail sync (M20, D3): every relevant message, from Gmail's history.

Run by the scheduler every two minutes, one run at a time, each bounded to
60 seconds; whatever is left waits for the next tick. A run:

1. reads the profile -- the mailbox's address keys its cursor -- and, the
   first time, starts the cursor where the old poller stopped;
2. makes an incremental pass: history pages after the cursor, the cursor
   moved after each page commits, so a crash or a deploy costs one page;
3. once, makes the switch-over listing;
4. lists any catch-up's gap into the fetch queue;
5. works the fetch queue;
6. spends what quota is left on the backfill.

Only a failure of `history.list` itself stops a pass. One message that will
not fetch goes to the queue and the pass moves on; an outage stops the pass
and blames nobody. A `404` from `history.list` -- Gmail no longer keeps the
cursor's history -- starts a catch-up: the gap is recorded and the cursor
moved to the present in one transaction, and the gap is worked off in the
background.

Every run holds a Postgres advisory lock: a scheduled run that finds it
taken skips its turn, and a CLI run waits for it.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, NamedTuple

import psycopg

from app.config import Settings
from app.google.auth import build_service, load_credentials
from app.google.gmail import (
    CursorExpiredError,
    GmailClient,
    HistoryChange,
    HistoryPage,
    MessageGoneError,
    is_outage,
)
from app.mail import quota
from app.mail.messages import (
    D1_QUERY,
    ArrivedVia,
    MessageRow,
    apply_labels,
    classify,
    mark_gone,
    owner_addresses,
    store,
    stored_ids,
)
from app.mail.quota import ShareExhaustedError

log = logging.getLogger(__name__)

SYNC_EVERY = timedelta(minutes=2)
"""How often the scheduler runs the sync."""

RUN_FOR = 60.0
"""Seconds a run may take before it leaves the rest to the next tick."""

LOCK = int.from_bytes(b"mailsync", "big")
"""The advisory lock every sync run holds, scheduled or from the CLI."""

MARGIN = timedelta(hours=1)
"""A catch-up's gap starts this long before the last pass that reached the
end of history: an hour's overlap costs a few fetches, a gap loses mail."""

REFETCH_FOR = timedelta(days=7)
"""A catch-up fetches again every stored row this recent: the feed's age
limit, so nothing trashed during an outage is fed on stale labels."""

WINDOW = timedelta(days=1)
"""A catch-up and the backfill list a day at a time, newest first."""

STRIKES = 5
"""Failures of its own before a queued message is marked unreadable."""

QUEUE_BATCH = 200
"""Queue entries a run reads. Each is tried at most once per run."""

SWITCH_OVER_FOR = timedelta(days=7)
SWITCH_OVER_QUERY = (
    "is:unread -category:promotions -category:social -category:updates -category:forums -in:chats"
)
"""Unread mail outside the four other tabs: what the old poller would still
have reached, had its page of ten not overflowed. Primary is never searched
as `category:primary`, which leaves out mail with no category label."""

QueueReason = Literal["fetch_failed", "label_change", "catch_up", "refetch"]

_ARRIVALS: dict[str, ArrivedVia] = {
    "fetch_failed": "queue",
    "label_change": "queue",
    "catch_up": "catch_up",
    "refetch": "catch_up",
}

_QUEUE_ORDER = """
    CASE reason WHEN 'refetch' THEN 0 WHEN 'label_change' THEN 1
                WHEN 'fetch_failed' THEN 2 ELSE 3 END
"""
"""Re-fetches first: until one answers, the feed holds its row back."""

_BRINGS_IN_WHEN_REMOVED = frozenset({"SPAM", "TRASH", "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL"})


class CursorMovedError(RuntimeError):
    """The cursor was not where this run left it. Someone else moved it; this
    run's page is rolled back rather than written over theirs."""


@dataclass(frozen=True, slots=True)
class Cursor:
    account: str
    history_id: str
    feed_from: datetime
    switch_over_at: datetime | None
    caught_up_at: datetime | None
    backfill_until: datetime
    gap_from: datetime | None
    gap_until: datetime | None
    gap_progress: datetime | None
    catch_ups: int
    created_at: datetime


class Gap(NamedTuple):
    since: datetime
    until: datetime


Stop = Literal["time", "shutdown", "quota", "outage", "error"]


@dataclass
class SyncReport:
    account: str = ""
    records: int = 0
    """History records handled."""

    stored: int = 0
    """Rows inserted."""

    updated: int = 0
    """Rows whose labels changed, or that came back."""

    gone: int = 0
    queued: int = 0
    reached_end: bool = False
    """The pass reached the end of history: what liveness counts."""

    caught_up_at: datetime | None = None
    catch_up: Gap | None = None
    """The gap a catch-up recorded in this run."""

    stopped: Stop | None = None
    """Why the run ended early, if it did."""

    error: str | None = None
    """The exception type that stopped it, for an outage or an error."""

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class _Run:
    conn: psycopg.Connection
    gmail: GmailClient
    owners: frozenset[str]
    started: datetime
    now: Callable[[], datetime]
    deadline: float
    clock: Callable[[], float]
    stop: threading.Event | None
    report: SyncReport
    account: str = ""
    failed: set[str] = field(default_factory=set)
    """Messages that failed on their own in this run: the queue tries them
    again at the next run, not straight away."""

    def halted(self) -> bool:
        """Whether the run must stop here: it already stopped, the process is
        shutting down, or its 60 seconds are up."""
        if self.report.stopped is not None:
            return True
        if self.stop is not None and self.stop.is_set():
            self.report.stopped = "shutdown"
            return True
        if self.clock() >= self.deadline:
            self.report.stopped = "time"
            return True
        return False

    def failure(self, exc: Exception, what: str) -> None:
        """Stop the run for a failure no single message is to blame for."""
        kind: Stop = "outage" if is_outage(exc) else "error"
        log.warning("mail sync stopped at %s: %s", what, type(exc).__name__)
        self.report.stopped = kind
        self.report.error = type(exc).__name__


def sync_client(settings: Settings) -> GmailClient:
    """The sync's Gmail client: every call charged to its share of the quota."""
    return GmailClient(build_service("gmail", "v1", load_credentials(settings)), share=quota.SYNC)


def owners_of(settings: Settings) -> tuple[str, ...]:
    return (settings.owner_email, *settings.owner_aliases)


def run_scheduled(settings: Settings, *, stop: threading.Event | None = None) -> SyncReport | None:
    """The scheduler's tick: one run, or None when another run -- a CLI
    command -- holds the lock, and this one skips its turn.

    The connection autocommits, so each page's transaction is the only one
    open, and the lock it holds is released when it closes.
    """
    with (
        psycopg.connect(settings.database_url, autocommit=True) as conn,
        sync_lock(conn, wait=False) as held,
    ):
        if not held:
            log.info("mail sync: another run holds the lock; skipping this turn")
            return None
        return sync_once(conn, sync_client(settings), owners=owners_of(settings), stop=stop)


def sync_once(
    conn: psycopg.Connection,
    gmail: GmailClient,
    *,
    owners: Iterable[str] = (),
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    clock: Callable[[], float] = time.monotonic,
    run_for: float = RUN_FOR,
    stop: threading.Event | None = None,
) -> SyncReport:
    """One run. The caller holds the advisory lock (`sync_lock`).

    `owners` are `OWNER_EMAIL` and `OWNER_ALIASES`; the mailbox's own address
    is added. `gmail` should be built with the sync's share of the quota.
    """
    report = SyncReport()
    run = _Run(
        conn=conn,
        gmail=gmail,
        owners=frozenset(),
        started=now(),
        now=now,
        deadline=clock() + run_for,
        clock=clock,
        stop=stop,
        report=report,
    )
    profile = gmail.profile()
    run.account = report.account = profile.address
    run.owners = owner_addresses(profile.address, *owners)

    cursor = load_cursor(conn, run.account) or _first_cursor(run, profile.history_id)
    _incremental(run, cursor)
    cursor = _reload(run)
    if cursor.switch_over_at is None and not run.halted():
        _switch_over(run, cursor)
    if cursor.gap_from is not None and not run.halted():
        _list_gap(run, cursor)
    if not run.halted():
        _work_queue(run)
    _close_gap(run)
    return report


# --- the cursor -------------------------------------------------------------

_CURSOR_COLUMNS = """
    account, history_id, feed_from, switch_over_at, caught_up_at, backfill_until,
    gap_from, gap_until, gap_progress, catch_ups, created_at
"""


def load_cursor(conn: psycopg.Connection, account: str) -> Cursor | None:
    row = conn.execute(
        f"SELECT {_CURSOR_COLUMNS} FROM gmail_cursors WHERE account = %s", (account,)
    ).fetchone()
    return Cursor(*row) if row is not None else None


def _reload(run: _Run) -> Cursor:
    cursor = load_cursor(run.conn, run.account)
    assert cursor is not None
    return cursor


def _first_cursor(run: _Run, current_history_id: str) -> Cursor:
    """Start where the old poller stopped (D3).

    It stored the profile's history id after each full pass, so a replay from
    there covers everything since, and `feed_from` is that pass's time. A
    fresh database has no id: the cursor starts at the mailbox's present.
    The backfill's windows end at `feed_from`.
    """
    old = run.conn.execute("SELECT last_history_id, updated_at FROM sync_state WHERE id = 1")
    found = old.fetchone()
    if found is not None and found[0]:
        history_id, feed_from = str(found[0]), found[1]
    else:
        history_id, feed_from = current_history_id, run.started
    run.conn.execute(
        """
        INSERT INTO gmail_cursors (account, history_id, feed_from, backfill_until)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (account) DO NOTHING
        """,
        (run.account, history_id, feed_from, feed_from),
    )
    log.info("mail sync: first run for this mailbox; the feed starts at %s", feed_from)
    return _reload(run)


def _move(
    conn: psycopg.Connection,
    account: str,
    old: str,
    new: str,
    *,
    caught_up_at: datetime | None = None,
) -> None:
    """Move the cursor, but only from where this run found it."""
    moved = conn.execute(
        """
        UPDATE gmail_cursors
           SET history_id = %s, caught_up_at = coalesce(%s, caught_up_at), updated_at = now()
         WHERE account = %s AND history_id = %s
        """,
        (new, caught_up_at, account, old),
    ).rowcount
    if not moved:
        raise CursorMovedError(f"the cursor for {account} is no longer at {old}")


# --- incremental passes -----------------------------------------------------


def _incremental(run: _Run, cursor: Cursor) -> None:
    """History pages after the cursor, until the end, a stop, or a catch-up."""
    start = position = cursor.history_id
    token: str | None = None
    while not run.halted():
        try:
            page = run.gmail.history_page(start, token)
        except CursorExpiredError:
            _catch_up(run, position, cursor)
            return
        except ShareExhaustedError:
            run.report.stopped = "quota"
            return
        except Exception as exc:
            run.failure(exc, "history.list")
            return
        position = _handle_page(run, page, position, last=page.next_page_token is None)
        if page.next_page_token is None or run.report.stopped is not None:
            return
        token = page.next_page_token


_GONE = object()
_FAILED = object()


def _handle_page(run: _Run, page: HistoryPage, position: str, *, last: bool) -> str:
    """Fetch what the page added, then apply it in one transaction.

    The fetches come first, outside the transaction, so no transaction waits
    on Gmail. The page is applied up to the last record whose fetches all
    answered; the cursor moves to that record -- or, at the end of history,
    to the mailbox's current id. Returns the cursor's new position.
    """
    deleted = {
        change.message_id
        for record in page.records
        for change in record.changes
        if change.kind == "deleted"
    }
    fetched: dict[str, object] = {}
    handled = 0
    for record in page.records:
        if not _fetch_additions(run, record.changes, deleted, fetched):
            break
        handled += 1

    complete = handled == len(page.records)
    new_position = position
    caught_up_at = None
    if complete and last:
        new_position, caught_up_at = page.history_id, run.now()
    elif handled:
        new_position = page.records[handled - 1].id

    with run.conn.transaction():
        for record in page.records[:handled]:
            for change in record.changes:
                _apply(run, change, deleted, fetched)
        if new_position != position or caught_up_at is not None:
            _move(run.conn, run.account, position, new_position, caught_up_at=caught_up_at)
    run.report.records += handled
    if caught_up_at is not None:
        run.report.reached_end = True
        run.report.caught_up_at = caught_up_at
    return new_position


def _fetch_additions(
    run: _Run, changes: tuple[HistoryChange, ...], deleted: set[str], fetched: dict[str, object]
) -> bool:
    """Fetch a record's added messages. False if the run must stop first.

    A message added and deleted in the same page is never fetched: drafts are
    replaced constantly, and each would cost a fetch and a 404.
    """
    for change in changes:
        message_id = change.message_id
        if change.kind != "added" or message_id in deleted or message_id in fetched:
            continue
        if run.halted():
            return False
        try:
            fetched[message_id] = classify(run.gmail.message_metadata(message_id), run.owners)
        except MessageGoneError:
            fetched[message_id] = _GONE
        except ShareExhaustedError:
            run.report.stopped = "quota"
            return False
        except Exception as exc:
            if is_outage(exc):
                run.failure(exc, "messages.get")
                return False
            log.warning(
                "mail sync: %s failed to fetch (%s); queued", message_id, type(exc).__name__
            )
            fetched[message_id] = _FAILED
    return True


def _apply(run: _Run, change: HistoryChange, deleted: set[str], fetched: dict[str, object]) -> None:
    conn, account, report = run.conn, run.account, run.report
    message_id = change.message_id
    if change.kind == "added":
        outcome = fetched.get(message_id)
        if isinstance(outcome, MessageRow):
            _count(report, store(conn, account, outcome, "history"))
        elif outcome is _GONE:
            report.gone += mark_gone(conn, account, message_id)
            dequeue(conn, message_id)
        elif outcome is _FAILED:
            run.failed.add(message_id)
            report.queued += enqueue(conn, message_id, "fetch_failed", strikes=1)
    elif change.kind == "deleted":
        report.gone += mark_gone(conn, account, message_id)
        dequeue(conn, message_id)
    else:
        added = change.label_ids if change.kind == "labels_added" else frozenset()
        removed = change.label_ids if change.kind == "labels_removed" else frozenset()
        result = apply_labels(conn, account, message_id, added=added, removed=removed)
        if result == "changed":
            report.updated += 1
        # Fetched in this page, its labels are already current.
        elif (
            result == "absent"
            and message_id not in deleted
            and message_id not in fetched
            and could_bring_in(change)
        ):
            report.queued += enqueue(conn, message_id, "label_change")


def could_bring_in(change: HistoryChange) -> bool:
    """Whether a label change could make D1 store a message it had not: out
    of Spam, Trash, Promotions or Social, or newly labelled SENT."""
    if change.kind == "labels_removed":
        return bool(change.label_ids & _BRINGS_IN_WHEN_REMOVED)
    return change.kind == "labels_added" and "SENT" in change.label_ids


def _count(report: SyncReport, outcome: str) -> None:
    if outcome == "inserted":
        report.stored += 1
    elif outcome == "updated":
        report.updated += 1


# --- the fetch queue --------------------------------------------------------


def enqueue(
    conn: psycopg.Connection, message_id: str, reason: QueueReason, *, strikes: int = 0
) -> bool:
    """Queue a message to fetch outside the pass. An entry already there,
    queued or unreadable, is left as it is."""
    return bool(
        conn.execute(
            """
            INSERT INTO gmail_fetch_queue (message_id, reason, strikes) VALUES (%s, %s, %s)
            ON CONFLICT (message_id) DO NOTHING
            """,
            (message_id, reason, strikes),
        ).rowcount
    )


def dequeue(conn: psycopg.Connection, message_id: str) -> None:
    conn.execute("DELETE FROM gmail_fetch_queue WHERE message_id = %s", (message_id,))


def _strike(conn: psycopg.Connection, message_id: str) -> None:
    conn.execute(
        """
        UPDATE gmail_fetch_queue
           SET strikes = strikes + 1,
               status = CASE WHEN strikes + 1 >= %s THEN 'unreadable' ELSE status END
         WHERE message_id = %s
        """,
        (STRIKES, message_id),
    )


def _work_queue(run: _Run) -> None:
    """Fetch what the queue holds, re-fetches first, each at most once a run.

    A failure of the message's own counts a strike, and five make it
    unreadable. An outage stops the queue and counts nothing.
    """
    conn = run.conn
    rows = conn.execute(
        f"""
        SELECT message_id, reason FROM gmail_fetch_queue
         WHERE status = 'queued'
         ORDER BY {_QUEUE_ORDER}, queued_at, message_id
         LIMIT %s
        """,
        (QUEUE_BATCH,),
    ).fetchall()
    for message_id, reason in rows:
        if message_id in run.failed:
            continue
        if run.halted():
            return
        try:
            row = classify(run.gmail.message_metadata(message_id), run.owners)
        except MessageGoneError:
            with conn.transaction():
                run.report.gone += mark_gone(conn, run.account, message_id)
                dequeue(conn, message_id)
            continue
        except ShareExhaustedError:
            run.report.stopped = "quota"
            return
        except Exception as exc:
            if is_outage(exc):
                run.failure(exc, "the fetch queue")
                return
            log.warning("mail sync: queued %s failed again (%s)", message_id, type(exc).__name__)
            run.failed.add(message_id)
            _strike(conn, message_id)
            continue
        with conn.transaction():
            _count(run.report, store(conn, run.account, row, _ARRIVALS[reason]))
            dequeue(conn, message_id)


# --- the switch-over ----------------------------------------------------------


def _switch_over(run: _Run, cursor: Cursor) -> None:
    """Once: unread mail outside the four other tabs, from the last seven days.

    Done when every listed message not yet stored has been fetched; a run
    that stops part-way lists again next time, and skips what it stored.
    """
    try:
        ids = run.gmail.message_ids(
            SWITCH_OVER_QUERY, after=run.started - SWITCH_OVER_FOR, before=run.started
        )
    except ShareExhaustedError:
        run.report.stopped = "quota"
        return
    except Exception as exc:
        run.failure(exc, "the switch-over listing")
        return
    have = stored_ids(run.conn, run.account, ids)
    for message_id in ids:
        if message_id in have:
            continue
        if not _fetch_and_store(run, message_id, "switch_over"):
            return
    run.conn.execute(
        """
        UPDATE gmail_cursors SET switch_over_at = %s, updated_at = now()
         WHERE account = %s AND switch_over_at IS NULL
        """,
        (run.now(), run.account),
    )
    log.info("mail sync: switch-over listing done, %d message(s) listed", len(ids))


def _fetch_and_store(
    run: _Run, message_id: str, arrived_via: ArrivedVia, *, before: datetime | None = None
) -> bool:
    """Fetch and store one listed message. False if the run must stop.

    A 404 passes it over, and a failure of its own queues it. `before` stores
    nothing at or after it: the backfill's limit.
    """
    if run.halted():
        return False
    try:
        row = classify(run.gmail.message_metadata(message_id), run.owners)
    except MessageGoneError:
        return True
    except ShareExhaustedError:
        run.report.stopped = "quota"
        return False
    except Exception as exc:
        if is_outage(exc):
            run.failure(exc, "messages.get")
            return False
        run.failed.add(message_id)
        run.report.queued += enqueue(run.conn, message_id, "fetch_failed", strikes=1)
        return True
    if before is None or row.internal_at < before:
        _count(run.report, store(run.conn, run.account, row, arrived_via))
    return True


# --- catching up --------------------------------------------------------------


def _catch_up(run: _Run, position: str, cursor: Cursor) -> None:
    """Gmail no longer keeps the cursor's history (D3).

    The profile's id is read first, then -- in one transaction -- the gap is
    recorded, from an hour before the last pass that reached the end to now,
    and the cursor moved to that id, so live mail flows again at the next
    pass. Every stored row from the last seven days is queued to be fetched
    again: mail trashed or moved to spam during the outage is not listed,
    and this is where it is seen, before the feed could process it.
    """
    try:
        profile = run.gmail.profile()
    except ShareExhaustedError:
        run.report.stopped = "quota"
        return
    except Exception as exc:
        run.failure(exc, "the catch-up's profile")
        return
    recorded = record_gap(
        run.conn,
        run.account,
        old_history_id=position,
        new_history_id=profile.history_id,
        since=(cursor.caught_up_at or cursor.feed_from) - MARGIN,
        until=run.now(),
    )
    run.report.catch_up = recorded
    log.warning(
        "mail sync: history expired; catching up from %s to %s", recorded.since, recorded.until
    )


def record_gap(
    conn: psycopg.Connection,
    account: str,
    *,
    old_history_id: str,
    new_history_id: str,
    since: datetime,
    until: datetime,
) -> Gap:
    """Record a gap and move the cursor past it, in one transaction.

    A gap still being worked off is widened rather than replaced: its listing
    starts again from the new end, and what was already fetched is fetched
    again, which costs quota but loses nothing.
    """
    with conn.transaction():
        found = conn.execute(
            """
            UPDATE gmail_cursors
               SET gap_from = LEAST(coalesce(gap_from, %(since)s), %(since)s),
                   gap_until = %(until)s,
                   gap_progress = NULL,
                   history_id = %(new)s,
                   catch_ups = catch_ups + 1,
                   updated_at = now()
             WHERE account = %(account)s AND history_id = %(old)s
            RETURNING gap_from, gap_until
            """,
            {
                "since": since,
                "until": until,
                "new": new_history_id,
                "account": account,
                "old": old_history_id,
            },
        ).fetchone()
        if found is None:
            raise CursorMovedError(f"the cursor for {account} is no longer at {old_history_id}")
        conn.execute(
            """
            INSERT INTO gmail_fetch_queue (message_id, reason)
            SELECT message_id, 'refetch' FROM gmail_messages
             WHERE account = %s AND gone_at IS NULL AND internal_at >= %s
            ON CONFLICT (message_id) DO NOTHING
            """,
            (account, until - REFETCH_FOR),
        )
    return Gap(since=found[0], until=found[1])


def _list_gap(run: _Run, cursor: Cursor) -> None:
    """List the gap a day at a time, newest first, into the fetch queue.

    Listing is cheap and the queue works at the quota's pace, so a day with
    more mail than one run can fetch still moves on. `gap_progress` records
    how far back the listing has reached; a restart resumes from it.
    """
    assert cursor.gap_from is not None and cursor.gap_until is not None
    progress = cursor.gap_progress or cursor.gap_until
    while progress > cursor.gap_from and not run.halted():
        start = max(cursor.gap_from, progress - WINDOW)
        try:
            ids = run.gmail.message_ids(D1_QUERY, after=start, before=progress)
        except ShareExhaustedError:
            run.report.stopped = "quota"
            return
        except Exception as exc:
            run.failure(exc, "the catch-up's listing")
            return
        with run.conn.transaction():
            for message_id in ids:
                run.report.queued += enqueue(run.conn, message_id, "catch_up")
            moved = run.conn.execute(
                """
                UPDATE gmail_cursors SET gap_progress = %s, updated_at = now()
                 WHERE account = %s AND gap_until = %s
                   AND gap_progress IS NOT DISTINCT FROM %s
                """,
                (start, run.account, cursor.gap_until, cursor.gap_progress),
            ).rowcount
            if not moved:
                raise CursorMovedError(f"the gap for {run.account} changed under this run")
        cursor = _reload(run)
        progress = start


def _close_gap(run: _Run) -> None:
    """Clear the gap once its listing is done and the queue holds none of its
    work: the listed messages and the re-fetches."""
    closed = run.conn.execute(
        """
        UPDATE gmail_cursors
           SET gap_from = NULL, gap_until = NULL, gap_progress = NULL, updated_at = now()
         WHERE account = %s AND gap_from IS NOT NULL
           AND coalesce(gap_progress, gap_until) <= gap_from
           AND NOT EXISTS (SELECT 1 FROM gmail_fetch_queue
                            WHERE status = 'queued' AND reason IN ('catch_up', 'refetch'))
        """,
        (run.account,),
    ).rowcount
    if closed:
        log.info("mail sync: the catch-up is done")


# --- the lock -----------------------------------------------------------------


@contextmanager
def sync_lock(conn: psycopg.Connection, *, wait: bool) -> Iterator[bool]:
    """Hold the sync's advisory lock for the block.

    The scheduled job does not wait (`wait=False`): it is told False at once,
    and skips its turn. The CLI waits for the scheduled run to finish.
    """
    if wait:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK,))
        held = True
    else:
        row = conn.execute("SELECT pg_try_advisory_lock(%s)", (LOCK,)).fetchone()
        held = bool(row and row[0])
    try:
        yield held
    finally:
        if held:
            try:
                conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK,))
            except psycopg.Error:
                # Closing the connection releases it all the same.
                log.warning("could not release the mail sync's lock; the connection will")
