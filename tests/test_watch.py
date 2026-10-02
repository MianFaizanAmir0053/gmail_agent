"""The watch (M17, D5): where spending stands, and each alert once."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import psycopg
import pytest

from app.config import Settings
from app.jobs.watch import watch


@dataclass
class FakeChannels:
    names: frozenset[str] = frozenset({"web_push"})
    asked: list[str] = field(default_factory=list)

    def alert(self, code: str, *, skip: frozenset[str] = frozenset()) -> set[str]:
        self.asked.append(code)
        return set(self.names - skip)


def _settings(cap: float = 1.0) -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql://localhost/test",
        gemini_api_key="k",
        monthly_budget_usd=cap,
    )


@pytest.fixture
def spent(conn: psycopg.Connection) -> psycopg.Connection:
    """A month with nothing spent and nothing sent; rolled back with the test."""
    conn.execute("DELETE FROM model_spend")
    conn.execute("DELETE FROM alerts_sent")
    conn.execute("UPDATE control SET budget_state = 'ok'")
    return conn


def _spend(conn: psycopg.Connection, amount: str) -> None:
    conn.execute(
        "INSERT INTO model_spend (at, model, cost_usd) VALUES (now(), 'gemini-3.6-flash', %s)",
        (Decimal(amount),),
    )


def _state(conn: psycopg.Connection) -> str:
    row = conn.execute("SELECT budget_state FROM control").fetchone()
    assert row is not None
    return str(row[0])


@pytest.mark.integration
def test_under_80_percent_nothing_is_sent(spent: psycopg.Connection) -> None:
    _spend(spent, "0.79")
    channels = FakeChannels()

    watched = watch(spent, _settings(), channels)

    assert (watched.state, channels.asked, _state(spent)) == ("ok", [], "ok")
    assert watched.month_spend_usd == Decimal("0.79")


@pytest.mark.integration
def test_at_80_percent_the_owner_is_warned_once(spent: psycopg.Connection) -> None:
    _spend(spent, "0.80")
    channels = FakeChannels()

    watch(spent, _settings(), channels)
    watch(spent, _settings(), channels)  # the next look, five minutes on

    assert channels.asked == ["budget_warning"]
    assert _state(spent) == "warning"


@pytest.mark.integration
def test_at_the_cap_the_owner_hears_processing_has_stopped(spent: psycopg.Connection) -> None:
    """Straight past 80%: only the second alert. The reserve counts: new work
    has stopped before the last ten cents are spent."""
    _spend(spent, "0.95")
    channels = FakeChannels()

    watched = watch(spent, _settings(), channels)

    assert (watched.state, channels.asked) == ("exhausted", ["budget_exhausted"])
    assert _state(spent) == "exhausted"


@pytest.mark.integration
def test_raising_the_cap_re_arms_the_alerts(spent: psycopg.Connection) -> None:
    _spend(spent, "0.85")
    channels = FakeChannels()

    watch(spent, _settings(cap=1.0), channels)
    watch(spent, _settings(cap=1.05), channels)  # still past 80% of the new cap

    assert channels.asked == ["budget_warning", "budget_warning"]


@pytest.mark.integration
def test_a_raised_cap_puts_the_state_back_and_audits_it(spent: psycopg.Connection) -> None:
    _spend(spent, "0.95")
    watch(spent, _settings(cap=1.0), FakeChannels())
    row = spent.execute("SELECT max(id) FROM audit_log").fetchone()
    assert row is not None

    channels = FakeChannels()
    watched = watch(spent, _settings(cap=10.0), channels)

    assert (watched.state, channels.asked, _state(spent)) == ("ok", [], "ok")
    kinds = spent.execute("SELECT kind FROM audit_log WHERE id > %s", (row[0],)).fetchall()
    assert kinds == [("budget_ok",)]
