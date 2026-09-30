"""Pricing, redaction, and trace recording."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg
import pytest

from app.obs.pricing import RATES, Rate, cost_usd, rate_at, unpriced_models
from app.obs.redact import redact, redact_text
from app.obs.trace import SpanUsage, Tracer, record_llm_usage

MODEL = next(iter(RATES))
AT = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
LAST_OLD_PRICE = datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)
FIRST_NEW_PRICE = datetime(2027, 1, 1, tzinfo=UTC)


# --- pricing ---------------------------------------------------------------


def test_unpriced_model_costs_none_not_zero() -> None:
    """Zero claims something was free, which quietly under-reports the total."""
    assert cost_usd("gemini-does-not-exist", at=AT, input_tokens=1_000_000) is None


def test_missing_model_is_none_not_zero() -> None:
    assert cost_usd(None, at=AT, input_tokens=1_000) is None


def test_cost_is_per_million_tokens() -> None:
    rate = rate_at(MODEL, AT)
    assert rate is not None
    expected = rate.input_.quantize(Decimal("0.000001"))
    assert cost_usd(MODEL, at=AT, input_tokens=1_000_000) == expected


def test_thinking_tokens_bill_as_output() -> None:
    both = cost_usd(MODEL, at=AT, output_tokens=1_000, thinking_tokens=1_000)
    output_only = cost_usd(MODEL, at=AT, output_tokens=2_000)
    assert both == output_only


def test_thinking_tokens_are_still_tracked_separately() -> None:
    """They bill like output but tuning them is the first cost lever, so the
    counts must stay distinguishable even though the arithmetic merges them."""
    thinking = cost_usd(MODEL, at=AT, thinking_tokens=1_000)
    assert thinking != cost_usd(MODEL, at=AT, input_tokens=1_000)


def test_cached_tokens_are_cheaper_when_a_rate_exists() -> None:
    cached = cost_usd(MODEL, at=AT, input_tokens=1_000_000, cached_tokens=1_000_000)
    fresh = cost_usd(MODEL, at=AT, input_tokens=1_000_000)
    assert cached is not None and fresh is not None
    assert cached < fresh


def test_cached_tokens_are_part_of_the_prompt_not_extra() -> None:
    """Gemini's `prompt_token_count` already includes `cached_content_token_count`.

    Adding the cached count on top billed every cached token twice -- once at the
    full input rate, again at the cached one -- which put this call at $0.000712
    and made a cache hit cost more than a miss.
    """
    cost = cost_usd(
        "gemini-3.5-flash-lite",
        at=AT,
        input_tokens=1_200,  # prompt_token_count
        cached_tokens=900,  # cached_content_token_count: a share of the 1,200
        output_tokens=90,
        thinking_tokens=40,
    )
    # 300 fresh x $0.30 + 900 cached x $0.03 + 130 out x $2.50, per million
    assert cost == Decimal("0.000442")


def test_a_cache_hit_never_costs_more_than_a_miss() -> None:
    for model in RATES:
        hit = cost_usd(model, at=AT, input_tokens=1_000, cached_tokens=1_000)
        miss = cost_usd(model, at=AT, input_tokens=1_000)
        assert hit is not None and miss is not None
        assert hit <= miss, model


def test_more_cached_than_prompt_tokens_is_refused() -> None:
    """Only reachable by passing cached tokens as extra to the prompt -- the
    double count, arriving from the other side."""
    with pytest.raises(ValueError, match="part of the prompt"):
        cost_usd(MODEL, at=AT, input_tokens=100, cached_tokens=900)


def test_cached_falls_back_to_input_rate_when_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Overestimating is the safer direction for a cost figure to be wrong in."""
    monkeypatch.setitem(RATES, "test-no-cache-rate", (Rate(Decimal("1.00"), Decimal("2.00")),))
    cost = cost_usd("test-no-cache-rate", at=AT, input_tokens=1_000_000, cached_tokens=1_000_000)
    assert cost == Decimal("1.000000")


def test_unpriced_models_reports_the_gap() -> None:
    assert unpriced_models({MODEL, "mystery-model", ""}) == {"mystery-model"}


# --- dated rates -----------------------------------------------------------

PUBLISHED = [
    # Standard tier, text, USD per 1M tokens: input, output, cached input.
    # Double entry against the pricing page, so a typo has to be made twice.
    ("gemini-2.5-flash", AT, "0.30", "2.50", "0.03"),
    ("gemini-2.5-flash-lite", AT, "0.10", "0.40", "0.01"),
    ("gemini-3.1-flash-lite", AT, "0.25", "1.50", "0.025"),
    ("gemini-3.5-flash", AT, "1.50", "9.00", "0.15"),
    ("gemini-3.5-flash-lite", AT, "0.30", "2.50", "0.03"),
    ("gemini-3.6-flash", LAST_OLD_PRICE, "0.75", "3.75", "0.075"),
    ("gemini-3.6-flash", FIRST_NEW_PRICE, "1.50", "7.50", "0.15"),
    ("gemini-3.7-flash", LAST_OLD_PRICE, "0.75", "3.75", "0.075"),
    ("gemini-3.7-flash", FIRST_NEW_PRICE, "1.50", "7.50", "0.15"),
    ("gemini-3.8-flash", LAST_OLD_PRICE, "0.75", "3.75", "0.075"),
    ("gemini-3.8-flash", FIRST_NEW_PRICE, "1.50", "7.50", "0.15"),
]


@pytest.mark.parametrize(("model", "at", "input_", "output", "cached"), PUBLISHED)
def test_rates_match_the_published_price_list(
    model: str, at: datetime, input_: str, output: str, cached: str
) -> None:
    rate = rate_at(model, at)
    assert rate is not None
    published = (Decimal(input_), Decimal(output), Decimal(cached))
    assert (rate.input_, rate.output, rate.cached_input) == published


def test_every_priced_model_is_in_the_published_list() -> None:
    """A model added to the table without being checked shows up here."""
    assert set(RATES) == {model for model, *_ in PUBLISHED}


def test_a_price_change_takes_effect_at_utc_midnight() -> None:
    before = cost_usd("gemini-3.6-flash", at=LAST_OLD_PRICE, input_tokens=1_000_000)
    after = cost_usd("gemini-3.6-flash", at=FIRST_NEW_PRICE, input_tokens=1_000_000)
    assert (before, after) == (Decimal("0.750000"), Decimal("1.500000"))


def test_the_rate_day_is_judged_in_utc_whatever_zone_the_timestamp_is_in() -> None:
    """psycopg hands TIMESTAMPTZ back in the session's zone. 03:00 on 1 January
    in Karachi is 22:00 on 31 December in UTC, so it still prices at the old
    rate -- the same day the span was priced on when it was written."""
    karachi = datetime(2027, 1, 1, 3, 0, tzinfo=ZoneInfo("Asia/Karachi"))
    rate = rate_at("gemini-3.6-flash", karachi)
    assert rate is not None
    assert rate.input_ == Decimal("0.75")


def test_a_naive_datetime_is_refused() -> None:
    """Which calendar day it falls on depends on a timezone nobody wrote down."""
    with pytest.raises(ValueError, match="naive"):
        cost_usd(MODEL, at=datetime(2027, 1, 1), input_tokens=1)


def test_undated_rates_hold_across_the_change() -> None:
    """Only 3.6-3.8 Flash move. Triage, on 3.5 Flash-Lite, is unaffected."""
    before = rate_at("gemini-3.5-flash-lite", LAST_OLD_PRICE)
    assert before is not None
    assert before == rate_at("gemini-3.5-flash-lite", FIRST_NEW_PRICE)


def test_a_model_not_yet_priced_on_a_date_costs_none_not_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dating rates must not create a way to be free: before a model's first
    rate takes effect it is unpriced, exactly like a model missing entirely."""
    future = Rate(Decimal("1.00"), Decimal("2.00"), effective_from=date(2030, 1, 1))
    monkeypatch.setitem(RATES, "test-future-model", (future,))

    assert cost_usd("test-future-model", at=AT, input_tokens=1_000) is None
    on_the_day = datetime(2030, 1, 1, tzinfo=UTC)
    assert cost_usd("test-future-model", at=on_the_day, input_tokens=1_000) == Decimal("0.001")


def test_every_model_is_priced_from_the_start_with_one_rate_per_day() -> None:
    """`unpriced_models` tests table membership, so a model whose first rate
    starts late would be unpriced before it without ever being reported. Two
    rates starting the same day would leave it ambiguous which one applies."""
    for model, schedule in RATES.items():
        days = [rate.effective_from for rate in schedule]
        assert min(days) == date.min, model
        assert len(days) == len(set(days)), model


def test_span_cost_follows_the_instant_it_is_priced_at() -> None:
    usage = SpanUsage(model="gemini-3.7-flash", input_tokens=1_000_000)
    assert usage.cost_at(LAST_OLD_PRICE) == Decimal("0.750000")
    assert usage.cost_at(FIRST_NEW_PRICE) == Decimal("1.500000")


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
    rate = rate_at(MODEL, datetime.now(UTC))

    assert row is not None and rate is not None
    assert row[0] == "success"
    assert row[1] == rate.input_ * 2
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


@pytest.mark.integration
def test_a_stored_span_reprices_to_the_cost_it_recorded(conn: psycopg.Connection) -> None:
    """History is only recomputable if the stored tokens and `started_at` are
    enough to reproduce the stored cost -- which pins pricing to the span's
    start rather than to whenever the row happened to be written."""
    tracer = Tracer(conn)
    tracer.start_run("msg-6")

    with tracer.span("extract"):
        record_llm_usage(
            model="gemini-3.6-flash",
            input_tokens=5_000,
            output_tokens=300,
            cached_tokens=4_000,
            thinking_tokens=200,
        )

    row = conn.execute(
        "SELECT model, started_at, input_tokens, output_tokens, cached_tokens,"
        " thinking_tokens, cost_usd FROM spans WHERE trace_id = %s",
        (tracer.trace_id,),
    ).fetchone()

    assert row is not None
    model, started_at, input_tokens, output_tokens, cached_tokens, thinking_tokens, stored = row
    assert stored is not None
    assert stored == cost_usd(
        model,
        at=started_at,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        thinking_tokens=thinking_tokens,
    )
