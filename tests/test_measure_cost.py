"""M15's cost measurement (spec B3): raw tokens, priced at two dates.

Stored `spans.cost_usd` was fixed at write time, and much of it was written
with placeholder rates, so nothing here reads it. Every figure is recomputed
from the raw token columns.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from app.jobs.measure_cost import (
    EmbeddingEstimate,
    SpanRow,
    UnpricedModelError,
    cost_report,
    refuse_local_database,
)

WINDOW_START = datetime(2026, 10, 6, tzinfo=UTC)
NO_EMBEDDINGS = EmbeddingEstimate(messages_per_day=0.0)


def _span(
    trace: str,
    node: str,
    *,
    model: str = "gemini-3.6-flash",
    input_tokens: int = 1_000_000,
    output_tokens: int = 0,
    retries: int = 0,
) -> SpanRow:
    return SpanRow(
        trace_id=trace,
        node=node,
        model=model,
        started_at=WINDOW_START + timedelta(hours=1),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=0,
        thinking_tokens=0,
        retry_count=retries,
    )


def _report(spans: list[SpanRow], **overrides: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "inbound_per_day": 100.0,
        "hosting_usd": 4.05,
        "database_usd": 0.0,
        "embeddings": NO_EMBEDDINGS,
        "billed_gemini_usd": None,
        "extraction_model": "gemini-3.6-flash",
    }
    return cost_report(spans, **(options | overrides))


def _extractions(n: int, *, extract_tokens: int = 1_000_000) -> list[SpanRow]:
    spans = []
    for i in range(n):
        spans.append(_span(f"t{i}", "classify", model="gemini-3.5-flash-lite", input_tokens=1000))
        spans.append(_span(f"t{i}", "extract", input_tokens=extract_tokens))
    return spans


def test_raw_tokens_are_priced_at_both_reference_dates() -> None:
    """3.6 Flash doubles on 1 Jan 2027: $0.75 per million input tokens before, $1.50 after."""
    report = _report(_extractions(20))
    extraction = report["extraction_per_extraction"]

    assert extraction["label"] == "measured"
    assert extraction["2026"] == pytest.approx(0.75)
    assert extraction["2027"] == pytest.approx(1.50)


def test_an_unpriced_model_stops_the_report() -> None:
    """A NULL cost summed as zero would understate the budget."""
    with pytest.raises(UnpricedModelError, match="mystery-model"):
        _report([_span("t", "extract", model="mystery-model")])


def test_fewer_than_twenty_extractions_are_not_called_measured() -> None:
    report = _report(_extractions(3))
    extraction = report["extraction_per_extraction"]

    assert extraction["label"].startswith("unmeasured")
    assert extraction["2027_upper_bound"] > 0


def test_the_projection_weights_extraction_by_the_meeting_rate() -> None:
    """Extraction runs only for the share of mail that looks like a meeting."""
    spans = [
        _span(f"t{i}", "classify", model="gemini-3.5-flash-lite", input_tokens=1000)
        for i in range(100)
    ]
    spans += [_span(f"t{i}", "extract") for i in range(20)]
    spans += [_span(f"t{i}", "extract") for i in range(20)]  # a revision round: same traces

    report = _report(spans)

    assert report["meeting_rate"] == pytest.approx(0.2)
    triage = report["triage_per_message"]["2027"]
    extraction = report["extraction_per_extraction"]["2027"]
    expected = 30 * 100.0 * (triage + 0.2 * 2 * extraction)  # two extractions per meeting
    assert report["projection"]["v1_observe_mode"]["2027"] == pytest.approx(expected, rel=1e-6)


def test_retries_are_totalled() -> None:
    spans = _extractions(20)
    spans.append(_span("tx", "classify", model="gemini-3.5-flash-lite", input_tokens=10, retries=3))

    assert _report(spans)["retries"] == 3


def test_the_bill_is_reconciled_within_fifteen_percent() -> None:
    spans = _extractions(20)
    actual = _report(spans)["reconciliation"]["priced_at_actual_dates"]

    assert _report(spans, billed_gemini_usd=actual * 1.1)["reconciliation"]["within_tolerance"]
    assert not _report(spans, billed_gemini_usd=actual * 2)["reconciliation"]["within_tolerance"]


def test_the_budget_holds_only_if_both_limits_do() -> None:
    cheap = _extractions(20, extract_tokens=3000)  # a realistic extraction prompt

    assert _report(cheap, inbound_per_day=1.0)["decision"]["budget_holds"] is True
    assert (
        _report(cheap, inbound_per_day=1.0, hosting_usd=12.0)["decision"]["budget_holds"] is False
    )


@pytest.mark.parametrize(
    "url",
    ["postgresql://mailagent:x@localhost:5432/mailagent", "postgresql://u:p@127.0.0.1/db"],
)
def test_a_local_database_is_refused(url: str) -> None:
    """The spans that matter are in production; a local run would measure nothing."""
    with pytest.raises(SystemExit, match="instance"):
        refuse_local_database(url)


def test_the_production_database_is_accepted() -> None:
    refuse_local_database("postgresql://postgres:p@db.abc.supabase.co:5432/postgres")


@pytest.mark.integration
def test_spans_are_read_back_with_their_raw_columns(conn: psycopg.Connection) -> None:
    from app.jobs.measure_cost import load_spans
    from app.obs.trace import Tracer, record_llm_usage

    tracer = Tracer(conn)
    tracer.start_run("m-cost")
    with tracer.span("classify"):
        record_llm_usage(
            model="gemini-3.5-flash-lite",
            input_tokens=1200,
            output_tokens=40,
            cached_tokens=200,
            thinking_tokens=0,
        )

    now = datetime.now(UTC)
    spans = load_spans(conn, now - timedelta(minutes=5), now + timedelta(minutes=5))

    mine = [span for span in spans if span.trace_id == str(tracer.trace_id)]
    assert [(s.node, s.model, s.input_tokens, s.cached_tokens) for s in mine] == [
        ("classify", "gemini-3.5-flash-lite", 1200, 200)
    ]


def test_no_classifications_means_no_decision() -> None:
    """With nothing measured, "the budget holds" would be a claim with no evidence."""
    report = _report([])

    assert report["decision"]["budget_holds"] is None
    assert report["meeting_rate_label"] == "unmeasured"
