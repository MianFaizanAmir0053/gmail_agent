"""Repricing stored traces against a real Postgres. Skipped when none is reachable."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from app.jobs.reprice import reprice
from app.obs.pricing import cost_usd
from app.obs.trace import Tracer, record_llm_usage

pytestmark = pytest.mark.integration

TRIAGE = "gemini-3.5-flash-lite"
WRITTEN = datetime(2026, 8, 17, 9, 30, tzinfo=UTC)

INPUT_TOKENS, OUTPUT_TOKENS = 2_314, 47
"""One triage call, about the size of the classify runs recorded in M08."""

STALE = Decimal("0.000250")
"""What the placeholder table stored for that call: $0.10 in, $0.40 out per million."""

FRESH = cost_usd(TRIAGE, at=WRITTEN, input_tokens=INPUT_TOKENS, output_tokens=OUTPUT_TOKENS)


@pytest.fixture(autouse=True)
def _only_this_tests_traces(conn: psycopg.Connection) -> None:
    """The job reprices every span in the database, and tests share that database
    with development. Emptied inside `conn`'s rolled-back transaction, so real
    traces are back afterwards; they just cannot leak into the counts."""
    conn.execute("DELETE FROM runs")  # spans cascade


def _run(
    conn: psycopg.Connection, *, total: Decimal | None = None, finished: bool = True
) -> uuid.UUID:
    """A run as stored. Finished means finish_run wrote `ended_at` and `total`."""
    trace_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO runs (trace_id, gmail_message_id, status, started_at, ended_at,"
        " total_cost_usd) VALUES (%s, %s, %s, %s, %s, %s)",
        (
            trace_id,
            f"msg-{trace_id}",
            "success" if finished else "running",
            WRITTEN,
            WRITTEN if finished else None,
            total,
        ),
    )
    return trace_id


def _span(
    conn: psycopg.Connection,
    trace_id: uuid.UUID,
    *,
    model: str = TRIAGE,
    cost: Decimal | None = STALE,
    started_at: datetime = WRITTEN,
) -> int:
    row = conn.execute(
        "INSERT INTO spans (trace_id, node, model, status, started_at, latency_ms,"
        " input_tokens, output_tokens, cost_usd)"
        " VALUES (%s, 'classify', %s, 'ok', %s, 800, %s, %s, %s) RETURNING id",
        (trace_id, model, started_at, INPUT_TOKENS, OUTPUT_TOKENS, cost),
    ).fetchone()
    assert row is not None
    span_id: int = row[0]
    return span_id


def _cost(conn: psycopg.Connection, span_id: int) -> Decimal | None:
    row = conn.execute("SELECT cost_usd FROM spans WHERE id = %s", (span_id,)).fetchone()
    assert row is not None
    cost: Decimal | None = row[0]
    return cost


def _total(conn: psycopg.Connection, trace_id: uuid.UUID) -> Decimal | None:
    row = conn.execute(
        "SELECT total_cost_usd FROM runs WHERE trace_id = %s", (trace_id,)
    ).fetchone()
    assert row is not None
    total: Decimal | None = row[0]
    return total


def _stored(conn: psycopg.Connection) -> tuple[list[Any], list[Any]]:
    spans = conn.execute("SELECT id, cost_usd FROM spans ORDER BY id").fetchall()
    runs = conn.execute("SELECT trace_id, total_cost_usd FROM runs ORDER BY trace_id").fetchall()
    return spans, runs


def test_a_span_stored_with_a_stale_cost_is_corrected(conn: psycopg.Connection) -> None:
    """The placeholder table's figure, repriced from the tokens stored beside it.
    The run's total follows its span."""
    trace_id = _run(conn, total=STALE)
    span_id = _span(conn, trace_id)

    result = reprice(conn)

    assert FRESH is not None and FRESH != STALE
    assert _cost(conn, span_id) == FRESH
    assert _total(conn, trace_id) == FRESH
    assert (result.spans_changed, result.runs_changed) == (1, 1)
    assert (result.spans_total_before, result.spans_total_after) == (STALE, FRESH)
    assert (result.runs_total_before, result.runs_total_after) == (STALE, FRESH)


def test_each_span_is_priced_on_the_day_it_started_not_today(conn: psycopg.Connection) -> None:
    """3.6 Flash doubles on 1 January 2027, so the same tokens either side of UTC
    midnight cost different amounts. The session zone is set east of UTC because
    psycopg returns `started_at` in it: the last second of 2026 comes back dated
    1 January, and pricing by that date would bill it at the new rate."""
    conn.execute("SET TIME ZONE 'Asia/Karachi'")  # undone with the transaction
    last_old = datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)
    first_new = datetime(2027, 1, 1, tzinfo=UTC)
    trace_id = _run(conn)
    old = _span(conn, trace_id, model="gemini-3.6-flash", started_at=last_old)
    new = _span(conn, trace_id, model="gemini-3.6-flash", started_at=first_new)

    armed = conn.execute("SELECT started_at FROM spans WHERE id = %s", (old,)).fetchone()
    assert armed is not None and armed[0].date() == date(2027, 1, 1)

    reprice(conn)

    old_rate = cost_usd(
        "gemini-3.6-flash", at=last_old, input_tokens=INPUT_TOKENS, output_tokens=OUTPUT_TOKENS
    )
    new_rate = cost_usd(
        "gemini-3.6-flash", at=first_new, input_tokens=INPUT_TOKENS, output_tokens=OUTPUT_TOKENS
    )
    assert old_rate != new_rate
    assert (_cost(conn, old), _cost(conn, new)) == (old_rate, new_rate)


def test_an_unpriced_span_stays_null_never_zero(conn: psycopg.Connection) -> None:
    """Zero would claim the call was free. A figure stored for a model the table
    cannot price goes to NULL too: the tokens are the record, not old dollars."""
    trace_id = _run(conn, total=STALE)
    never_priced = _span(conn, trace_id, model="mystery-model", cost=None)
    once_priced = _span(conn, trace_id, model="mystery-model", cost=STALE)

    result = reprice(conn)

    assert _cost(conn, never_priced) is None
    assert _cost(conn, once_priced) is None
    assert _total(conn, trace_id) is None  # not zero
    assert result.spans_changed == 1
    assert result.unpriced == {"mystery-model": 2}
    assert result.spans_total_after is None


def test_a_second_run_changes_nothing(conn: psycopg.Connection) -> None:
    finished = _run(conn, total=STALE)
    _span(conn, finished)
    _span(conn, finished, model="mystery-model", cost=None)
    running = _run(conn, finished=False)
    _span(conn, running)

    first = reprice(conn)
    after_first = _stored(conn)
    second = reprice(conn)

    assert (first.spans_changed, first.runs_changed) == (2, 1)
    assert (second.spans_changed, second.runs_changed) == (0, 0)
    assert second.spans_total_before == second.spans_total_after == first.spans_total_after
    assert second.runs_total_before == second.runs_total_after == first.runs_total_after
    assert _stored(conn) == after_first


def test_a_dry_run_reports_the_real_run_and_writes_nothing(conn: psycopg.Connection) -> None:
    trace_id = _run(conn, total=STALE)
    span_id = _span(conn, trace_id)

    dry = reprice(conn, dry_run=True)

    assert dry.spans_changed == 1
    assert (_cost(conn, span_id), _total(conn, trace_id)) == (STALE, STALE)
    assert reprice(conn) == dry


def test_only_finished_runs_are_re_totalled(conn: psycopg.Connection) -> None:
    """A run in flight is left to finish_run, which will sum its spans as they are
    by then. A finished run is re-totalled even from NULL: finish_run leaves NULL
    when nothing was priced, and 3.8 Flash was missing from the old table."""
    running = _run(conn, finished=False)
    _span(conn, running)
    unpriced_then = _run(conn, total=None)
    _span(conn, unpriced_then, model="gemini-3.8-flash", cost=None)

    result = reprice(conn)

    assert _total(conn, running) is None
    assert _total(conn, unpriced_then) == cost_usd(
        "gemini-3.8-flash", at=WRITTEN, input_tokens=INPUT_TOKENS, output_tokens=OUTPUT_TOKENS
    )
    assert (result.runs_checked, result.runs_changed) == (1, 1)


def test_a_run_the_tracer_just_finished_is_already_current(conn: psycopg.Connection) -> None:
    """The job prices a span with the tracer's own call and totals a run with
    finish_run's own sum, so rows written under the current table stay put --
    including a span with every kind of token, which a crossed argument would move."""
    tracer = Tracer(conn)
    tracer.start_run("msg-1")
    with tracer.span("fetch"):
        pass
    with tracer.span("classify"):
        record_llm_usage(
            model=TRIAGE,
            input_tokens=5_000,
            output_tokens=300,
            cached_tokens=4_000,
            thinking_tokens=200,
        )
    with tracer.span("extract"):
        record_llm_usage(
            model="mystery-model",
            input_tokens=5_000,
            output_tokens=100,
            cached_tokens=0,
            thinking_tokens=0,
        )
    tracer.finish_run("success")

    result = reprice(conn)

    assert (result.spans_checked, result.spans_changed) == (2, 0)
    assert (result.runs_checked, result.runs_changed) == (1, 0)
