"""The spend cap's gate (M17, D5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import psycopg
import pytest

from app.policy.budget import (
    BudgetExhaustedError,
    Gate,
    MessageTooCostlyError,
    Spend,
    UnpricedModelError,
    record_state,
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


# --- where spending stands (17.11) ------------------------------------------------------


def _spent(monkeypatch: pytest.MonkeyPatch, amount: str, *, cap: str = "40") -> Gate:
    monkeypatch.setattr(Gate, "month_spend", lambda self, now=None: Decimal(amount))
    return Gate(cast(Any, None), Decimal(cap), Decimal("0.50"), clock=lambda: NOW)


@pytest.mark.parametrize(
    ("spent", "state"),
    [
        ("0", "ok"),
        ("31.99", "ok"),
        ("32", "warning"),
        ("39.89", "warning"),
        ("39.90", "exhausted"),  # the reserve: new work has stopped
        ("45", "exhausted"),
    ],
)
def test_where_spending_stands(monkeypatch: pytest.MonkeyPatch, spent: str, state: str) -> None:
    assert _spent(monkeypatch, spent).state() == state


def test_a_budget_alert_is_about_the_month_and_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once per month and cap value (D5): raising the cap re-arms the alerts."""
    end_of_month = datetime(2026, 10, 31, 23, 59, tzinfo=UTC)
    gate = _spent(monkeypatch, "0")
    gate.clock = lambda: end_of_month

    raised = _spent(monkeypatch, "0", cap="50")
    raised.clock = lambda: end_of_month

    assert gate.alert_subject() == "2026-10:40.00"
    assert raised.alert_subject() == "2026-10:50.00"


def test_the_subject_is_the_month_of_the_moment_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """A look that straddles midnight on the 1st files its alert under the
    month its state was read in."""
    gate = _spent(monkeypatch, "0")
    gate.clock = lambda: datetime(2026, 11, 1, 0, 0, 1, tzinfo=UTC)

    assert gate.alert_subject(datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC)) == "2026-10:40.00"


def test_a_model_with_no_price_stops_new_work_but_spends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run that reached it would be refused part way, and run again from the
    start. The state stays where spending is: `/health` reports the model."""
    gate = _spent(monkeypatch, "0")
    gate.models_in_use = ("gemini-3.6-flash", "gemini-0-unpriced")

    assert gate.allows_new_work() is False
    assert gate.unpriced() == ["gemini-0-unpriced"]
    assert gate.state() == "ok"


def test_a_message_under_its_ceiling_may_spend(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _spent(monkeypatch, "0")
    monkeypatch.setattr(Gate, "message_spend", lambda self, message_id: Decimal("0.49"))
    assert gate.allows_message("m1") is True

    monkeypatch.setattr(Gate, "message_spend", lambda self, message_id: Decimal("0.50"))
    assert gate.allows_message("m1") is False


@pytest.mark.integration
def test_a_change_of_state_is_written_and_audited_once(conn: psycopg.Connection) -> None:
    conn.execute("UPDATE control SET budget_state = 'ok'")
    row = conn.execute("SELECT coalesce(max(id), 0) FROM audit_log").fetchone()
    assert row is not None

    assert record_state(conn, "warning") is True
    assert record_state(conn, "warning") is False
    assert record_state(conn, "ok") is True

    assert conn.execute("SELECT budget_state FROM control").fetchone() == ("ok",)
    kinds = conn.execute("SELECT kind FROM audit_log WHERE id > %s ORDER BY id", (row[0],))
    assert kinds.fetchall() == [("budget_warning",), ("budget_ok",)]
