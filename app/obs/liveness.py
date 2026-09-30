"""What `/health` knows about the poller, without asking the database.

`/health` is public and polled every 30 seconds by the platform and every few
minutes by an uptime monitor. A database round-trip per request would add
load and keep a scale-to-zero database awake. It would also prove less than
it seems: every poll already connects to the database, so "the last poll
succeeded" covers the database as well.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

STALL_INTERVALS = 3
"""How many poll intervals may pass without a success before health degrades.
One missed tick is noise; three in a row is an outage worth paging for."""


@dataclass
class Liveness:
    booted_at: datetime
    last_poll_at: datetime | None = None
    last_poll_ok_at: datetime | None = None

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
