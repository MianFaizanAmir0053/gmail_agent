"""Token alerts (M16, D6): the right alert, once per state change, and only
recorded once a push service has accepted it."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from app.channel.alerts import TokenAlert, send_token_alerts, token_alerts
from app.google.tokens import TokenHealth

ISSUED = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
SUBJECT = ISSUED.isoformat()


def _health(days_remaining: float) -> TokenHealth:
    return TokenHealth(
        issued_at=ISSUED,
        expires_at=ISSUED + timedelta(days=7),
        days_remaining=days_remaining,
    )


# --- which alert --------------------------------------------------------------------


@pytest.mark.parametrize("state", ["production-unconfirmed", "production-confirmed"])
def test_a_production_token_never_gets_the_countdown_alert(state: str) -> None:
    """The bug D6 fixes: the old check counted down every token from issue,
    so a production token would have pushed every twelve hours from day five."""
    assert token_alerts(ISSUED, state, _health(1.0), standby=None) == []  # type: ignore[arg-type]


def test_a_testing_token_within_two_days_of_expiry_gets_one_alert() -> None:
    assert token_alerts(ISSUED, "testing", _health(1.5), standby=None) == [
        TokenAlert("token_expiring", SUBJECT)
    ]
    assert token_alerts(ISSUED, "testing", _health(3.0), standby=None) == []


@pytest.mark.parametrize("standby", [None, "expired", "unreadable"])
def test_an_expired_token_with_no_usable_standby_alerts_that_mail_has_stopped(
    standby: str | None,
) -> None:
    assert token_alerts(ISSUED, "expired", _health(-1.0), standby=standby) == [
        TokenAlert("token_expired", SUBJECT)
    ]


def test_failover_to_the_standby_is_its_own_alert() -> None:
    """Mail still flows, but the primary needs replacing."""
    assert token_alerts(ISSUED, "expired", _health(-1.0), standby="production-unconfirmed") == [
        TokenAlert("standby_in_use", SUBJECT)
    ]


def test_a_new_token_is_a_new_state_to_alert_about() -> None:
    later = ISSUED + timedelta(days=7)
    first = token_alerts(ISSUED, "testing", _health(1.0), standby=None)
    second = token_alerts(later, "testing", _health(1.0), standby=None)
    assert first[0].subject != second[0].subject


# --- once, and only after delivery (Postgres) -------------------------------------------


@dataclass
class FakeChannels:
    delivers: bool = True
    sent: list[str] = field(default_factory=list)

    def alert(self, code: str) -> bool:
        self.sent.append(code)
        return self.delivers


def _recorded(conn: psycopg.Connection) -> list[tuple[str, str]]:
    return conn.execute("SELECT code, subject FROM alerts_sent ORDER BY sent_at").fetchall()


@pytest.mark.integration
def test_an_alert_is_recorded_only_after_a_channel_delivered_it(conn: psycopg.Connection) -> None:
    """With no subscriptions, or a failed send, it is retried at the next check."""
    conn.execute("DELETE FROM alerts_sent")
    alert = TokenAlert("token_expired", SUBJECT)
    undelivered = FakeChannels(delivers=False)

    assert send_token_alerts(conn, undelivered, [alert]) == []
    assert _recorded(conn) == []

    assert send_token_alerts(conn, FakeChannels(), [alert]) == ["token_expired"]
    assert _recorded(conn) == [("token_expired", SUBJECT)]


@pytest.mark.integration
def test_an_alert_goes_out_once_per_state_change_even_across_a_restart(
    conn: psycopg.Connection,
) -> None:
    conn.execute("DELETE FROM alerts_sent")
    alert = TokenAlert("token_expiring", SUBJECT)
    send_token_alerts(conn, FakeChannels(), [alert])

    after_restart = FakeChannels()  # a new process, the same database
    assert send_token_alerts(conn, after_restart, [alert]) == []
    assert after_restart.sent == []

    next_token = TokenAlert("token_expiring", (ISSUED + timedelta(days=7)).isoformat())
    assert send_token_alerts(conn, after_restart, [next_token]) == ["token_expiring"]
