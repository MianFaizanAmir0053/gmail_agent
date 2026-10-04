"""The chat check and the turn's own ceiling (M19, D9)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg
import pytest

from app.config import Settings
from app.policy import models
from app.policy.budget import (
    CHAT_STOPS,
    ChatCheck,
    Gate,
    MessageTooCostlyError,
    Spend,
    chat_message_id,
)

NOW = datetime(2026, 10, 15, 5, tzinfo=UTC)
"""10:00 on Thursday 15 October in Karachi."""

PRICED = "gemini-3.6-flash"
KARACHI = ZoneInfo("Asia/Karachi")


def _spent(conn: psycopg.Connection, at: datetime, cost: str, message_id: str | None) -> None:
    conn.execute(
        "INSERT INTO model_spend (at, model, message_id, cost_usd) VALUES (%s, %s, %s, %s)",
        (at, PRICED, message_id, Decimal(cost)),
    )


def _check(
    conn: psycopg.Connection, *, cap: str = "40", models_in_use: tuple[str, ...] = (PRICED,)
) -> ChatCheck:
    conn.execute("DELETE FROM model_spend")  # rolled back with the test
    conn.execute("UPDATE control SET paused = false WHERE id = 1")
    gate = Gate(conn, Decimal(cap), Decimal("0.10"), clock=lambda: NOW, models_in_use=models_in_use)
    return ChatCheck(gate, Decimal("1.00"), Decimal("15"), KARACHI)


@pytest.mark.integration
def test_a_turn_may_go_on_when_nothing_stops_it(conn: psycopg.Connection) -> None:
    assert _check(conn).stop() is None


@pytest.mark.integration
def test_a_pause_stops_a_turn(conn: psycopg.Connection) -> None:
    check = _check(conn)
    conn.execute("UPDATE control SET paused = true WHERE id = 1")
    assert check.stop() == "paused"


@pytest.mark.integration
def test_the_months_cap_stops_a_turn(conn: psycopg.Connection) -> None:
    check = _check(conn, cap="1")
    _spent(conn, NOW, "1.00", "m1")
    assert check.stop() == "spending"


@pytest.mark.integration
def test_a_model_with_no_price_stops_a_turn(conn: psycopg.Connection) -> None:
    check = _check(conn, models_in_use=(PRICED, "gemini-0-unpriced"))
    assert check.stop() == "spending"


@pytest.mark.integration
def test_the_daily_cap_counts_the_owners_day(conn: psycopg.Connection) -> None:
    check = _check(conn)
    # 23:30 yesterday in Karachi, and mail spent today: neither is today's chat.
    _spent(conn, datetime(2026, 10, 14, 18, 30, tzinfo=UTC), "1.00", chat_message_id(1))
    _spent(conn, NOW, "5.00", "m1")
    assert check.stop() is None

    # 00:30 today in Karachi, though still the 14th in UTC.
    _spent(conn, datetime(2026, 10, 14, 19, 30, tzinfo=UTC), "1.00", chat_message_id(2))
    assert check.stop() == "daily_cap"


@pytest.mark.integration
def test_the_mail_reserve_stops_chat_before_mail(conn: psycopg.Connection) -> None:
    """Chat never stops mail late in a month (D9)."""
    check = _check(conn)
    _spent(conn, datetime(2026, 10, 1, tzinfo=UTC), "24.90", "m1")
    assert check.stop() is None
    _spent(conn, datetime(2026, 10, 2, tzinfo=UTC), "0.20", "m2")
    # A gate reads the month's total at most once a minute: a new one reads now.
    later = ChatCheck(
        Gate(conn, Decimal("40"), Decimal("0.10"), clock=lambda: NOW, models_in_use=(PRICED,)),
        check.daily_cap_usd,
        check.mail_reserve_usd,
        KARACHI,
    )
    assert later.stop() == "mail_reserve"
    assert later.gate.allows_new_work()  # mail itself may go on


@pytest.mark.integration
def test_a_pause_is_said_first(conn: psycopg.Connection) -> None:
    check = _check(conn, cap="1")
    _spent(conn, NOW, "1.00", chat_message_id(1))
    conn.execute("UPDATE control SET paused = true WHERE id = 1")
    assert check.stop() == "paused"


def test_each_stop_has_a_fixed_reason() -> None:
    assert set(CHAT_STOPS) == {"paused", "spending", "daily_cap", "mail_reserve"}
    assert all(reason.endswith("so I stopped.") for reason in CHAT_STOPS.values())


# --- the turn's own ceiling ------------------------------------------------------------


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql://localhost/test",
        gemini_api_key="test-key",
        user_timezone="Asia/Karachi",
    )


@pytest.mark.integration
def test_a_turn_stops_at_its_own_ceiling(conn: psycopg.Connection) -> None:
    conn.execute("DELETE FROM model_spend")
    gate = models.chat_gate(_settings(), conn)
    turn = chat_message_id(7)
    gate.record(PRICED, turn, Spend(cost_usd=Decimal("0.10")))

    with pytest.raises(MessageTooCostlyError):
        gate.check(PRICED, turn)
    gate.check(PRICED, chat_message_id(8))  # another turn is not held back


def test_the_chat_check_reads_its_caps_from_settings() -> None:
    gate = Gate(conn=None, cap_usd=Decimal(40), ceiling_usd=Decimal("0.10"))  # type: ignore[arg-type]
    check = models.chat_check(_settings(), gate)
    assert (check.daily_cap_usd, check.mail_reserve_usd, check.zone) == (
        Decimal("1.0"),
        Decimal("15.0"),
        KARACHI,
    )
