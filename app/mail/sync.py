"""Mail sync (M20, D3): every relevant message, from Gmail's history.

    python -m app.mail.sync --once            # one run, as the scheduler does
    python -m app.mail.sync --status          # the records, as counts and times
    python -m app.mail.sync --show <id>       # one row's metadata, no content
    python -m app.mail.sync --catch-up        # as if the cursor had expired
    python -m app.mail.sync --check-feed      # the exit criterion's feed checks

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

import argparse
import logging
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, NamedTuple

import psycopg

from app.config import Settings, get_settings
from app.google.auth import build_service, load_credentials
from app.google.gmail import (
    CursorExpiredError,
    GmailClient,
    HistoryChange,
    HistoryPage,
    MessageGoneError,
    is_outage,
)
from app.mail import feed, quota
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

BACKFILL_FOR = timedelta(days=90)
"""How far back from `feed_from` the backfill reaches. The fetch queue drops,
rather than stores, a message older than the backfill's floor: a label
change can touch years-old mail, and only what the backfill reaches matters."""

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

    status: dict[str, Any] | None = None
    """The records as the run left them, for `/health` (`status`)."""

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
        report = sync_once(conn, sync_client(settings), owners=owners_of(settings), stop=stop)
        report.status = status(conn, report.account)
        return report


def status(conn: psycopg.Connection, account: str) -> dict[str, Any] | None:
    """The sync's records, as counts and times only: what `/health` shows the
    owner (D6), and where `--status` starts. None before the first run."""
    cursor = load_cursor(conn, account)
    if cursor is None:
        return None
    counts = conn.execute(
        """
        SELECT (SELECT count(*) FROM gmail_messages WHERE account = %(account)s),
               (SELECT count(*) FROM gmail_fetch_queue WHERE status = 'queued'),
               (SELECT count(*) FROM gmail_fetch_queue WHERE status = 'unreadable'),
               (SELECT count(*) FROM processed_messages
                 WHERE status = 'skipped' AND error = %(old)s)
        """,
        {"account": account, "old": feed.TOO_OLD},
    ).fetchone()
    assert counts is not None
    rows, queued, unreadable, too_old = (int(count) for count in counts)

    def when(at: datetime | None) -> str | None:
        return at.isoformat() if at is not None else None

    return {
        "caught_up_at": when(cursor.caught_up_at),
        "feed_from": when(cursor.feed_from),
        "switch_over_at": when(cursor.switch_over_at),
        "backfill": {
            "until": when(cursor.backfill_until),
            "done": cursor.backfill_until <= cursor.feed_from - BACKFILL_FOR,
        },
        "catch_up": None
        if cursor.gap_from is None
        else {
            "from": when(cursor.gap_from),
            "until": when(cursor.gap_until),
            "listed_back_to": when(cursor.gap_progress),
        },
        "catch_ups": cursor.catch_ups,
        "queue": {"queued": queued, "unreadable": unreadable},
        "too_old": too_old,
        "rows": rows,
    }


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
        _work_queue(run, floor=cursor.feed_from - BACKFILL_FOR)
    _close_gap(run)
    if not run.halted():
        _backfill(run, _reload(run))
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
        outcome = _fetch(run, message_id, "messages.get")
        if outcome is None:
            return False
        fetched[message_id] = outcome
    return True


def _fetch(run: _Run, message_id: str, what: str) -> object:
    """One message's row, `_GONE` for a 404, `_FAILED` for a failure of its
    own, or None when the run must stop.

    A failure that looks like an outage -- a 5xx or a timeout that outlasted
    the client's retries -- is checked with one cheap call, the profile (a
    unit). If Gmail answers that, the failure is the message's own: it is
    queued and struck like any other, for one message that answers 500 every
    time would otherwise stop every pass on it, and with it the cursor, the
    queue, the backfill and any catch-up. Only if the probe fails too is it an
    outage, which stops the run and blames nobody.
    """
    try:
        return classify(run.gmail.message_metadata(message_id), run.owners)
    except MessageGoneError:
        return _GONE
    except ShareExhaustedError:
        run.report.stopped = "quota"
        return None
    except Exception as exc:
        if is_outage(exc) and not _gmail_answers(run, exc, what):
            return None
        log.warning("mail sync: %s failed to fetch (%s); queued", message_id, type(exc).__name__)
        return _FAILED


def _gmail_answers(run: _Run, exc: Exception, what: str) -> bool:
    """The probe after a fetch failed as if Gmail were down. False, with the
    run stopped, when it is: the probe failed too, or the share is spent."""
    try:
        run.gmail.profile()
    except ShareExhaustedError:
        run.report.stopped = "quota"
        return False
    except Exception:
        run.failure(exc, what)
        return False
    return True


def _apply(run: _Run, change: HistoryChange, deleted: set[str], fetched: dict[str, object]) -> None:
    conn, account, report = run.conn, run.account, run.report
    message_id = change.message_id
    if change.kind == "added":
        outcome = fetched.get(message_id)
        if isinstance(outcome, MessageRow):
            if not _stored(run, outcome, "history"):
                _failed_on_its_own(run, message_id)
        elif outcome is _GONE:
            report.gone += mark_gone(conn, account, message_id)
            dequeue(conn, message_id)
        elif outcome is _FAILED:
            _failed_on_its_own(run, message_id)
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


def _stored(run: _Run, row: MessageRow, arrived_via: ArrivedVia) -> bool:
    """Store one row in a savepoint of its own. False if the database refused
    it: that is the message's own failure, and must not roll back the rest of
    its page -- the next run would replay the same page for ever."""
    try:
        with run.conn.transaction():
            _count(run.report, store(run.conn, run.account, row, arrived_via))
    except psycopg.DatabaseError as exc:
        log.warning("mail sync: %s could not be stored (%s)", row.message_id, type(exc).__name__)
        return False
    return True


def _failed_on_its_own(run: _Run, message_id: str) -> None:
    """Queue a message that failed on its own, with its first strike. The
    queue tries it again at the next run, not straight away."""
    run.failed.add(message_id)
    run.report.queued += enqueue(run.conn, message_id, "fetch_failed", strikes=1)


# --- the fetch queue --------------------------------------------------------


def enqueue(
    conn: psycopg.Connection, message_id: str, reason: QueueReason, *, strikes: int = 0
) -> bool:
    """Queue a message to fetch outside the pass. An entry already there,
    queued or unreadable, is left as it is. One queued with a strike failed
    just now."""
    return bool(
        conn.execute(
            """
            INSERT INTO gmail_fetch_queue (message_id, reason, strikes, failed_at)
            VALUES (%(id)s, %(reason)s, %(strikes)s, CASE WHEN %(strikes)s > 0 THEN now() END)
            ON CONFLICT (message_id) DO NOTHING
            """,
            {"id": message_id, "reason": reason, "strikes": strikes},
        ).rowcount
    )


def dequeue(conn: psycopg.Connection, message_id: str) -> None:
    conn.execute("DELETE FROM gmail_fetch_queue WHERE message_id = %s", (message_id,))


def _strike(conn: psycopg.Connection, message_id: str) -> None:
    conn.execute(
        """
        UPDATE gmail_fetch_queue
           SET strikes = strikes + 1,
               status = CASE WHEN strikes + 1 >= %s THEN 'unreadable' ELSE status END,
               failed_at = now()
         WHERE message_id = %s
        """,
        (STRIKES, message_id),
    )


def _work_queue(run: _Run, *, floor: datetime) -> None:
    """Fetch what the queue holds, each at most once a run.

    Entries that never failed come first, re-fetches first among them; then
    those that failed, fewest strikes and longest ago first, so a head that
    fails every time cannot starve the rest. A failure of the message's own
    counts a strike, and five make it unreadable. An outage stops the queue
    and counts nothing. A message older than `floor`, where the backfill
    stops, is dropped rather than stored.
    """
    conn = run.conn
    rows = conn.execute(
        f"""
        SELECT message_id, reason FROM gmail_fetch_queue
         WHERE status = 'queued'
         ORDER BY strikes, failed_at NULLS FIRST, {_QUEUE_ORDER}, queued_at, message_id
         LIMIT %s
        """,
        (QUEUE_BATCH,),
    ).fetchall()
    for message_id, reason in rows:
        if message_id in run.failed:
            continue
        if run.halted():
            return
        row = _fetch(run, message_id, "the fetch queue")
        if row is None:
            return
        if row is _GONE:
            with conn.transaction():
                run.report.gone += mark_gone(conn, run.account, message_id)
                dequeue(conn, message_id)
            continue
        if not isinstance(row, MessageRow):  # it failed on its own
            run.failed.add(message_id)
            _strike(conn, message_id)
            continue
        if row.internal_at < floor:  # older than the backfill reaches
            dequeue(conn, message_id)
            continue
        try:
            with conn.transaction():
                _count(run.report, store(conn, run.account, row, _ARRIVALS[reason]))
                dequeue(conn, message_id)
        except psycopg.DatabaseError as exc:
            # The database refused the row: a strike, as for a failed fetch.
            log.warning(
                "mail sync: queued %s could not be stored (%s)", message_id, type(exc).__name__
            )
            run.failed.add(message_id)
            _strike(conn, message_id)


# --- the switch-over ----------------------------------------------------------


def _switch_over(run: _Run, cursor: Cursor) -> None:
    """Once: unread mail outside the four other tabs, from the last seven days.

    The window runs to the listing itself, not to the run's start. On a
    fresh database `feed_from` is the run's start and the cursor is the
    profile's id, read a moment later: mail accepted in between is in no
    history page after the cursor, and newer than anything the backfill
    stores. What the listing shares with history is stored once.

    Done when every listed message not yet stored has been fetched; a run
    that stops part-way lists again next time, and skips what it stored.
    """
    ids = _list(run, SWITCH_OVER_QUERY, run.started - SWITCH_OVER_FOR, run.now(), "the switch-over")
    if ids is None or not _store_listed(run, ids, "switch_over"):
        return
    run.conn.execute(
        """
        UPDATE gmail_cursors SET switch_over_at = %s, updated_at = now()
         WHERE account = %s AND switch_over_at IS NULL
        """,
        (run.now(), run.account),
    )
    log.info("mail sync: switch-over listing done, %d message(s) listed", len(ids))


def _list(run: _Run, query: str, after: datetime, before: datetime, what: str) -> list[str] | None:
    """A window's ids, or None if the run must stop."""
    try:
        return run.gmail.message_ids(query, after=after, before=before)
    except ShareExhaustedError:
        run.report.stopped = "quota"
    except Exception as exc:
        run.failure(exc, what)
    return None


def _store_listed(
    run: _Run, ids: list[str], arrived_via: ArrivedVia, *, before: datetime | None = None
) -> bool:
    """Fetch and store the listed messages not stored yet. False if the run
    must stop first: what it stored stays, and the next run skips it."""
    have = stored_ids(run.conn, run.account, ids)
    for message_id in ids:
        if message_id in have:
            continue
        if not _fetch_and_store(run, message_id, arrived_via, before=before):
            return False
    return True


def _fetch_and_store(
    run: _Run, message_id: str, arrived_via: ArrivedVia, *, before: datetime | None = None
) -> bool:
    """Fetch and store one listed message. False if the run must stop.

    A 404 passes it over, and a failure of its own -- to fetch, or to store
    -- queues it. `before` stores nothing at or after it: the backfill's limit.
    """
    if run.halted():
        return False
    row = _fetch(run, message_id, "messages.get")
    if row is None:
        return False
    stored = (
        not isinstance(row, MessageRow)  # a 404 passes it over
        or (before is not None and row.internal_at >= before)
        or _stored(run, row, arrived_via)
    )
    if row is _FAILED or not stored:
        _failed_on_its_own(run, message_id)
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
        ids = _list(run, D1_QUERY, start, progress, "the catch-up's listing")
        if ids is None:
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


# --- the backfill ---------------------------------------------------------------


def _backfill(run: _Run, cursor: Cursor) -> None:
    """Last, with whatever quota the rest left (D3).

    A day at a time, newest first, from `feed_from` back to 90 days before it,
    listing D1's set and storing what D1 keeps. Its windows end at
    `feed_from`, and nothing at or after it is stored: the feed starts an hour
    before it, so a backfilled message must never look new. `backfill_until`
    records how far back it has reached, so a restart resumes rather than
    starting over; a day with no mail is recorded with the next one that has
    some, or when the run stops.
    """
    floor = cursor.feed_from - BACKFILL_FOR
    written = until = cursor.backfill_until
    while until > floor and not run.halted():
        start = max(floor, until - WINDOW)
        ids = _list(run, D1_QUERY, start, until, "the backfill's listing")
        if ids is None or not _store_listed(run, ids, "backfill", before=cursor.feed_from):
            break
        until = start
        if ids:
            written = _backfilled(run, written, until)
    if until != written:
        _backfilled(run, written, until)


def _backfilled(run: _Run, old: datetime, new: datetime) -> datetime:
    moved = run.conn.execute(
        """
        UPDATE gmail_cursors SET backfill_until = %s, updated_at = now()
         WHERE account = %s AND backfill_until = %s
        """,
        (new, run.account, old),
    ).rowcount
    if not moved:
        raise CursorMovedError(f"the backfill for {run.account} moved under this run")
    return new


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


# --- the command line (D8) --------------------------------------------------------

Out = Callable[[str], None]


def print_status(conn: psycopg.Connection, out: Out = print) -> None:
    """`--status`: each mailbox's cursor, `feed_from`, the backfill's, queue's
    and any catch-up's progress, counts by direction, category and how rows
    arrived, then the latest too-old records and the last recalls."""
    accounts = [row[0] for row in conn.execute("SELECT account FROM gmail_cursors ORDER BY 1")]
    if not accounts:
        out("No sync has run yet: poll still reads the newest unread page.")
    for account in accounts:
        found = status(conn, account)
        assert found is not None
        backfill, queue, gap = found["backfill"], found["queue"], found["catch_up"]
        out(account)
        out(f"  last reached the end of history: {found['caught_up_at']}")
        out(f"  feed from: {found['feed_from']}; switch-over listed: {found['switch_over_at']}")
        out(f"  backfill back to {backfill['until']}{' (done)' if backfill['done'] else ''}")
        if gap is None:
            out(f"  catch-up: none ({found['catch_ups']} so far)")
        else:
            out(f"  catch-up: {gap['from']} to {gap['until']}, listed to {gap['listed_back_to']}")
        out(f"  fetch queue: {queue['queued']} queued, {queue['unreadable']} unreadable")
        out(f"  rows: {found['rows']}")
        for column in ("direction", "category", "arrived_via"):
            counts = conn.execute(
                f"SELECT {column}, count(*) FROM gmail_messages WHERE account = %s"
                " GROUP BY 1 ORDER BY 1",
                (account,),
            ).fetchall()
            out(f"  by {column}: " + (", ".join(f"{name} {n}" for name, n in counts) or "none"))
    too_old = conn.execute(
        """
        SELECT gmail_message_id, created_at FROM processed_messages
         WHERE status = 'skipped' AND error = %s
         ORDER BY created_at DESC LIMIT 10
        """,
        (feed.TOO_OLD,),
    ).fetchall()
    out(f"Latest too-old records: {len(too_old) or 'none'}")
    for message_id, at in too_old:
        out(f"  {at:%Y-%m-%d %H:%M}  {message_id}")
    recalls = conn.execute(
        """
        SELECT job, finished_at, ok, seen, failed FROM job_runs
         WHERE job LIKE 'mail_recall%%'
         ORDER BY finished_at DESC LIMIT 6
        """
    ).fetchall()
    out(f"Last recalls: {len(recalls) or 'none'}")
    for job, at, ok, seen, failed in recalls:
        verdict = "ok" if ok else "SHORT"
        out(f"  {at:%Y-%m-%d %H:%M}  {job}  {verdict}  {seen} checked, {failed} short")


def print_message(conn: psycopg.Connection, message_id: str, out: Out = print) -> bool:
    """`--show`: one row's metadata. There is no content to show: none is
    stored. The ledger's status is shown without its reason, which can be
    the model's words."""
    cursor = conn.execute("SELECT * FROM gmail_messages WHERE message_id = %s", (message_id,))
    rows = cursor.fetchall()
    if not rows:
        out(f"{message_id}: not stored")
        return False
    assert cursor.description is not None
    names = [column.name for column in cursor.description]
    for row in rows:
        for name, value in zip(names, row, strict=True):
            out(f"  {name}: {value}")
    ledger = conn.execute(
        "SELECT status FROM processed_messages WHERE gmail_message_id = %s", (message_id,)
    ).fetchone()
    out(f"  ledger: {ledger[0] if ledger else 'none'}")
    queued = conn.execute(
        "SELECT reason, strikes, status FROM gmail_fetch_queue WHERE message_id = %s",
        (message_id,),
    ).fetchone()
    if queued is not None:
        out(f"  fetch queue: {queued[0]}, {queued[1]} strike(s), {queued[2]}")
    return True


def force_catch_up(
    conn: psycopg.Connection, gmail: GmailClient, *, now: datetime | None = None
) -> Gap:
    """`--catch-up`: as if the cursor had expired. The scheduled sync works
    the gap off; `--status` shows how far it has got."""
    profile = gmail.profile()
    cursor = load_cursor(conn, profile.address)
    if cursor is None:
        raise SystemExit("No sync has run for this mailbox yet: run --once first.")
    return record_gap(
        conn,
        profile.address,
        old_history_id=cursor.history_id,
        new_history_id=profile.history_id,
        since=(cursor.caught_up_at or cursor.feed_from) - MARGIN,
        until=now or datetime.now(UTC),
    )


def check_feed(conn: psycopg.Connection, out: Out = print) -> bool:
    """`--check-feed`: the exit criterion's checks of the switch-over (D8).

    1. No message older than `feed_from` less an hour has a ledger row made
       after the switch-over: the backfill never reached the pipeline.
    2. Every Primary message from the switch-over hour -- an hour either side
       of `feed_from` -- was processed, or recorded with a reason.
    """
    early = conn.execute(
        """
        SELECT m.message_id
          FROM gmail_messages m
          JOIN gmail_cursors c ON c.account = m.account
          JOIN processed_messages p ON p.gmail_message_id = m.message_id
         WHERE m.internal_at < c.feed_from - %(margin)s AND p.created_at >= c.created_at
         ORDER BY m.internal_at
        """,
        {"margin": feed.MARGIN},
    ).fetchall()
    waiting = conn.execute(
        f"""
        SELECT m.message_id
          FROM gmail_messages m
          JOIN gmail_cursors c ON c.account = m.account
         WHERE {feed.RULE}
           AND m.internal_at >= c.feed_from - %(margin)s
           AND m.internal_at < c.feed_from + %(margin)s
         ORDER BY m.internal_at
        """,
        {"margin": feed.MARGIN},
    ).fetchall()
    out(
        "Older mail processed after the switch-over: "
        + (", ".join(row[0] for row in early) if early else "none (good)")
    )
    out(
        "Switch-over hour mail neither processed nor recorded: "
        + (", ".join(row[0] for row in waiting) if waiting else "none (good)")
    )
    return not early and not waiting


@contextmanager
def _cli_lock(conn: psycopg.Connection) -> Iterator[None]:
    """The CLI waits for a scheduled run to finish, and says so."""
    with sync_lock(conn, wait=False) as held:
        if held:
            yield
            return
    print("Waiting for the scheduled run to finish...")
    with sync_lock(conn, wait=True):
        yield


def _connect(database_url: str) -> psycopg.Connection:
    return psycopg.connect(database_url, autocommit=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.mail.sync",
        description="The mail sync (M20). Every command waits for a scheduled run to finish.",
    )
    commands = parser.add_mutually_exclusive_group(required=True)
    commands.add_argument("--once", action="store_true", help="Run one pass.")
    commands.add_argument(
        "--status", action="store_true", help="The records, as counts and times only."
    )
    commands.add_argument("--show", metavar="MESSAGE_ID", help="One row's metadata.")
    commands.add_argument(
        "--catch-up", action="store_true", help="Force a catch-up, as if the cursor had expired."
    )
    commands.add_argument(
        "--check-feed", action="store_true", help="The exit criterion's switch-over checks."
    )
    args = parser.parse_args(argv)
    settings = get_settings()

    with _connect(settings.database_url) as conn, _cli_lock(conn):
        if args.once:
            report = sync_once(conn, sync_client(settings), owners=owners_of(settings))
            print(
                f"{report.records} record(s): {report.stored} stored, {report.updated} updated, "
                f"{report.gone} gone, {report.queued} queued; "
                f"{'reached the end of history' if report.reached_end else 'not at the end'}"
                f"{f' (stopped: {report.stopped})' if report.stopped else ''}."
            )
            return 0 if report.ok else 1
        if args.status:
            print_status(conn)
            return 0
        if args.show:
            return 0 if print_message(conn, args.show) else 1
        if args.catch_up:
            gap = force_catch_up(conn, sync_client(settings))
            print(
                f"Recorded a gap from {gap.since:%Y-%m-%d %H:%M} to {gap.until:%Y-%m-%d %H:%M} "
                "UTC. The scheduled sync works it off; --status shows how far it has got."
            )
            return 0
        return 0 if check_feed(conn) else 1


if __name__ == "__main__":
    raise SystemExit(main())
