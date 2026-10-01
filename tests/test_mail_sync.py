"""The mail sync (M20, D3), against a fake mailbox and a real Postgres.

The fake answers as Gmail does (`tests/fake_gmail.py`); the cursor, the rows
and the queue are Postgres behaviour, so these run as integration tests.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fake_gmail import SECRET, FakeMailbox, http_error

from app.google.gmail import GmailClient
from app.mail import feed, quota, sync
from app.mail.messages import MessageRow, classify, store
from app.mail.quota import Pacer
from app.mail.sync import Cursor, SyncReport, load_cursor, sync_lock, sync_once

pytestmark = pytest.mark.integration

ME = "me@example.com"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
PRIMARY = {"INBOX", "UNREAD", "CATEGORY_PERSONAL"}


@dataclass
class Clock:
    """Monotonic seconds for the run's 60-second bound; they pass only when a
    test says so."""

    now: float = 0.0
    step: float = 0.0
    """Added on every reading: a run that is slow."""

    def __call__(self) -> float:
        self.now += self.step
        return self.now


@pytest.fixture
def mail(conn: psycopg.Connection) -> Iterator[psycopg.Connection]:
    for table in ("gmail_messages", "gmail_cursors", "gmail_fetch_queue"):
        conn.execute(f"DELETE FROM {table}")
    yield conn


def _client(box: FakeMailbox, pacer: Pacer | None = None) -> GmailClient:
    # No retries: a scripted failure is an outage at once, and nothing sleeps.
    return GmailClient(box, pacer=pacer or Pacer(), share=quota.SYNC, retry_for=0)


def _sync(
    conn: psycopg.Connection,
    box: FakeMailbox,
    *,
    now: datetime = NOW,
    pacer: Pacer | None = None,
    clock: Clock | None = None,
    stop: threading.Event | None = None,
) -> SyncReport:
    return sync_once(
        conn,
        _client(box, pacer),
        owners=(ME,),
        now=lambda: now,
        clock=clock or Clock(),
        stop=stop,
    )


def _cursor(conn: psycopg.Connection) -> Cursor:
    found = load_cursor(conn, ME)
    assert found is not None
    return found


def _rows(conn: psycopg.Connection) -> dict[str, dict[str, Any]]:
    cursor = conn.execute("SELECT * FROM gmail_messages WHERE account = %s", (ME,))
    assert cursor.description is not None
    names = [column.name for column in cursor.description]
    return {row[1]: dict(zip(names, row, strict=True)) for row in cursor.fetchall()}


def _queue(conn: psycopg.Connection) -> dict[str, tuple[str, int, str]]:
    rows = conn.execute("SELECT message_id, reason, strikes, status FROM gmail_fetch_queue")
    return {row[0]: (row[1], row[2], row[3]) for row in rows.fetchall()}


def _old_poller(conn: psycopg.Connection, history_id: str | None, at: datetime) -> None:
    conn.execute(
        "UPDATE sync_state SET last_history_id = %s, updated_at = %s WHERE id = 1",
        (history_id, at),
    )


def _started(conn: psycopg.Connection, box: FakeMailbox, *, backfilled: bool = True) -> None:
    """A mailbox already synced: the cursor at its current id, caught up and
    switched over at NOW, and -- unless asked otherwise -- nothing left to
    backfill."""
    conn.execute(
        """
        INSERT INTO gmail_cursors (account, history_id, feed_from, switch_over_at,
                                   caught_up_at, backfill_until)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (ME, str(box.history_id), NOW, NOW, NOW, NOW - timedelta(days=91) if backfilled else NOW),
    )


# --- the first run ----------------------------------------------------------


def test_the_first_run_starts_where_the_old_poller_stopped(mail: psycopg.Connection) -> None:
    """Its replay covers everything since the old poller's last full pass."""
    box = FakeMailbox()
    last_pass = NOW - timedelta(minutes=8)
    _old_poller(mail, str(box.history_id), last_pass)
    box.deliver("after-the-pass", labels=PRIMARY, at=NOW - timedelta(minutes=5))

    report = _sync(mail, box)

    cursor = _cursor(mail)
    assert cursor.feed_from == last_pass
    # The backfill starts at `feed_from`; with nothing to store it went all the way.
    assert cursor.backfill_until == last_pass - timedelta(days=90)
    assert cursor.history_id == str(box.history_id)
    assert cursor.caught_up_at == NOW
    assert report.reached_end
    assert _rows(mail)["after-the-pass"]["arrived_via"] == "history"
    assert box.asked("history.list")[0]["startHistoryId"] == "1000"


def test_a_fresh_database_starts_from_the_mailbox_as_it_is_now(mail: psycopg.Connection) -> None:
    box = FakeMailbox(history_id=4242)
    _old_poller(mail, None, NOW - timedelta(days=30))

    _sync(mail, box)

    cursor = _cursor(mail)
    assert (cursor.history_id, cursor.feed_from) == ("4242", NOW)


def test_an_expired_old_cursor_makes_the_first_run_a_catch_up(mail: psycopg.Connection) -> None:
    box = FakeMailbox()
    last_pass = NOW - timedelta(days=9)
    _old_poller(mail, "900", last_pass)
    box.expire()

    report = _sync(mail, box)

    assert report.catch_up == (last_pass - timedelta(hours=1), NOW)
    cursor = _cursor(mail)
    assert cursor.history_id == str(box.history_id)
    assert cursor.catch_ups == 1


def test_the_switch_over_listing_runs_once(mail: psycopg.Connection) -> None:
    """Unread mail outside the four other tabs, from the last seven days."""
    box = FakeMailbox()
    box.put("unread", labels=PRIMARY, at=NOW - timedelta(days=2))
    box.put("read", labels={"INBOX", "CATEGORY_PERSONAL"}, at=NOW - timedelta(days=2))
    box.put("promo", labels={"INBOX", "UNREAD", "CATEGORY_PROMOTIONS"}, at=NOW - timedelta(days=1))
    box.put("updates", labels={"INBOX", "UNREAD", "CATEGORY_UPDATES"}, at=NOW - timedelta(days=1))
    box.put("too-old", labels=PRIMARY, at=NOW - timedelta(days=8))
    _old_poller(mail, None, NOW)

    _sync(mail, box)
    _sync(mail, box)

    assert _cursor(mail).switch_over_at == NOW
    rows = _rows(mail)
    assert rows["unread"]["arrived_via"] == "switch_over"
    # Read mail, older mail and other tabs are the backfill's, if anyone's.
    for other in ("read", "promo", "updates", "too-old"):
        assert rows.get(other, {}).get("arrived_via") != "switch_over"
    switch_over_queries = [q for q in box.asked("messages.list") if "is:unread" in q["q"]]
    assert len(switch_over_queries) == 1


# --- incremental passes -----------------------------------------------------


def test_a_pass_stores_new_mail_applies_label_changes_and_marks_deletions(
    mail: psycopg.Connection,
) -> None:
    box = FakeMailbox()
    _started(mail, box)
    box.deliver("new", labels=PRIMARY, at=NOW)
    box.deliver("sent", labels={"SENT"}, at=NOW, sender=ME, to="sara@example.com")
    box.put("stored-earlier", labels=PRIMARY, at=NOW - timedelta(hours=3))
    _sync(mail, box)
    mail.execute(
        """
        INSERT INTO gmail_messages (account, message_id, thread_id, internal_at, label_ids,
                                    direction, to_self, category, has_list_unsubscribe,
                                    arrived_via)
        VALUES (%s, 'stored-earlier', 't', %s, %s, 'in', false, 'primary', false, 'history')
        """,
        (ME, NOW - timedelta(hours=3), sorted(PRIMARY)),
    )
    fetched_before = len(box.fetched())
    box.relabel("stored-earlier", remove={"UNREAD", "CATEGORY_PERSONAL"}, add={"CATEGORY_FORUMS"})
    box.delete("new")

    report = _sync(mail, box)

    rows = _rows(mail)
    assert rows["sent"]["direction"] == "out"
    assert rows["stored-earlier"]["label_ids"] == ["CATEGORY_FORUMS", "INBOX"]
    assert rows["stored-earlier"]["category"] == "forums"
    assert rows["new"]["gone_at"] is not None
    # The label change needed no fetch.
    assert "stored-earlier" not in box.fetched()[fetched_before:]
    assert report.reached_end


def test_a_label_change_that_could_bring_a_message_in_is_queued_not_fetched_in_the_pass(
    mail: psycopg.Connection,
) -> None:
    """Out of Spam, Trash, Promotions or Social: the queue fetches it after
    the pass, so a slow fetch never holds up the cursor."""
    box = FakeMailbox()
    _started(mail, box)
    box.put("promo", labels={"INBOX", "CATEGORY_PROMOTIONS"}, at=NOW - timedelta(days=1))
    box.put("spam", labels={"SPAM", "CATEGORY_PERSONAL"}, at=NOW - timedelta(days=1))
    box.put("social", labels={"INBOX", "CATEGORY_SOCIAL"}, at=NOW - timedelta(days=1))
    box.relabel("promo", remove={"CATEGORY_PROMOTIONS"}, add={"CATEGORY_PERSONAL"})
    box.relabel("spam", remove={"SPAM"}, add={"INBOX"})
    box.relabel("social", add={"STARRED"})  # cannot bring it in: still Social
    calls_before = len(box.calls)

    report = _sync(mail, box)

    calls = box.calls[calls_before:]
    last_history = max(i for i, (name, _) in enumerate(calls) if name == "history.list")
    fetches = [
        (i, kwargs["id"]) for i, (name, kwargs) in enumerate(calls) if name == "messages.get"
    ]
    assert sorted(message for _, message in fetches) == ["promo", "spam"]
    assert all(i > last_history for i, _ in fetches)
    rows = _rows(mail)
    assert rows["promo"]["arrived_via"] == "queue" and "spam" in rows
    assert "social" not in rows
    assert report.queued == 2 and _queue(mail) == {}


def test_a_message_added_and_deleted_in_one_pass_is_never_fetched(
    mail: psycopg.Connection,
) -> None:
    """Drafts are replaced constantly; each would otherwise cost a fetch and a 404."""
    box = FakeMailbox()
    _started(mail, box)
    box.deliver("draft-1", labels={"DRAFT"}, at=NOW)
    box.delete("draft-1")
    box.deliver("draft-2", labels={"DRAFT"}, at=NOW)

    _sync(mail, box)

    assert "draft-1" not in box.fetched()
    assert _rows(mail) == {}  # and draft-2, fetched, is not stored


@pytest.mark.parametrize(
    "labels",
    [
        {"DRAFT"},
        {"CHAT"},
        {"SPAM", "CATEGORY_PERSONAL"},
        {"TRASH", "INBOX"},
        {"INBOX", "CATEGORY_PROMOTIONS"},
        {"INBOX", "CATEGORY_SOCIAL"},
    ],
)
def test_what_d1_leaves_out_is_never_stored(mail: psycopg.Connection, labels: set[str]) -> None:
    box = FakeMailbox()
    _started(mail, box)
    box.deliver("m", labels=labels, at=NOW)

    _sync(mail, box)

    assert _rows(mail) == {}


def test_a_fetch_404_in_a_pass_is_passed_over(mail: psycopg.Connection) -> None:
    box = FakeMailbox()
    _started(mail, box)
    box.deliver("vanished", labels=PRIMARY, at=NOW)
    box.messages.pop("vanished")  # deleted, its deletion not yet in history
    box.deliver("next", labels=PRIMARY, at=NOW)

    report = _sync(mail, box)

    assert set(_rows(mail)) == {"next"}
    assert _queue(mail) == {}
    assert report.reached_end and report.error is None


def test_a_message_that_fails_on_its_own_is_queued_and_the_pass_goes_on(
    mail: psycopg.Connection,
) -> None:
    """One bad message never holds up the cursor."""
    box = FakeMailbox()
    _started(mail, box)
    box.deliver("bad", labels=PRIMARY, at=NOW)
    box.deliver("good", labels=PRIMARY, at=NOW)
    box.fail("messages.get:bad", http_error(400))

    report = _sync(mail, box)

    assert set(_rows(mail)) == {"good"}
    # One strike: the queue tries it again at the next run, not straight away.
    assert _queue(mail)["bad"] == ("fetch_failed", 1, "queued")
    assert _cursor(mail).history_id == str(box.history_id)
    assert report.reached_end

    _sync(mail, box)
    assert set(_rows(mail)) == {"good", "bad"} and _queue(mail) == {}


def test_a_header_with_a_nul_is_stored_without_it(mail: psycopg.Connection) -> None:
    """Postgres text cannot hold NUL. Kept, it failed the row's insert, which
    rolled back its whole page, and every run replayed that page for ever."""
    box = FakeMailbox()
    _started(mail, box)
    box.deliver("nul", labels=PRIMARY, at=NOW, to="me@exam\x00ple.com", Auto_Submitted="n\x00o")
    box.deliver("next", labels=PRIMARY, at=NOW)

    report = _sync(mail, box)

    rows = _rows(mail)
    assert rows["nul"]["to_addrs"] == ["me@example.com"]
    assert rows["nul"]["auto_submitted"] == "no"
    assert "next" in rows and report.reached_end


def _refusing(monkeypatch: pytest.MonkeyPatch, *refused: str) -> None:
    """The database refuses these messages' rows: a category its CHECK rejects."""

    def classify_badly(meta: Any, owners: frozenset[str]) -> MessageRow:
        row = classify(meta, owners)
        return replace(row, category="nonsense") if meta.id in refused else row  # type: ignore[arg-type]

    monkeypatch.setattr("app.mail.sync.classify", classify_badly)


def test_a_row_the_database_refuses_is_queued_and_its_page_goes_on(
    mail: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each row is stored in a savepoint of its own: a refused one is struck
    like a failed fetch, and the cursor moves past it."""
    box = FakeMailbox()
    _started(mail, box)
    box.deliver("refused", labels=PRIMARY, at=NOW)
    box.deliver("next", labels=PRIMARY, at=NOW)
    _refusing(monkeypatch, "refused")

    report = _sync(mail, box)

    assert set(_rows(mail)) == {"next"}
    assert _queue(mail)["refused"] == ("fetch_failed", 1, "queued")
    assert _cursor(mail).history_id == str(box.history_id)
    assert report.reached_end and report.ok

    for _ in range(4):  # the queue tries it again at each run, and strikes it
        _sync(mail, box)
    assert _queue(mail)["refused"] == ("fetch_failed", 5, "unreadable")


def test_an_outage_stops_the_pass_and_blames_no_message(mail: psycopg.Connection) -> None:
    """Gmail down: the fetch fails, and so does the probe after it."""
    box = FakeMailbox()
    _started(mail, box)
    box.deliver("first", labels=PRIMARY, at=NOW)
    first_record = box.history_id
    box.deliver("second", labels=PRIMARY, at=NOW)
    box.fail("messages.get:second", http_error(503))
    box.fail("getProfile", None, http_error(503))  # the run's own read answers

    report = _sync(mail, box)

    assert set(_rows(mail)) == {"first"}
    assert _queue(mail) == {}  # no strike, no queue
    assert _cursor(mail).history_id == str(first_record)  # the last record handled
    assert (report.reached_end, report.stopped, report.error) == (False, "outage", "HttpError")

    _sync(mail, box)  # the next tick picks up where it stopped
    assert set(_rows(mail)) == {"first", "second"}


def test_one_message_that_answers_500_every_time_does_not_wedge_the_sync(
    mail: psycopg.Connection,
) -> None:
    """Gmail answers the probe, so the failure is the message's own: it is
    queued and struck, and passes, queue and feed carry on without it."""
    box = FakeMailbox()
    _started(mail, box)
    box.deliver("bad", labels=PRIMARY, at=NOW)
    box.deliver("good", labels=PRIMARY, at=NOW)
    box.fail("messages.get:bad", *[http_error(500)] * 20)

    report = _sync(mail, box)

    assert _cursor(mail).history_id == str(box.history_id)
    assert set(_rows(mail)) == {"good"}
    assert _queue(mail)["bad"] == ("fetch_failed", 1, "queued")
    assert report.reached_end and report.stopped is None
    after_the_failure = [name for name, _ in box.calls][box.fetched().index("bad") :]
    assert "getProfile" in after_the_failure  # the one cheap probe

    for _ in range(4):
        _sync(mail, box)
    assert _queue(mail)["bad"] == ("fetch_failed", 5, "unreadable")

    box.deliver("later", labels=PRIMARY, at=NOW)
    report = _sync(mail, box)

    assert report.reached_end and "later" in _rows(mail)
    assert feed.candidates(mail, 10, now=NOW) == ["good", "later"]


def test_a_queue_entry_that_failed_before_waits_behind_the_rest(mail: psycopg.Connection) -> None:
    """A bad head must not starve the queue: entries that failed more often,
    or more recently, are tried after the others."""
    box = FakeMailbox()
    _started(mail, box)
    for message_id in ("struck-twice", "struck-lately", "struck-long-ago", "clean"):
        box.put(message_id, labels=PRIMARY, at=NOW - timedelta(hours=1))
    mail.execute(
        """
        INSERT INTO gmail_fetch_queue (message_id, reason, queued_at, strikes, failed_at) VALUES
            ('struck-twice', 'refetch', now() - interval '3h', 2, now() - interval '2h'),
            ('struck-lately', 'refetch', now() - interval '3h', 1, now()),
            ('struck-long-ago', 'refetch', now() - interval '3h', 1, now() - interval '1h'),
            ('clean', 'fetch_failed', now(), 0, NULL)
        """
    )
    # The run's profile read and history page, then three fetches.
    pacer = Pacer(shares={quota.SYNC: 1 + 2 + 3 * 20}, clock=lambda: 0.0)

    report = _sync(mail, box, pacer=pacer)

    assert report.stopped == "quota"
    # The fourth was asked for, and refused by the pacer.
    assert box.fetched() == ["clean", "struck-long-ago", "struck-lately", "struck-twice"]
    assert set(_rows(mail)) == {"clean", "struck-long-ago", "struck-lately"}


def test_a_backfilled_message_that_answers_500_every_time_is_queued_and_the_backfill_goes_on(
    mail: psycopg.Connection,
) -> None:
    box = FakeMailbox()
    _started(mail, box, backfilled=False)
    box.put("bad", labels={"INBOX", "CATEGORY_PERSONAL"}, at=NOW - timedelta(days=2))
    box.put("older", labels={"INBOX", "CATEGORY_PERSONAL"}, at=NOW - timedelta(days=3))
    box.fail("messages.get:bad", *[http_error(503)] * 20)

    report = _sync(mail, box)

    assert set(_rows(mail)) == {"older"}
    assert _queue(mail)["bad"] == ("fetch_failed", 1, "queued")
    assert _cursor(mail).backfill_until == NOW - timedelta(days=90)
    assert report.stopped is None


def test_a_failing_history_list_stops_the_pass_where_it_is(mail: psycopg.Connection) -> None:
    box = FakeMailbox()
    _started(mail, box)
    before = _cursor(mail)
    box.deliver("m", labels=PRIMARY, at=NOW)
    box.fail("history.list", http_error(500))

    report = _sync(mail, box)

    assert _cursor(mail).history_id == before.history_id
    assert report.error == "HttpError" and not report.reached_end


def test_five_strikes_make_a_queued_message_unreadable(mail: psycopg.Connection) -> None:
    box = FakeMailbox()
    _started(mail, box)
    box.deliver("bad", labels=PRIMARY, at=NOW)
    box.fail("messages.get:bad", *[http_error(400)] * 10)

    for _ in range(5):
        _sync(mail, box)

    assert _queue(mail)["bad"] == ("fetch_failed", 5, "unreadable")
    attempts = box.fetched().count("bad")
    _sync(mail, box)
    assert box.fetched().count("bad") == attempts  # passed over from now on


def test_an_outage_in_the_queue_charges_no_strike(mail: psycopg.Connection) -> None:
    box = FakeMailbox()
    _started(mail, box)
    box.deliver("bad", labels=PRIMARY, at=NOW)
    box.fail("messages.get:bad", http_error(400), http_error(503))
    # Both runs' own profile reads answer; the probe after the 503 does not.
    box.fail("getProfile", None, None, http_error(503))

    _sync(mail, box)  # the pass's strike
    report = _sync(mail, box)  # an outage while the queue fetches it

    assert report.stopped == "outage"
    assert _queue(mail)["bad"] == ("fetch_failed", 1, "queued")
    _sync(mail, box)
    assert "bad" in _rows(mail) and _queue(mail) == {}


def test_a_crash_mid_pass_costs_at_most_one_page(
    mail: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    box = FakeMailbox(page_size=2)
    _started(mail, box)
    for name in ("a", "b", "c", "d"):
        box.deliver(name, labels=PRIMARY, at=NOW)
    page_one_end = int(box.history[-3]["id"])

    def crash_on_c(conn: Any, account: str, row: Any, arrived_via: Any) -> Any:
        if row.message_id == "c":
            raise RuntimeError("killed mid-page")
        return store(conn, account, row, arrived_via)

    monkeypatch.setattr("app.mail.sync.store", crash_on_c)
    with pytest.raises(RuntimeError):
        _sync(mail, box)

    assert set(_rows(mail)) == {"a", "b"}
    assert _cursor(mail).history_id == str(page_one_end)

    monkeypatch.setattr("app.mail.sync.store", store)
    _sync(mail, box)
    assert set(_rows(mail)) == {"a", "b", "c", "d"}
    assert _cursor(mail).history_id == str(box.history_id)


def test_the_cursor_moves_only_from_where_this_run_found_it(mail: psycopg.Connection) -> None:
    """Every write to the cursor is conditional on the value it replaces."""
    box = FakeMailbox()
    _started(mail, box)
    box.deliver("m", labels=PRIMARY, at=NOW)
    real_page = box._history

    def page_then_move(**kwargs: Any) -> Any:
        mail.execute("UPDATE gmail_cursors SET history_id = '1' WHERE account = %s", (ME,))
        return real_page(**kwargs)

    box._history = page_then_move  # type: ignore[method-assign]

    with pytest.raises(sync.CursorMovedError):
        _sync(mail, box)
    assert "m" not in _rows(mail)


def test_a_run_stops_at_its_sixty_seconds_and_the_next_carries_on(
    mail: psycopg.Connection,
) -> None:
    box = FakeMailbox(page_size=1)
    _started(mail, box)
    for name in ("a", "b", "c"):
        box.deliver(name, labels=PRIMARY, at=NOW)

    report = _sync(mail, box, clock=Clock(step=25.0))

    assert report.stopped == "time" and not report.reached_end
    assert 0 < len(_rows(mail)) < 3
    _sync(mail, box)
    assert set(_rows(mail)) == {"a", "b", "c"}


def test_a_shutdown_stops_the_pass_between_pages(mail: psycopg.Connection) -> None:
    box = FakeMailbox(page_size=1)
    _started(mail, box)
    for name in ("a", "b"):
        box.deliver(name, labels=PRIMARY, at=NOW)
    stop = threading.Event()
    stop.set()

    report = _sync(mail, box, stop=stop)

    assert report.stopped == "shutdown"
    assert _rows(mail) == {}


def test_the_sync_is_held_to_its_share_of_the_minute(mail: psycopg.Connection) -> None:
    """Counted by method: a run that has spent its 2,000 units stops, and
    the next minute carries on."""
    box = FakeMailbox()
    _started(mail, box)
    for number in range(110):
        box.deliver(f"m{number:03}", labels=PRIMARY, at=NOW)
    pacer = Pacer(clock=lambda: 0.0)

    report = _sync(mail, box, pacer=pacer)

    assert report.stopped == "quota"
    spent = pacer.by_method()
    assert sum(spent.values()) <= 2000
    assert set(spent) <= {"getProfile", "history.list", "messages.get", "messages.list"}
    assert 90 <= len(_rows(mail)) < 110
    _sync(mail, box)  # a fresh minute
    assert len(_rows(mail)) == 110


# --- catching up --------------------------------------------------------------


def test_a_history_404_reads_the_profile_first_records_the_gap_and_moves_the_cursor(
    mail: psycopg.Connection,
) -> None:
    box = FakeMailbox()
    _started(mail, box)
    caught_up = _cursor(mail).caught_up_at
    assert caught_up is not None
    box.deliver("during-outage", labels=PRIMARY, at=NOW + timedelta(days=2))
    box.expire()
    later = NOW + timedelta(days=9)

    report = _sync(mail, box, now=later)

    assert report.catch_up == (caught_up - timedelta(hours=1), later)
    assert _cursor(mail).history_id == str(box.history_id)
    methods = [name for name, _ in box.calls]
    expired = max(i for i, name in enumerate(methods) if name == "history.list")
    assert "getProfile" in methods[expired:]  # the new cursor is read after the 404
    assert _rows(mail)["during-outage"]["arrived_via"] == "catch_up"


def test_the_gap_is_listed_with_d1s_set_and_worked_off(mail: psycopg.Connection) -> None:
    box = FakeMailbox()
    _started(mail, box)
    box.expire()
    for day in range(1, 4):
        box.put(f"gap-{day}", labels=PRIMARY, at=NOW + timedelta(days=day))
    later = NOW + timedelta(days=4)

    _sync(mail, box, now=later)  # records the gap, lists it, fetches it

    rows = _rows(mail)
    assert {"gap-1", "gap-2", "gap-3"} <= set(rows)
    assert rows["gap-1"]["arrived_via"] == "catch_up"
    cursor = _cursor(mail)
    assert (cursor.gap_from, cursor.gap_until, cursor.gap_progress) == (None, None, None)
    queries = [q["q"] for q in box.asked("messages.list")]
    assert any(
        q.startswith("-category:promotions -category:social -in:chats -in:drafts") for q in queries
    )


def test_a_catch_up_resumes_after_a_restart(mail: psycopg.Connection) -> None:
    box = FakeMailbox()
    _started(mail, box)
    box.expire()
    for day in range(1, 6):
        box.put(f"gap-{day}", labels=PRIMARY, at=NOW + timedelta(days=day, hours=1))
    later = NOW + timedelta(days=6)

    # Killed after its first two windows.
    report = _sync(mail, box, now=later, clock=Clock(step=12.0))
    assert report.stopped == "time"
    progress = _cursor(mail).gap_progress
    assert progress is not None and progress < later

    _sync(mail, box, now=later)  # the restart carries on from `gap_progress`
    _sync(mail, box, now=later)

    assert {f"gap-{day}" for day in range(1, 6)} <= set(_rows(mail))
    assert _cursor(mail).gap_from is None


def test_a_catch_up_refetches_the_last_seven_days_so_trashed_mail_is_seen(
    mail: psycopg.Connection,
) -> None:
    """Mail trashed or spammed during the outage is not listed, so the gap's
    listing cannot see it. Fetching the week's rows again by id does."""
    box = FakeMailbox()
    _started(mail, box)
    box.deliver("kept", labels=PRIMARY, at=NOW - timedelta(days=1))
    box.deliver("old", labels=PRIMARY, at=NOW - timedelta(days=12))
    _sync(mail, box)
    box.expire()
    box.messages["kept"].labels |= {"TRASH"}  # during the outage: no history survives

    _sync(mail, box, now=NOW + timedelta(days=1))

    rows = _rows(mail)
    assert "TRASH" in rows["kept"]["label_ids"]
    assert "TRASH" not in rows["old"]["label_ids"]  # beyond the feed's seven days


def test_a_catch_up_marks_nothing_gone_for_not_being_listed(mail: psycopg.Connection) -> None:
    """Archived or recategorised mail is not gone: the gap's listing leaves
    out Promotions, and the old code read `in:inbox` too."""
    box = FakeMailbox()
    _started(mail, box)
    box.deliver("archived", labels=PRIMARY, at=NOW)
    box.deliver("moved", labels=PRIMARY, at=NOW)
    _sync(mail, box)
    box.expire()
    box.messages["archived"].labels -= {"INBOX"}
    box.messages["moved"].labels = {"INBOX", "CATEGORY_PROMOTIONS"}

    _sync(mail, box, now=NOW + timedelta(days=1))

    rows = _rows(mail)
    assert rows["archived"]["gone_at"] is None
    assert rows["moved"]["gone_at"] is None
    assert rows["moved"]["category"] == "promotions"  # the re-fetch saw where it went


def test_a_refetch_that_answers_404_marks_the_row_gone(mail: psycopg.Connection) -> None:
    box = FakeMailbox()
    _started(mail, box)
    box.deliver("deleted", labels=PRIMARY, at=NOW - timedelta(days=1))
    _sync(mail, box)
    box.expire()
    box.messages.pop("deleted")  # deleted during the outage

    _sync(mail, box, now=NOW + timedelta(days=1))

    assert _rows(mail)["deleted"]["gone_at"] is not None


def test_every_window_is_in_epoch_seconds(mail: psycopg.Connection) -> None:
    box = FakeMailbox()
    box.put("m", labels=PRIMARY, at=NOW - timedelta(days=3))
    _old_poller(mail, None, NOW)
    _sync(mail, box)
    box.expire()
    _sync(mail, box, now=NOW + timedelta(days=2))

    queries = [q["q"] for q in box.asked("messages.list")]
    assert queries
    for query in queries:
        assert re.search(r"after:\d+ before:\d+$", query), query
        assert "/" not in query and "category:primary" not in query


# --- the backfill (20.5) ----------------------------------------------------------

READ = {"INBOX", "CATEGORY_PERSONAL"}


def test_the_backfill_reaches_ninety_days_before_feed_from_and_no_further(
    mail: psycopg.Connection,
) -> None:
    box = FakeMailbox()
    _started(mail, box, backfilled=False)
    box.put("recent", labels=READ, at=NOW - timedelta(days=1))
    box.put("updates", labels={"CATEGORY_UPDATES"}, at=NOW - timedelta(days=30))
    box.put("sent", labels={"SENT"}, at=NOW - timedelta(days=60), sender=ME)
    box.put("promo", labels={"INBOX", "CATEGORY_PROMOTIONS"}, at=NOW - timedelta(days=10))
    box.put("too-old", labels=READ, at=NOW - timedelta(days=91))

    _sync(mail, box)

    rows = _rows(mail)
    assert {m: rows[m]["arrived_via"] for m in ("recent", "updates", "sent")} == dict.fromkeys(
        ("recent", "updates", "sent"), "backfill"
    )
    assert "promo" not in rows and "too-old" not in rows
    assert _cursor(mail).backfill_until == NOW - timedelta(days=90)
    listings = len(box.asked("messages.list"))
    _sync(mail, box)
    assert len(box.asked("messages.list")) == listings  # done: nothing listed again


def test_the_backfill_never_stores_mail_newer_than_feed_from(mail: psycopg.Connection) -> None:
    """Its windows end at `feed_from`; a listing's spare second is not stored."""
    box = FakeMailbox()
    last_pass = NOW - timedelta(hours=2)
    _old_poller(mail, str(box.history_id), last_pass)
    box.put("before", labels=READ, at=last_pass - timedelta(minutes=1))
    box.put("at-the-instant", labels=READ, at=last_pass)
    box.put("after", labels=READ, at=last_pass + timedelta(minutes=1))

    _sync(mail, box)

    rows = _rows(mail)
    assert rows["before"]["arrived_via"] == "backfill"
    assert "at-the-instant" not in rows and "after" not in rows


def test_the_backfill_resumes_from_backfill_until_after_a_restart(
    mail: psycopg.Connection,
) -> None:
    box = FakeMailbox()
    _started(mail, box, backfilled=False)
    for day in range(1, 11):
        box.put(f"d{day}", labels=READ, at=NOW - timedelta(days=day, hours=12))

    report = _sync(mail, box, clock=Clock(step=5.0))  # stopped part-way

    assert report.stopped == "time"
    reached = _cursor(mail).backfill_until
    assert NOW - timedelta(days=90) < reached < NOW
    _sync(mail, box)  # the restart

    assert {f"d{day}" for day in range(1, 11)} <= set(_rows(mail))
    assert len(box.fetched()) == len(set(box.fetched()))  # nothing fetched twice


def test_the_backfill_only_spends_what_the_incremental_pass_left(
    mail: psycopg.Connection,
) -> None:
    box = FakeMailbox()
    _started(mail, box, backfilled=False)
    for number in range(98):
        box.deliver(f"new{number:02}", labels=PRIMARY, at=NOW)
    for number in range(5):
        box.put(f"old{number}", labels=READ, at=NOW - timedelta(days=2))
    pacer = Pacer(clock=lambda: 0.0)

    report = _sync(mail, box, pacer=pacer)

    rows = _rows(mail)
    assert {f"new{number:02}" for number in range(98)} <= set(rows)  # live mail first
    assert sum(1 for m in rows if m.startswith("old")) <= 1
    assert report.stopped == "quota"
    names = [name for name, _ in box.calls]
    first_listing = names.index("messages.list")
    assert "history.list" not in names[first_listing:]


def test_a_backfilled_row_the_database_refuses_is_queued_and_the_backfill_goes_on(
    mail: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    box = FakeMailbox()
    _started(mail, box, backfilled=False)
    box.put("refused", labels=READ, at=NOW - timedelta(days=2))
    box.put("older", labels=READ, at=NOW - timedelta(days=3))
    _refusing(monkeypatch, "refused")

    _sync(mail, box)

    assert set(_rows(mail)) == {"older"}
    assert _queue(mail)["refused"] == ("fetch_failed", 1, "queued")
    assert _cursor(mail).backfill_until == NOW - timedelta(days=90)


def test_a_queued_message_older_than_ninety_days_is_dropped(mail: psycopg.Connection) -> None:
    """A label change can touch years-old mail; only the last 90 days matter."""
    box = FakeMailbox()
    _started(mail, box)
    box.put("ancient", labels={"INBOX", "CATEGORY_PROMOTIONS"}, at=NOW - timedelta(days=120))
    box.relabel("ancient", remove={"CATEGORY_PROMOTIONS"}, add={"CATEGORY_PERSONAL"})

    _sync(mail, box)

    assert "ancient" in box.fetched()
    assert "ancient" not in _rows(mail)
    assert _queue(mail) == {}


# --- what /health shows (20.7) ------------------------------------------------------


def test_the_status_is_counts_and_times_only(mail: psycopg.Connection) -> None:
    box = FakeMailbox()
    _started(mail, box)
    box.deliver("m", labels=PRIMARY, at=NOW)
    box.deliver("bad", labels=PRIMARY, at=NOW)
    box.fail("messages.get:bad", http_error(400))
    _sync(mail, box)

    found = sync.status(mail, ME)

    assert found is not None
    assert found["rows"] == 1
    assert found["queue"] == {"queued": 1, "unreadable": 0}
    assert found["caught_up_at"] == NOW.isoformat()
    assert found["backfill"]["done"] is True
    assert found["catch_up"] is None
    assert sync.status(mail, "nobody@example.com") is None


# --- the lock -------------------------------------------------------------------


def test_a_scheduled_run_skips_its_turn_while_another_holds_the_lock(
    mail: psycopg.Connection, migrated_database: str
) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as other:
        with sync_lock(other, wait=False) as held:
            assert held
            with sync_lock(mail, wait=False) as second:
                assert not second
        with sync_lock(mail, wait=False) as after:
            assert after


def test_a_cli_run_waits_for_the_scheduled_run(migrated_database: str) -> None:
    order: list[str] = []
    with psycopg.connect(migrated_database, autocommit=True) as scheduled:
        with sync_lock(scheduled, wait=False) as held:
            assert held

            def cli() -> None:
                with (
                    psycopg.connect(migrated_database, autocommit=True) as conn,
                    sync_lock(conn, wait=True),
                ):
                    order.append("cli")

            waiting = threading.Thread(target=cli)
            waiting.start()
            waiting.join(timeout=1.0)
            order.append("scheduled done")
        waiting.join(timeout=10.0)

    assert order == ["scheduled done", "cli"]


# --- content --------------------------------------------------------------------


def test_nothing_the_sync_stores_carries_content(mail: psycopg.Connection) -> None:
    box = FakeMailbox()
    _started(mail, box)
    box.deliver("m", labels=PRIMARY, at=NOW)

    _sync(mail, box)

    assert SECRET not in repr(_rows(mail))
    for kwargs in box.asked("messages.get"):
        assert kwargs["fields"] == "id,threadId,labelIds,internalDate,payload/headers"
