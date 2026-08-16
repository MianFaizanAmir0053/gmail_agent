"""Pricing, redaction, and trace recording."""

from __future__ import annotations

from decimal import Decimal

import psycopg
import pytest

from app.obs.pricing import RATES, cost_usd, unpriced_models
from app.obs.redact import redact, redact_text
from app.obs.trace import Tracer, record_llm_usage

MODEL = next(iter(RATES))


# --- pricing ---------------------------------------------------------------


def test_unpriced_model_costs_none_not_zero() -> None:
    """Zero claims something was free, which quietly under-reports the total."""
    assert cost_usd("gemini-does-not-exist", input_tokens=1_000_000) is None


def test_missing_model_is_none_not_zero() -> None:
    assert cost_usd(None, input_tokens=1_000) is None


def test_cost_is_per_million_tokens() -> None:
    rate = RATES[MODEL]
    assert cost_usd(MODEL, input_tokens=1_000_000) == rate.input_.quantize(Decimal("0.000001"))


def test_thinking_tokens_bill_as_output() -> None:
    both = cost_usd(MODEL, output_tokens=1_000, thinking_tokens=1_000)
    output_only = cost_usd(MODEL, output_tokens=2_000)
    assert both == output_only


def test_thinking_tokens_are_still_tracked_separately() -> None:
    """They bill like output but tuning them is the first cost lever, so the
    counts must stay distinguishable even though the arithmetic merges them."""
    assert cost_usd(MODEL, thinking_tokens=1_000) != cost_usd(MODEL, input_tokens=1_000)


def test_cached_tokens_are_cheaper_when_a_rate_exists() -> None:
    cached = cost_usd(MODEL, cached_tokens=1_000_000)
    fresh = cost_usd(MODEL, input_tokens=1_000_000)
    assert cached is not None and fresh is not None
    assert cached < fresh


def test_cached_falls_back_to_input_rate_when_unknown() -> None:
    """Overestimating is the safer direction for a cost figure to be wrong in."""
    from app.obs.pricing import Rate

    RATES["test-no-cache-rate"] = Rate(Decimal("1.00"), Decimal("2.00"))
    try:
        assert cost_usd("test-no-cache-rate", cached_tokens=1_000_000) == Decimal("1.000000")
    finally:
        del RATES["test-no-cache-rate"]


def test_unpriced_models_reports_the_gap() -> None:
    assert unpriced_models({MODEL, "mystery-model", ""}) == {"mystery-model"}


# --- redaction -------------------------------------------------------------


def test_emails_are_removed() -> None:
    assert "sara@realcorp.co.uk" not in redact_text("write to sara@realcorp.co.uk")


def test_phone_numbers_are_removed() -> None:
    assert "1234567" not in redact_text("call +92 300 1234567")


def test_urls_are_removed() -> None:
    assert "realcorp" not in redact_text("see https://internal.realcorp.co.uk/deck")


def test_long_digit_runs_are_removed() -> None:
    assert "4111111111111111" not in redact_text("card 4111111111111111")


def test_structure_survives_redaction() -> None:
    """Traces exist to debug extraction, so the shape has to remain readable."""
    redacted = redact({"subject": "Sync with sara@x.com", "attendees": ["bob@y.com"], "n": 3})
    assert redacted["n"] == 3
    assert "Sync with" in redacted["subject"]
    assert redacted["attendees"] == ["<email>"]


def test_dict_keys_are_not_redacted() -> None:
    """Keys are field names we chose, not content."""
    assert "email" in redact({"email": "a@b.com"})


def test_long_bodies_are_truncated() -> None:
    """A trace is for diagnosis, not archival."""
    assert len(redact_text("x" * 10_000)) < 2_200


# --- tracing ---------------------------------------------------------------


def test_usage_recording_outside_a_span_is_a_no_op() -> None:
    """The eval harness and unit tests call the LLM with no tracer attached."""
    record_llm_usage(model="m", input_tokens=1, output_tokens=1, cached_tokens=0, thinking_tokens=0)


@pytest.mark.integration
def test_span_captures_usage_reported_from_inside(conn: psycopg.Connection) -> None:
    tracer = Tracer(conn)
    tracer.start_run("msg-1")

    with tracer.span("classify"):
        record_llm_usage(
            model=MODEL, input_tokens=1200, output_tokens=90, cached_tokens=0, thinking_tokens=40
        )

    row = conn.execute(
        "SELECT node, model, input_tokens, thinking_tokens, status, cost_usd FROM spans"
        " WHERE trace_id = %s",
        (tracer.trace_id,),
    ).fetchone()

    assert row is not None
    assert row[0] == "classify"
    assert row[1] == MODEL
    assert (row[2], row[3]) == (1200, 40)
    assert row[4] == "ok"
    assert row[5] is not None


@pytest.mark.integration
def test_a_failing_node_records_an_error_span_and_re_raises(conn: psycopg.Connection) -> None:
    tracer = Tracer(conn)
    tracer.start_run("msg-2")

    with pytest.raises(RuntimeError, match="gmail down"), tracer.span("fetch"):
        raise RuntimeError("gmail down")

    row = conn.execute(
        "SELECT status, error FROM spans WHERE trace_id = %s", (tracer.trace_id,)
    ).fetchone()

    assert row is not None
    assert row[0] == "error"
    assert "gmail down" in row[1]


@pytest.mark.integration
def test_span_payloads_are_redacted_before_storage(conn: psycopg.Connection) -> None:
    tracer = Tracer(conn)
    tracer.start_run("msg-3")

    with tracer.span("fetch", payload={"sender": "real.person@realcorp.co.uk"}):
        pass

    row = conn.execute(
        "SELECT input_redacted::text FROM spans WHERE trace_id = %s", (tracer.trace_id,)
    ).fetchone()

    assert row is not None
    assert "realcorp" not in row[0]


@pytest.mark.integration
def test_finish_run_totals_span_costs(conn: psycopg.Connection) -> None:
    tracer = Tracer(conn)
    tracer.start_run("msg-4")

    for node in ("classify", "extract"):
        with tracer.span(node):
            record_llm_usage(
                model=MODEL,
                input_tokens=1_000_000,
                output_tokens=0,
                cached_tokens=0,
                thinking_tokens=0,
            )

    tracer.finish_run("success")

    row = conn.execute(
        "SELECT status, total_cost_usd, duration_ms FROM runs WHERE trace_id = %s",
        (tracer.trace_id,),
    ).fetchone()

    assert row is not None
    assert row[0] == "success"
    assert row[1] == RATES[MODEL].input_ * 2
    assert row[2] is not None


@pytest.mark.integration
def test_unpriced_spans_leave_the_total_null_rather_than_understated(
    conn: psycopg.Connection,
) -> None:
    tracer = Tracer(conn)
    tracer.start_run("msg-5")

    with tracer.span("extract"):
        record_llm_usage(
            model="mystery-model",
            input_tokens=5000,
            output_tokens=100,
            cached_tokens=0,
            thinking_tokens=0,
        )

    row = conn.execute(
        "SELECT cost_usd, input_tokens FROM spans WHERE trace_id = %s", (tracer.trace_id,)
    ).fetchone()

    assert row is not None
    assert row[0] is None  # not zero
    assert row[1] == 5000  # tokens still recorded, so cost is recomputable later
