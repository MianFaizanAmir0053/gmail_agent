"""What `/health` knows about the poller, without asking the database.

`/health` is public and polled every 30 seconds by the platform and every few
minutes by an uptime monitor. A database round-trip per request would add
load and keep a scale-to-zero database awake. It would also prove less than
it seems: every poll already connects to the database, so "the last poll
succeeded" covers the database as well.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.google.tokens import RefreshOutcome

STALL_INTERVALS = 3
"""How many poll intervals may pass without a success before health degrades.
One missed tick is noise; three in a row is an outage worth paging for."""

FETCHES_JUDGED_FROM = 3
"""Fetches a mail sync run must have tried before all of them failing counts
against it: in its own report (`job_runs`) and here."""

DECISION_STUCK_AFTER = timedelta(hours=1)
"""How long a decision may stay open before health degrades. Three failed
attempts settle one within about twelve minutes, so an hour means the worker
itself is stuck."""


@dataclass
class Liveness:
    booted_at: datetime
    last_poll_at: datetime | None = None
    last_poll_ok_at: datetime | None = None
    oldest_open_decision_at: datetime | None = None
    """When the oldest open decision was made, as the decisions job last saw
    it. A decision recorded in this process sets it too, if the job saw none:
    a job that hangs stops refreshing it, and the hang then ages into a 503."""

    push_subscriptions: int | None = None
    """Browsers subscribed to push, as the hourly token check last counted.
    None until the first count; zero means nobody would hear a push."""

    unconfirmed_writes: int | None = None
    """Open decisions whose calendar write could not be confirmed (M17, D3),
    as the decisions job last counted. They ask Google again every hour, so
    they are counted here rather than as stuck."""

    budget_state: str | None = None
    """Where this month's model spending stands (M17, D5), as the watch job
    last read it. An exhausted budget is not an outage."""

    month_spend_usd: Decimal | None = None

    def subscriptions_counted(self, count: int) -> None:
        self.push_subscriptions = count

    def writes_checked(self, unconfirmed: int) -> None:
        self.unconfirmed_writes = unconfirmed

    def budget_checked(self, state: str, month_spend_usd: Decimal) -> None:
        self.budget_state = state
        self.month_spend_usd = month_spend_usd

    def decisions_checked(self, oldest_open: datetime | None) -> None:
        self.oldest_open_decision_at = oldest_open

    def decision_recorded(self, at: datetime) -> None:
        if self.oldest_open_decision_at is None:
            self.oldest_open_decision_at = at

    def decision_stuck(self, now: datetime) -> bool:
        oldest = self.oldest_open_decision_at
        return oldest is not None and now - oldest > DECISION_STUCK_AFTER

    def poll_finished(self, *, ok: bool, at: datetime) -> None:
        self.last_poll_at = at
        if ok:
            self.last_poll_ok_at = at

    def poll_overdue(self, now: datetime, interval: timedelta) -> bool:
        """True once no poll has succeeded for `STALL_INTERVALS` intervals.

        Before the first success the clock starts one interval after boot,
        because that is when the first tick fires. A fresh process is not
        unhealthy merely for being new.
        """
        reference = self.last_poll_ok_at or (self.booted_at + interval)
        return now - reference > STALL_INTERVALS * interval


LIVENESS = Liveness(booted_at=datetime.now(UTC))
"""The running process's own record. Written by the scheduler's poll job,
read by `/health`."""


@dataclass
class RefreshEvidence:
    last_ok_at: datetime | None = None
    rejected: bool = False


@dataclass
class TokenEvidence:
    """What this process has seen of each token's refreshes, keyed by the
    token's `issued_at`. Seeded from `job_runs` at boot, so a restart does not
    forget a confirmation that took a week to earn."""

    _by_token: dict[datetime, RefreshEvidence] = field(default_factory=dict)

    def record(self, outcome: RefreshOutcome) -> None:
        evidence = self._by_token.setdefault(outcome.issued_at, RefreshEvidence())
        if outcome.ok:
            evidence.last_ok_at = outcome.at
            evidence.rejected = False
        elif outcome.rejected:
            evidence.rejected = True

    def seed(self, issued_at: datetime, *, last_ok_at: datetime | None, rejected: bool) -> None:
        self._by_token[issued_at] = RefreshEvidence(last_ok_at=last_ok_at, rejected=rejected)

    def for_token(self, issued_at: datetime) -> RefreshEvidence:
        return self._by_token.get(issued_at, RefreshEvidence())


TOKEN_EVIDENCE = TokenEvidence()


@dataclass
class MailSyncLiveness:
    """What `/health` knows about the mail sync (M20, D6), without asking
    the database. Written by the sync and recall jobs.

    Judged by the last pass that reached the end of history. A run that only
    made headway through a backlog does not count: a sync that is alive but
    hours behind is as blind as a dead one.
    """

    booted_at: datetime
    caught_up_at: datetime | None = None
    fetches_failing_since: datetime | None = None
    """When runs began to fail every fetch they tried: set by a run that tried
    at least three and fetched none, cleared by the next run that fetched
    one. Each pass still reaches the end of history meanwhile."""

    status: dict[str, Any] | None = None
    """The records as the last run left them: counts and times only."""

    recall: dict[str, Any] | None = None
    """The last daily recall, as counts."""

    recall_failure: dict[str, str] | None = None
    """When the latest recall attempt failed, and the exception's type; None
    once one completes. The hourly job tries again."""

    def reached_end(self, at: datetime) -> None:
        self.caught_up_at = at

    def recall_finished(self, summary: dict[str, Any]) -> None:
        self.recall = summary
        self.recall_failure = None

    def recall_failed(self, *, at: datetime, error: str) -> None:
        self.recall_failure = {"at": at.isoformat(), "error": error}

    def fetches_tried(self, *, tried: int, failed: int, at: datetime) -> None:
        """What a run's fetches came to. A run that tried too few to judge,
        all of them failing, changes nothing."""
        if failed < tried:
            self.fetches_failing_since = None
        elif tried >= FETCHES_JUDGED_FROM and self.fetches_failing_since is None:
            self.fetches_failing_since = at

    def overdue(self, now: datetime, interval: timedelta) -> bool:
        """True once no pass has reached the end for `STALL_INTERVALS`
        intervals. Before the first, the clock starts one interval after
        boot, when the first tick fires, as poll's does."""
        reference = self.caught_up_at or (self.booted_at + interval)
        return now - reference > STALL_INTERVALS * interval

    def fetches_failing(self, now: datetime, interval: timedelta) -> bool:
        """True once every fetch has failed for `STALL_INTERVALS` intervals."""
        since = self.fetches_failing_since
        return since is not None and now - since > STALL_INTERVALS * interval

    def report(self, now: datetime) -> dict[str, Any]:
        """The owner's view: the cursor's age, whether every fetch is failing,
        the backfill's, the queue's and any catch-up's progress, the
        unreadable and too-old counts, the table's size, the last recall, and
        whether the latest attempt at one failed."""
        age = round((now - self.caught_up_at).total_seconds()) if self.caught_up_at else None
        failing = self.fetches_failing_since
        return {
            "cursor_age_seconds": age,
            "fetches_failing_since": failing.isoformat() if failing else None,
            **(self.status or {}),
            "last_recall": self.recall,
            "last_recall_failure": self.recall_failure,
        }


MAIL_SYNC = MailSyncLiveness(booted_at=LIVENESS.booted_at)

_HANDLER_MARK = "_mailagent_handler"


def configure_logging() -> None:
    """Make the app's own INFO lines visible when running under uvicorn.

    Uvicorn configures its own loggers and leaves the root logger without a
    handler, so every `log.info` in `app.*` -- including the scheduler's record
    of each poll -- was silently dropped. Idempotent: a second call adds no
    second handler.
    """
    app_logger = logging.getLogger("app")
    app_logger.setLevel(logging.INFO)
    if any(getattr(handler, _HANDLER_MARK, False) for handler in app_logger.handlers):
        return

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    setattr(handler, _HANDLER_MARK, True)
    app_logger.addHandler(handler)
