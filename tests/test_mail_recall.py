"""Recall, of the sync and of the feed (M20, D5), against a fake mailbox and a
real Postgres.

The checks run at a NOW long before any real row: the audit log is shared
and append-only, and whatever M17's own work writes to it today must not
look like a pause in these windows.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fake_gmail import FakeMailbox

from app.channel.channels import Channels
from app.config import Settings
from app.google.gmail import GmailClient
from app.mail import feed, quota
from app.mail import recall as recall_module
from app.mail.messages import apply_labels
from app.mail.quota import Pacer
from app.mail.recall import (
    RecallResult,
    SyncBusyError,
    check,
    held_intervals,
    recall,
    run_daily,
    send_alerts,
)
from app.mail.sync import sync_lock
from app.store.job_runs import JobRuns
from app.store.ledger import MessageLedger, MessageStatus

pytestmark = pytest.mark.integration

ME = "me@example.com"
NOW = datetime(2020, 3, 10, 12, 0, tzinfo=UTC)
PRIMARY = {"INBOX", "CATEGORY_PERSONAL"}


@pytest.fixture
def mail(conn: psycopg.Connection) -> Iterator[psycopg.Connection]:
    for table in ("gmail_messages", "gmail_cursors", "gmail_fetch_queue", "alerts_sent"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute(
        """
        INSERT INTO gmail_cursors (account, history_id, feed_from, backfill_until, caught_up_at)
        VALUES (%s, '1', %s, %s, %s)
        """,
        (ME, NOW - timedelta(days=30), NOW - timedelta(days=30), NOW),
    )
    yield conn


def _store(
    conn: psycopg.Connection,
    message_id: str,
    *,
    at: datetime,
    category: str = "primary",
    labels: set[str] = PRIMARY,
) -> None:
    """A row stored at `at`, its stall clock started then, as `store` would
    have if it meets the feed's rule."""
    primary = category == "primary"
    conn.execute(
        """
        INSERT INTO gmail_messages (account, message_id, thread_id, internal_at, label_ids,
                                    direction, to_self, category, has_list_unsubscribe,
                                    arrived_via, first_seen_at, updated_at, offered_since)
        VALUES (%s, %s, %s, %s, %s, 'in', false, %s, false, 'history', %s, %s, %s)
        """,
        (
            ME,
            message_id,
            f"t-{message_id}",
            at,
            sorted(labels) if primary else [f"CATEGORY_{category.upper()}"],
            category,
            at,
            at,
            at if primary else None,
        ),
    )


def _client(box: FakeMailbox) -> GmailClient:
    """As the recall's own: charged to the sync's share."""
    return GmailClient(box, pacer=Pacer(), share=quota.SYNC, retry_for=0)


def _recall(conn: psycopg.Connection, box: FakeMailbox) -> RecallResult:
    return recall(conn, _client(box), account=ME, owners=(ME,), now=NOW)


def _audit(conn: psycopg.Connection, kind: str, at: datetime) -> None:
    conn.execute("INSERT INTO audit_log (kind, at) VALUES (%s, %s)", (kind, at))


# --- sync recall ------------------------------------------------------------------


def test_a_missing_id_is_stored_and_fed_and_gives_one_alert(mail: psycopg.Connection) -> None:
    box = FakeMailbox()
    box.put("stored", labels=PRIMARY, at=NOW - timedelta(hours=5))
    box.put("missed", labels=PRIMARY, at=NOW - timedelta(hours=10))
    box.put("outside", labels=PRIMARY, at=NOW - timedelta(hours=1))  # still the sync's turn
    _store(mail, "stored", at=NOW - timedelta(hours=5))
    MessageLedger(mail).claim("stored", "stored")  # processed: the feed is fine

    result = _recall(mail, box)

    assert (result.listed, result.missed, result.repaired) == (2, 1, 1)
    row = mail.execute(
        "SELECT arrived_via FROM gmail_messages WHERE message_id = 'missed'"
    ).fetchone()
    assert row == ("recall",)
    assert "missed" in feed.candidates(mail, 10, now=NOW)  # repaired inbound mail is fed
    assert result.alerts() == ["mail_sync_missed"]

    phone = Phone()
    for _ in range(2):  # a second check the same day sends nothing more
        send_alerts(mail, result.alerts(), channels=Channels([phone]), day="2020-03-10")
    assert phone.alerted == ["mail_sync_missed"]


@dataclass
class Phone:
    """A channel that hears alerts: a stand-in for web push or Telegram."""

    name: str = "web_push"
    delivers: bool = True
    alerted: list[str] = field(default_factory=list)

    def announce_proposal(self, record: Any) -> None:
        raise AssertionError("the recall announces no proposal")

    def alert(self, code: str) -> bool:
        self.alerted.append(code)
        return self.delivers


def test_the_daily_check_alerts_through_the_channels_once_a_day(
    mail: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_daily` end to end: the sync's own client, the configured
    channels, and `alerts_sent` keeping each channel to one alert a day."""
    box = FakeMailbox()
    box.put("missed", labels=PRIMARY, at=NOW - timedelta(hours=10))
    phone, telegram = Phone(), Phone(name="telegram", delivers=False)
    settings = Settings(_env_file=None, database_url="postgresql://unused", gemini_api_key="k")
    monkeypatch.setattr(recall_module, "_connect", lambda url: nullcontext(mail))
    monkeypatch.setattr(recall_module, "sync_client", lambda settings: _client(box))
    monkeypatch.setattr(
        recall_module, "configured_channels", lambda settings: Channels([phone, telegram])
    )

    first = run_daily(settings, now=NOW)
    box.put("missed-again", labels=PRIMARY, at=NOW - timedelta(hours=9))
    second = run_daily(settings, now=NOW)  # the same day, another shortfall

    assert first is not None and first.alerts() == ["mail_sync_missed"]
    assert second is not None and second.alerts() == ["mail_sync_missed"]
    # Delivered once by the phone; Telegram did not deliver, so it is asked again.
    assert phone.alerted == ["mail_sync_missed"]
    assert telegram.alerted == ["mail_sync_missed", "mail_sync_missed"]
    sent = mail.execute("SELECT code, subject, channel FROM alerts_sent").fetchall()
    assert sent == [("mail_sync_missed", "2020-03-10", "web_push")]


def test_mail_the_sync_already_knows_about_is_not_a_miss(mail: psycopg.Connection) -> None:
    """Queued for a fetch (a catch-up's, or one that failed), or older than
    the backfill has reached: the sync has it in hand, and fetching it here
    would only spend its quota."""
    mail.execute(
        "UPDATE gmail_cursors SET feed_from = %s, backfill_until = %s",
        (NOW - timedelta(hours=3), NOW - timedelta(hours=12)),
    )
    box = FakeMailbox()
    box.put("queued", labels=PRIMARY, at=NOW - timedelta(hours=5))
    box.put("not-backfilled-yet", labels=PRIMARY, at=NOW - timedelta(hours=20))
    box.put("missed", labels=PRIMARY, at=NOW - timedelta(hours=6))
    mail.execute("INSERT INTO gmail_fetch_queue (message_id, reason) VALUES ('queued', 'catch_up')")

    result = _recall(mail, box)

    assert (result.missed, result.repaired) == (1, 1)
    assert box.fetched() == ["missed"]


def test_mail_stored_while_the_recall_looked_is_not_a_miss(mail: psycopg.Connection) -> None:
    """A store that comes back unchanged means the row was there after all."""
    box = FakeMailbox()
    box.put("raced", labels=PRIMARY, at=NOW - timedelta(hours=5))
    fetch = box._get

    def stored_meanwhile(**kwargs: Any) -> Any:
        _store(mail, kwargs["id"], at=NOW - timedelta(hours=5))
        return fetch(**kwargs)

    box._get = stored_meanwhile  # type: ignore[method-assign]

    assert _recall(mail, box).missed == 0


def test_the_recall_waits_for_the_syncs_lock_and_gives_up_in_time(
    mail: psycopg.Connection, migrated_database: str
) -> None:
    """A sync run and the recall never interleave; one that held the lock too
    long fails the recall, and the hourly job tries again."""
    with (
        psycopg.connect(migrated_database, autocommit=True) as other,
        sync_lock(other, wait=False) as held,
    ):
        assert held
        with pytest.raises(SyncBusyError):
            check(mail, _client(FakeMailbox()), Channels([]), now=NOW, lock_wait=0.3)

    assert check(mail, _client(FakeMailbox()), Channels([]), now=NOW).alerts() == []


def test_job_runs_say_whether_the_days_recall_has_completed(
    mail: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hourly job reads this to recall once a day (`app.jobs.scheduler`):
    a failed attempt does not count, and the next hour tries again."""
    from app.jobs import scheduler

    mail.execute("DELETE FROM job_runs WHERE job LIKE 'mail_recall%%'")
    monkeypatch.setattr(scheduler, "connect", lambda url, **kwargs: nullcontext(mail))
    settings = Settings(_env_file=None, database_url="postgresql://unused", gemini_api_key="k")
    due = datetime(2020, 3, 10, 5, 15, tzinfo=UTC)
    runs = JobRuns(mail)

    runs.record("mail_recall", due, due, ok=False, error="RuntimeError")
    runs.record("mail_recall_sync", due - timedelta(days=1), due - timedelta(days=1), ok=True)
    assert not scheduler._recalled_since(settings, due)

    runs.record("mail_recall_sync", due + timedelta(hours=1), due + timedelta(hours=1), ok=False)
    assert scheduler._recalled_since(settings, due)  # completed, whatever it found


def test_a_clean_day_raises_nothing(mail: psycopg.Connection) -> None:
    box = FakeMailbox()
    box.put("stored", labels=PRIMARY, at=NOW - timedelta(hours=5))
    _store(mail, "stored", at=NOW - timedelta(hours=5))
    MessageLedger(mail).claim("stored", "stored")

    result = _recall(mail, box)

    assert result.alerts() == []
    assert (result.sync_ok, result.feed_ok, result.categories_ok) == (True, True, True)


# --- feed recall ----------------------------------------------------------------------


def test_a_stalled_feed_raises_its_alert(mail: psycopg.Connection) -> None:
    """A row that has met the rule for over an hour with no ledger row."""
    _store(mail, "waiting", at=NOW - timedelta(hours=3))

    result = _recall(mail, FakeMailbox())

    assert (result.eligible, result.stalled) == (1, 1)
    assert result.alerts() == ["mail_feed_stalled"]


@pytest.mark.parametrize(
    "events",
    [
        [("paused", timedelta(hours=4))],  # paused, and still paused
        [("budget_exhausted", timedelta(hours=4))],  # the cap stopped work
        [("paused", timedelta(days=40))],  # paused long before the window, never resumed
    ],
)
def test_a_pause_or_a_stopped_cap_is_not_a_stall(
    mail: psycopg.Connection, events: list[tuple[str, timedelta]]
) -> None:
    _store(mail, "waiting", at=NOW - timedelta(hours=3))
    for kind, ago in events:
        _audit(mail, kind, NOW - ago)

    result = _recall(mail, FakeMailbox())

    assert (result.stalled, result.held) == (0, 1)
    assert result.alerts() == []


def test_a_row_the_queue_holds_back_for_over_six_hours_has_stalled(
    mail: psycopg.Connection,
) -> None:
    """The feed holds a row back while its fetch is queued. That was
    invisible here, so a fetch that never answered held it for ever."""
    _store(mail, "held-long", at=NOW - timedelta(hours=10))
    _store(mail, "held-briefly", at=NOW - timedelta(hours=10))
    mail.execute(
        """
        INSERT INTO gmail_fetch_queue (message_id, reason, queued_at)
        VALUES ('held-long', 'refetch', %s), ('held-briefly', 'refetch', %s)
        """,
        (NOW - timedelta(hours=7), NOW - timedelta(hours=5)),
    )

    result = _recall(mail, FakeMailbox())

    assert (result.eligible, result.stalled) == (0, 1)
    assert result.alerts() == ["mail_feed_stalled"]


def test_reading_a_stalled_message_does_not_hide_the_stall(mail: psycopg.Connection) -> None:
    """The clock used to restart at any label change, so opening a stalled
    message on the phone just before the check hid it."""
    _store(mail, "waiting", at=NOW - timedelta(hours=3), labels=PRIMARY | {"UNREAD"})
    apply_labels(mail, ME, "waiting", removed=frozenset({"UNREAD"}))

    assert _recall(mail, FakeMailbox()).stalled == 1


def test_mail_that_only_now_meets_the_rule_has_not_stalled(mail: psycopg.Connection) -> None:
    """Moved out of spam a moment ago: it has met the rule only since."""
    _store(mail, "rescued", at=NOW - timedelta(hours=3), labels=PRIMARY | {"SPAM"})
    mail.execute("UPDATE gmail_messages SET offered_since = NULL WHERE message_id = 'rescued'")
    apply_labels(mail, ME, "rescued", removed=frozenset({"SPAM"}))

    result = _recall(mail, FakeMailbox())

    assert (result.eligible, result.stalled) == (1, 0)


def test_a_pause_that_ended_before_the_row_arrived_excuses_nothing(
    mail: psycopg.Connection,
) -> None:
    _store(mail, "waiting", at=NOW - timedelta(hours=3))
    _audit(mail, "paused", NOW - timedelta(hours=9))
    _audit(mail, "resumed", NOW - timedelta(hours=8))

    assert _recall(mail, FakeMailbox()).stalled == 1


def test_a_short_pause_inside_a_long_stall_is_still_a_stall(mail: psycopg.Connection) -> None:
    """A hold is taken off a row's wait, not used to excuse the whole of it:
    five minutes' pause cannot hide a day without the feed."""
    _store(mail, "waiting", at=NOW - timedelta(hours=20))
    _audit(mail, "paused", NOW - timedelta(hours=10))
    _audit(mail, "resumed", NOW - timedelta(hours=10) + timedelta(minutes=5))

    result = _recall(mail, FakeMailbox())

    assert (result.stalled, result.held) == (1, 0)


def test_a_row_whose_wait_was_mostly_held_is_not_a_stall(mail: psycopg.Connection) -> None:
    """Under an hour of the wait fell outside the pause and the cap."""
    _store(mail, "waiting", at=NOW - timedelta(hours=5))
    _audit(mail, "paused", NOW - timedelta(hours=5))
    _audit(mail, "budget_exhausted", NOW - timedelta(hours=4))
    _audit(mail, "resumed", NOW - timedelta(hours=2))
    _audit(mail, "budget_ok", NOW - timedelta(minutes=30))

    result = _recall(mail, FakeMailbox())

    assert (result.stalled, result.held) == (0, 1)


def test_a_pause_and_a_cap_at_once_are_counted_once(mail: psycopg.Connection) -> None:
    """Two hours paused and capped together are two hours held, not four:
    three of the five hours waited were the feed's own."""
    _store(mail, "waiting", at=NOW - timedelta(hours=5))
    _audit(mail, "paused", NOW - timedelta(hours=5))
    _audit(mail, "budget_exhausted", NOW - timedelta(hours=5))
    _audit(mail, "resumed", NOW - timedelta(hours=3))
    _audit(mail, "budget_ok", NOW - timedelta(hours=3))

    assert _recall(mail, FakeMailbox()).stalled == 1


def test_a_cap_ends_with_its_month(mail: psycopg.Connection) -> None:
    exhausted = datetime(2020, 1, 20, tzinfo=UTC)
    _audit(mail, "budget_exhausted", exhausted)

    assert held_intervals(mail, NOW - timedelta(days=60), NOW) == [
        (exhausted, datetime(2020, 2, 1, tzinfo=UTC))
    ]


@pytest.mark.parametrize("kind", ["budget_ok", "budget_warning"])
def test_a_cap_ends_when_spending_starts_again(mail: psycopg.Connection, kind: str) -> None:
    """A raised cap, or a new month's: `budget_ok` closes it as a warning does."""
    exhausted = NOW - timedelta(hours=6)
    _audit(mail, "budget_exhausted", exhausted)
    _audit(mail, kind, exhausted + timedelta(hours=1))

    assert held_intervals(mail, NOW - timedelta(days=1), NOW) == [
        (exhausted, exhausted + timedelta(hours=1))
    ]


def test_a_model_with_no_price_holds_the_feed_until_the_state_changes(
    mail: psycopg.Connection,
) -> None:
    """New work stops while a model in use has no price (M17, 17.16), until a
    deploy prices it: the next state the watch records, whatever it is. Not
    the month's end: a price does not come back on the 1st."""
    unpriced = NOW - timedelta(hours=6)
    _audit(mail, "budget_unpriced", unpriced)
    _audit(mail, "budget_ok", unpriced + timedelta(hours=2))

    assert held_intervals(mail, NOW - timedelta(days=1), NOW) == [
        (unpriced, unpriced + timedelta(hours=2))
    ]


def test_a_model_still_without_a_price_holds_the_feed_until_now(
    mail: psycopg.Connection,
) -> None:
    unpriced = datetime(2020, 2, 25, tzinfo=UTC)
    _audit(mail, "budget_unpriced", unpriced)

    assert held_intervals(mail, NOW - timedelta(days=30), NOW) == [(unpriced, NOW)]


def test_a_model_without_a_price_for_over_a_month_still_holds_the_feed(
    mail: psycopg.Connection,
) -> None:
    """Its stop began before the month the recall reads back: still in force."""
    unpriced = NOW - timedelta(days=40)
    _audit(mail, "budget_unpriced", unpriced)

    assert held_intervals(mail, NOW - timedelta(days=1), NOW) == [(unpriced, NOW)]


def test_a_too_old_record_for_day_old_mail_raises_the_feed_alert(
    mail: psycopg.Connection,
) -> None:
    """The age rule is wrong if it skips mail that was hours old."""
    _store(mail, "young", at=NOW - timedelta(hours=6))
    ledger = MessageLedger(mail)
    ledger.claim("young", "young")
    ledger.mark("young", MessageStatus.SKIPPED, error=feed.TOO_OLD)
    mail.execute(
        "UPDATE processed_messages SET created_at = %s WHERE gmail_message_id = 'young'",
        (NOW - timedelta(hours=5),),
    )

    result = _recall(mail, FakeMailbox())

    assert result.too_old_young == 1
    assert result.alerts() == ["mail_feed_stalled"]


# --- category agreement -----------------------------------------------------------------


def test_category_disagreement_is_caught_both_ways(mail: psycopg.Connection) -> None:
    box = FakeMailbox()
    box.put("gmail-primary", labels=PRIMARY, at=NOW - timedelta(hours=5))
    box.put("gmail-updates", labels={"CATEGORY_UPDATES"}, at=NOW - timedelta(hours=5))
    box.put("agrees", labels={"CATEGORY_FORUMS"}, at=NOW - timedelta(hours=5))
    _store(mail, "gmail-primary", at=NOW - timedelta(hours=5), category="updates")
    _store(mail, "gmail-updates", at=NOW - timedelta(hours=5), category="primary")
    _store(mail, "agrees", at=NOW - timedelta(hours=5), category="forums")
    for message_id in ("gmail-primary", "gmail-updates", "agrees"):
        MessageLedger(mail).claim(message_id, message_id)

    result = _recall(mail, box)

    assert (result.categories_checked, result.categories_mismatched) == (3, 2)
    assert result.alerts() == ["mail_sync_missed"]


def test_the_summary_is_counts_and_times_only(mail: psycopg.Connection) -> None:
    summary: dict[str, Any] = _recall(mail, FakeMailbox()).summary()

    assert summary["since"] == (NOW - timedelta(hours=26)).isoformat()
    assert all(isinstance(value, int | str) for value in summary.values())
