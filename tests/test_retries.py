"""Retries made visible (M15).

`call_with_retry` absorbs transient failures, which is the point of it, but it
used to absorb them without a trace: a model that needed three attempts per
call looked exactly like one that answered first time. Each retry is now
counted into the span the call ran in.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import psycopg
import pytest

from app.extraction.llm import LlmError, call_with_retry
from app.obs.trace import Tracer


class _TransientError(Exception):
    code = 503


def _flaky(failures: int) -> Callable[[], str]:
    remaining = [failures]

    def call() -> str:
        if remaining[0]:
            remaining[0] -= 1
            raise _TransientError("model overloaded")
        return "ok"

    return call


class _NullConn:
    """Swallows the span write; these tests read the span's own counters."""

    def execute(self, *args: Any, **kwargs: Any) -> None:
        return None


def _tracer() -> Tracer:
    return Tracer(cast(psycopg.Connection, _NullConn()))


def test_retries_are_counted_into_the_open_span() -> None:
    with _tracer().span("extract") as usage:
        assert call_with_retry(_flaky(2), what="m", base_delay=0) == "ok"

    assert usage.retries == 2


def test_retries_that_run_out_are_counted_too() -> None:
    with pytest.raises(LlmError), _tracer().span("extract") as usage:
        call_with_retry(_flaky(10), what="m", attempts=4, base_delay=0)

    assert usage.retries == 3


def test_retrying_outside_a_span_needs_no_tracing() -> None:
    """The eval harness and unit tests run the same code with nothing tracing."""
    assert call_with_retry(_flaky(1), what="m", base_delay=0) == "ok"


@pytest.mark.integration
def test_the_count_reaches_the_spans_table(conn: psycopg.Connection) -> None:
    tracer = Tracer(conn)
    tracer.start_run("m-retry")

    with tracer.span("extract"):
        call_with_retry(_flaky(2), what="m", base_delay=0)

    row = conn.execute(
        "SELECT retry_count FROM spans WHERE trace_id = %s", (tracer.trace_id,)
    ).fetchone()
    assert row == (2,)
