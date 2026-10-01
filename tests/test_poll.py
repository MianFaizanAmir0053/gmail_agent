"""One polling pass, with the ledger, the cursor, the feed and the graph faked."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from app.channel.park import proposal_from
from app.google.gmail import MessageGoneError
from app.graph.runner import GraphSession
from app.jobs import poll
from app.mail import feed as real_feed
from app.store.ledger import MessageStatus


@dataclass
class FakeLedger:
    claimed: list[str] = field(default_factory=list)
    marks: list[tuple[str, MessageStatus, str | None]] = field(default_factory=list)

    def unseen(self, message_ids: list[str]) -> list[str]:
        return message_ids

    def claim(self, message_id: str, thread_id: str) -> bool:
        self.claimed.append(message_id)
        return True

    def mark(self, message_id: str, status: MessageStatus, *, error: str | None = None) -> None:
        self.marks.append((message_id, status, error))

    def get(self, message_id: str) -> None:
        return None


@dataclass
class FakeFeed:
    """Stands in for `app.mail.feed`, whose SQL is tested on Postgres. Off
    until a test turns it on: before the first sync run, poll reads the
    newest unread page as it always did."""

    on: bool = False
    waiting: list[str] = field(default_factory=list)
    too_old: list[str] = field(default_factory=list)
    GONE: str = real_feed.GONE
    TOO_OLD: str = real_feed.TOO_OLD

    def active(self, conn: object) -> bool:
        return self.on

    def candidates(self, conn: object, limit: int) -> list[str]:
        return self.waiting[:limit]

    def record_too_old(self, conn: object) -> list[str]:
        return self.too_old


@pytest.fixture(autouse=True)
def feed(monkeypatch: pytest.MonkeyPatch) -> FakeFeed:
    fake = FakeFeed()
    monkeypatch.setattr(poll, "feed", fake)
    return fake


@dataclass
class FakeCursor:
    values: list[str] = field(default_factory=list)

    def set(self, history_id: str) -> None:
        self.values.append(history_id)


@dataclass
class FakeGmail:
    unread: list[str]
    asked: list[int] = field(default_factory=list)

    def list_unread(self, max_results: int = 10) -> list[str]:
        self.asked.append(max_results)
        return self.unread[:max_results]

    def current_history_id(self) -> str:
        return "h42"


@dataclass
class FakeDeps:
    gmail: FakeGmail


@dataclass
class FakeSession:
    unread: list[str]
    on_start: Callable[[str], None] = lambda message_id: None
    parked: dict[str, str] = field(default_factory=dict)
    started: list[str] = field(default_factory=list)
    conn: object = field(default_factory=object)
    gmail: FakeGmail = field(init=False)

    def __post_init__(self) -> None:
        self.gmail = FakeGmail(self.unread)

    @property
    def deps(self) -> FakeDeps:
        return FakeDeps(self.gmail)

    def start(self, message_id: str, thread_id: str) -> None:
        self.started.append(message_id)
        self.on_start(message_id)

    def pending(self, message_id: str) -> dict[str, Any] | None:
        if message_id not in self.parked:
            return None
        return {"proposed": {"title": self.parked[message_id]}}


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch) -> FakeLedger:
    fake = FakeLedger()
    monkeypatch.setattr(poll, "MessageLedger", lambda conn: fake)
    return fake


@pytest.fixture
def cursor(monkeypatch: pytest.MonkeyPatch) -> FakeCursor:
    fake = FakeCursor()
    monkeypatch.setattr(poll, "SyncCursor", lambda conn: fake)
    return fake


@dataclass
class Parks:
    recorded: list[str] = field(default_factory=list)
    announced: list[str] = field(default_factory=list)


@pytest.fixture
def parks(monkeypatch: pytest.MonkeyPatch) -> Parks:
    """Stands in for the park step, whose writes are tested on Postgres."""
    fake = Parks()

    def record_park(
        session: Any, message_id: str, pending: dict[str, Any], *, announce: Any = None
    ) -> None:
        fake.recorded.append(message_id)
        if announce is not None:
            announce(proposal_from(message_id, pending, 1))

    monkeypatch.setattr(poll, "record_park", record_park)
    return fake


def test_a_normal_pass_claims_every_unread_message(ledger: FakeLedger, cursor: FakeCursor) -> None:
    session = FakeSession(unread=["a", "b", "c"])

    result = poll.poll_once(cast(GraphSession, session), 10, stop=threading.Event())

    assert result.started == 3
    assert ledger.claimed == ["a", "b", "c"]
    assert cursor.values == ["h42"]


def test_a_shutdown_mid_pass_claims_nothing_further(ledger: FakeLedger, cursor: FakeCursor) -> None:
    """The message in hand finishes; nothing new is taken that a kill could strand."""
    stop = threading.Event()
    session = FakeSession(unread=["a", "b", "c"], on_start=lambda _: stop.set())

    result = poll.poll_once(cast(GraphSession, session), 10, stop=stop)

    assert result.started == 1
    assert ledger.claimed == ["a"]


def test_a_stopped_pass_does_not_advance_the_cursor(ledger: FakeLedger, cursor: FakeCursor) -> None:
    """Unprocessed mail is still behind the cursor; moving it would skip that mail."""
    stop = threading.Event()
    session = FakeSession(unread=["a", "b"], on_start=lambda _: stop.set())

    poll.poll_once(cast(GraphSession, session), 10, stop=stop)

    assert cursor.values == []


def test_a_parked_message_goes_through_the_park_step(
    ledger: FakeLedger, cursor: FakeCursor, parks: Parks
) -> None:
    """The ledger mark and the proposal row are written together (M16, D2)."""
    session = FakeSession(unread=["a", "b"], parked={"b": "Design review"})

    poll.poll_once(
        cast(GraphSession, session),
        10,
        stop=threading.Event(),
        announce=lambda record: parks.announced.append(record.message_id),
    )

    assert parks.recorded == ["b"]
    assert parks.announced == ["b"]


def test_poll_no_longer_talks_to_telegram_itself() -> None:
    """It announces through the channels it is given (M16, D7)."""
    names = set(vars(poll))
    assert not names & {"TelegramClient", "send_approval_card", "admin_chat_id", "_notify"}


def test_production_output_names_no_titles(
    ledger: FakeLedger,
    cursor: FakeCursor,
    parks: Parks,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Hosted logs sit outside the database's controls: ids and statuses only."""
    session = FakeSession(unread=["a"], parked={"a": "Salary review with HR"})

    poll.poll_once(cast(GraphSession, session), 10, stop=threading.Event(), show_titles=False)

    out = capsys.readouterr().out
    assert "AWAITING APPROVAL" in out
    assert "Salary review" not in out


def test_a_park_that_cannot_be_recorded_does_not_stop_the_pass(
    ledger: FakeLedger, cursor: FakeCursor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The thread is still parked; reconciliation records it. The rest of the
    batch should not wait an interval for that."""
    recorded: list[str] = []

    def record_park(session: Any, message_id: str, pending: Any, **kwargs: Any) -> None:
        if message_id == "a":
            raise RuntimeError("connection lost")
        recorded.append(message_id)

    monkeypatch.setattr(poll, "record_park", record_park)
    session = FakeSession(unread=["a", "b"], parked={"a": "One", "b": "Two"})

    result = poll.poll_once(cast(GraphSession, session), 10, stop=threading.Event())

    assert recorded == ["b"]
    # Counted, so the tick is not reported healthy.
    assert result.failed == 1


def test_reset_refuses_the_production_ledger() -> None:
    """Decisions are M24's evidence; a reset would try to erase them."""
    with pytest.raises(SystemExit):
        poll.reset(cast(Any, object()), app_env="prod")


# --- the feed (M20, D4) ------------------------------------------------------


def test_before_the_first_sync_run_poll_reads_the_newest_unread_page(
    ledger: FakeLedger, cursor: FakeCursor, feed: FakeFeed
) -> None:
    session = FakeSession(unread=["a", "b"])

    result = poll.poll_once(cast(GraphSession, session), 10, stop=threading.Event())

    assert session.gmail.asked == [10]
    assert ledger.claimed == ["a", "b"]
    assert cursor.values == ["h42"]
    assert result.seen == 2


def test_once_the_feed_has_started_poll_claims_its_candidates(
    ledger: FakeLedger, cursor: FakeCursor, feed: FakeFeed
) -> None:
    """New Primary mail, read or not. The unread page is not even asked for,
    and the old poller's cursor stays where the first sync run found it."""
    feed.on = True
    feed.waiting = ["x", "y", "z"]
    session = FakeSession(unread=["a"])

    result = poll.poll_once(cast(GraphSession, session), 2, stop=threading.Event())

    assert ledger.claimed == ["x", "y"]
    assert session.gmail.asked == []
    assert cursor.values == []
    assert (result.seen, result.started) == (2, 2)


def test_mail_too_old_when_reached_is_recorded_before_anything_is_claimed(
    ledger: FakeLedger,
    cursor: FakeCursor,
    feed: FakeFeed,
    capsys: pytest.CaptureFixture[str],
) -> None:
    feed.on = True
    feed.too_old = ["ancient"]
    session = FakeSession(unread=[])

    poll.poll_once(cast(GraphSession, session), 10, stop=threading.Event())

    assert session.started == []  # no model call
    assert "ancient  SKIPPED  too old when reached" in capsys.readouterr().out


def test_a_message_gone_before_its_turn_is_skipped_not_failed(
    ledger: FakeLedger, cursor: FakeCursor, feed: FakeFeed
) -> None:
    """Deleted between the sync seeing it and the graph fetching it."""
    feed.on = True
    feed.waiting = ["gone", "next"]

    def start(message_id: str) -> None:
        if message_id == "gone":
            raise MessageGoneError(message_id)

    session = FakeSession(unread=[], on_start=start)

    result = poll.poll_once(cast(GraphSession, session), 10, stop=threading.Event())

    assert ledger.marks == [("gone", MessageStatus.SKIPPED, "no longer in the mailbox")]
    assert session.started == ["gone", "next"]
    assert result.failed == 0


def test_messages_are_claimed_in_exactly_one_place() -> None:
    """M17's spend gate asks before each claim; there must be one to guard."""
    import inspect

    assert inspect.getsource(poll).count(".claim(") == 1
