"""The shared Gmail pacer (M20, D3, "Quota").

Gmail allows 6,000 quota units per user per minute, and everything that
touches the mailbox draws on the same minute: the sync, the pipeline's
fetches, M17's thread reads. A 429 does not say whose turn it was, so one
pacer per process counts what each call costs, by method, over the last
sixty seconds.

The sync has a share of its own: at most 2,000 units a minute, a third, so
the rest is always left for the pipeline and M17. When its share is spent,
the sync is told (`ShareExhaustedError`) rather than kept waiting: it stops,
and the next tick carries on. Everyone else waits for the minute to free up,
which costs seconds where a 429 would cost a retry.

Another process -- the CLI on the instance, M15's `measure` run locally --
has a pacer of its own. The advisory lock keeps two syncs from running at
once, and the runbook keeps `measure` away from a backfill.
"""

from __future__ import annotations

import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

UNITS: Mapping[str, int] = MappingProxyType(
    {
        "getProfile": 1,
        "history.list": 2,
        "messages.list": 5,
        "messages.get": 20,
        "threads.list": 10,
        "threads.get": 40,
    }
)
"""What each method costs, from Gmail's usage-limits page (checked 2026-10-01).
A method missing here is refused: every call is counted, so a call nobody
priced is a bug, not a free one."""

PER_MINUTE = 6_000
"""Gmail's quota per user per minute, shared by every caller."""

SYNC = "sync"
"""The sync's share (`GmailClient(share=SYNC)`)."""

SYNC_PER_MINUTE = 2_000
"""A third of the minute: what the sync, its queue, its catch-up and its
backfill may spend between them."""

WINDOW = 60.0
"""Seconds a spend counts for."""


class ShareExhaustedError(RuntimeError):
    """A share has spent its minute. Stop, and leave the rest to the next tick."""


class PacingTimeoutError(TimeoutError):
    """The minute stays full for longer than the caller can wait. A timeout,
    so the Gmail client treats it as Gmail being unavailable for everyone."""


@dataclass(frozen=True, slots=True)
class _Spend:
    at: float
    method: str
    units: int
    share: str | None


class Pacer:
    def __init__(
        self,
        *,
        per_minute: int = PER_MINUTE,
        shares: Mapping[str, int] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._per_minute = per_minute
        self._shares = dict(shares) if shares is not None else {SYNC: SYNC_PER_MINUTE}
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._spent: deque[_Spend] = deque()

    def spend(
        self, method: str, *, share: str | None = None, wait_for: float | None = None
    ) -> None:
        """Count one call of `method`, waiting if the minute is full.

        A call in a share raises `ShareExhaustedError` instead of waiting, both
        when its share is spent and when the whole minute is: the sync yields.
        Anyone else waits, for at most `wait_for` seconds when given: a wait
        that would run past it raises `PacingTimeoutError` at once, so a
        caller's own deadline holds (the pipeline's fetch has one).
        """
        units = UNITS[method]
        latest = None if wait_for is None else self._clock() + wait_for
        while True:
            with self._lock:
                now = self._clock()
                self._forget(now)
                total = sum(spend.units for spend in self._spent)
                if share is not None and (
                    self._spent_by(share) + units > self._shares[share]
                    or total + units > self._per_minute
                ):
                    raise ShareExhaustedError(share)
                if total + units <= self._per_minute:
                    self._spent.append(_Spend(now, method, units, share))
                    return
                # Until the oldest spend leaves the window. Slept outside the
                # lock, so other callers can still be told their own answer.
                wait = self._spent[0].at + WINDOW - now
                if latest is not None and now + wait > latest:
                    raise PacingTimeoutError(method)
            self._sleep(max(wait, 0.01))

    def available(self, share: str | None = None) -> int:
        """Units that could be spent now without waiting or being refused."""
        with self._lock:
            self._forget(self._clock())
            left = self._per_minute - sum(spend.units for spend in self._spent)
            if share is not None:
                left = min(left, self._shares[share] - self._spent_by(share))
            return max(left, 0)

    def by_method(self) -> dict[str, int]:
        """Units spent in the last minute, by method."""
        with self._lock:
            self._forget(self._clock())
            counted: Counter[str] = Counter()
            for spend in self._spent:
                counted[spend.method] += spend.units
            return dict(counted)

    def _spent_by(self, share: str) -> int:
        return sum(spend.units for spend in self._spent if spend.share == share)

    def _forget(self, now: float) -> None:
        while self._spent and self._spent[0].at <= now - WINDOW:
            self._spent.popleft()


PACER = Pacer()
"""The process's pacer. A `GmailClient` built without one uses this, so every
Gmail call in the process -- including the pipeline's, whose client is built
in `app/graph/runner.py` -- counts against the same minute."""
