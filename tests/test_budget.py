"""The spend cap's gate (M17, D5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest

from app.policy.budget import (
    BudgetExhaustedError,
    Gate,
    MessageTooCostlyError,
    Spend,
    UnpricedModelError,
)

NOW = datetime.now(UTC)
MONTH_START = NOW.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
PRICED = "gemini-3.6-flash"


def _gate(conn: psycopg.Connection, *, cap: str = "40", ceiling: str = "0.50") -> Gate:
    conn.execute("DELETE FROM model_spend")  # rolled back with the test
    return Gate(conn, Decimal(cap), Decimal(ceiling), clock=lambda: NOW)


def _rows(conn: psycopg.Connection) -> list[tuple[object, ...]]:
    return conn.execute(
        "SELECT model, message_id, cost_usd, estimated, refused FROM model_spend ORDER BY id"
    ).fetchall()


@pytest.mark.integration
def test_a_priced_call_under_the_cap_is_allowed_and_recorded(conn: psycopg.Connection) -> None:
    gate = _gate(conn)

    gate.check(PRICED, "m1")
    gate.record(PRICED, "m1", Spend(input_tokens=1000, output_tokens=50, cost_usd=Decimal("0.01")))

    assert _rows(conn) == [(PRICED, "m1", Decimal("0.010000"), False, None)]


@pytest.mark.integration
def test_a_model_with_no_rate_is_refused_before_any_call(conn: psycopg.Connection) -> None:
    """Fail closed: a model nobody priced could spend without limit."""
    gate = _gate(conn)

    with pytest.raises(UnpricedModelError):
        gate.check("gemini-2.5-pro", "m1")

    assert _rows(conn) == [("gemini-2.5-pro", "m1", Decimal("0.000000"), False, "unpriced")]


@pytest.mark.integration
def test_at_the_cap_every_call_is_refused(conn: psycopg.Connection) -> None:
    gate = _gate(conn, cap="1")
    gate.record(PRICED, None, Spend(cost_usd=Decimal("1.00")))

    with pytest.raises(BudgetExhaustedError):
        gate.check(PRICED, "m2")

    assert _rows(conn)[-1][-1] == "exhausted"


@pytest.mark.integration
def test_a_message_over_its_ceiling_is_refused(conn: psycopg.Connection) -> None:
    gate = _gate(conn)
    gate.record(PRICED, "m1", Spend(cost_usd=Decimal("0.50")))

    with pytest.raises(MessageTooCostlyError):
        gate.check(PRICED, "m1")
    gate.check(PRICED, "m2")  # another message is not held back

    assert _rows(conn)[-1][-1] == "too_costly"


@pytest.mark.integration
def test_the_months_total_is_read_once_a_minute_plus_what_it_metered(
    conn: psycopg.Connection,
) -> None:
    """Spend by another process shows within a minute; its own at once."""
    clock = [NOW]
    gate = Gate(conn, Decimal("40"), Decimal("0.50"), clock=lambda: clock[0])
    conn.execute("DELETE FROM model_spend")
    assert gate.month_spend() == Decimal(0)

    conn.execute("INSERT INTO model_spend (model, cost_usd, at) VALUES (%s, 2, %s)", (PRICED, NOW))
    gate.record(PRICED, None, Spend(cost_usd=Decimal("1")))
    assert gate.month_spend() == Decimal(1)  # the other process's 2 not read yet

    clock[0] = NOW + timedelta(minutes=1)
    assert gate.month_spend() == Decimal(3)


@pytest.mark.integration
def test_last_months_spend_does_not_count(conn: psycopg.Connection) -> None:
    gate = _gate(conn, cap="1")
    conn.execute(
        "INSERT INTO model_spend (model, cost_usd, at) VALUES (%s, 5, %s)",
        (PRICED, MONTH_START - timedelta(minutes=1)),
    )

    gate.check(PRICED, "m1")  # a new month: nothing spent yet
