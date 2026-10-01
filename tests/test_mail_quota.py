"""The shared Gmail pacer (M20, D3, "Quota").

Gmail allows 6,000 units per user per minute, shared by everything that
touches the mailbox. The sync may spend a third of it; everything else waits
its turn rather than meeting a 429.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.mail import quota
from app.mail.quota import UNITS, Pacer, ShareExhaustedError


@dataclass
class FakeClock:
    """Seconds that pass only when something sleeps."""

    now: float = 1000.0
    slept: list[float] = field(default_factory=list)

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _pacer(clock: FakeClock) -> Pacer:
    return Pacer(clock=clock, sleep=clock.sleep)


def test_the_published_prices() -> None:
    """Checked against Gmail's usage-limits page on 2026-10-01."""
    assert UNITS == {
        "getProfile": 1,
        "history.list": 2,
        "messages.list": 5,
        "messages.get": 20,
        "threads.list": 10,
        "threads.get": 40,
    }


def test_the_sync_is_held_to_two_thousand_units_a_minute() -> None:
    clock = FakeClock()
    pacer = _pacer(clock)

    for _ in range(100):  # 100 fetches at 20 units: the whole share
        pacer.spend("messages.get", share=quota.SYNC)

    with pytest.raises(ShareExhaustedError):
        pacer.spend("history.list", share=quota.SYNC)
    assert clock.slept == []  # the sync is told, never kept waiting

    clock.now += 60.0
    pacer.spend("messages.get", share=quota.SYNC)


def test_units_are_counted_by_method() -> None:
    clock = FakeClock()
    pacer = _pacer(clock)

    pacer.spend("messages.get", share=quota.SYNC)
    pacer.spend("messages.get")
    pacer.spend("history.list", share=quota.SYNC)
    pacer.spend("getProfile")

    assert pacer.by_method() == {"messages.get": 40, "history.list": 2, "getProfile": 1}
    assert pacer.available(quota.SYNC) == 2000 - 22
    assert pacer.available() == 6000 - 43


def test_spending_leaves_the_window_after_a_minute() -> None:
    clock = FakeClock()
    pacer = _pacer(clock)
    pacer.spend("messages.get", share=quota.SYNC)

    clock.now += 59.9
    assert pacer.available(quota.SYNC) == 1980
    clock.now += 0.2
    assert pacer.available(quota.SYNC) == 2000


def test_other_callers_wait_for_the_minute_rather_than_fail() -> None:
    """A 429 would only cost a retry; waiting a few seconds costs nothing."""
    clock = FakeClock()
    pacer = _pacer(clock)
    for _ in range(300):  # 6,000 units: the whole per-user minute
        pacer.spend("messages.get")

    pacer.spend("messages.get")

    assert clock.slept and sum(clock.slept) >= 60.0
    assert pacer.available() == 6000 - 20


def test_the_syncs_share_counts_toward_the_whole_minute() -> None:
    clock = FakeClock()
    pacer = _pacer(clock)
    for _ in range(100):
        pacer.spend("messages.get", share=quota.SYNC)

    for _ in range(200):  # the other 4,000
        pacer.spend("messages.get")
    assert clock.slept == []

    pacer.spend("getProfile")
    assert clock.slept  # the 6,001st unit waited


def test_the_sync_yields_when_others_have_spent_the_minute() -> None:
    clock = FakeClock()
    pacer = _pacer(clock)
    for _ in range(300):
        pacer.spend("messages.get")

    with pytest.raises(ShareExhaustedError):
        pacer.spend("getProfile", share=quota.SYNC)


def test_an_unpriced_method_is_refused() -> None:
    """Every call is counted, so a call nobody priced is a bug, not a freebie."""
    with pytest.raises(KeyError):
        _pacer(FakeClock()).spend("messages.send")


def test_the_process_has_one_pacer() -> None:
    assert isinstance(quota.PACER, Pacer)
