"""Token alerts (M16, D6).

Three situations deserve a push, each once per state change:
- a Testing token within two days of expiry (`token_expiring`);
- a token that has expired with no usable standby, so mail has stopped
  (`token_expired`);
- failover to the standby, so mail still flows but the primary needs
  replacing (`standby_in_use`).

"Once" is kept in `alerts_sent`, keyed by the alert and the token it is about,
so it survives restarts and a new token alerts afresh. A row is written only
after a channel delivered the alert: with no subscriptions yet, or a failed
send, nothing is recorded and the next check tries again.

The judgement reads token state exactly as `/health` does
(`app/obs/token_report.py`). The old check counted every token down from
issue, so a production token would have pushed every twelve hours from day
five.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import psycopg

from app.channel.channels import AlertCode
from app.google.tokens import TokenHealth, TokenState
from app.obs.token_report import UNUSABLE_STANDBY

log = logging.getLogger(__name__)


class AlertSender(Protocol):
    def alert(self, code: AlertCode) -> bool: ...


@dataclass(frozen=True, slots=True)
class TokenAlert:
    code: AlertCode
    subject: str
    """Which token it is about: its `issued_at`. A new token is a new subject."""


def token_alerts(
    issued_at: datetime, state: TokenState, health: TokenHealth, *, standby: str | None
) -> list[TokenAlert]:
    subject = issued_at.isoformat()
    if state == "expired":
        code: AlertCode = "token_expired" if standby in UNUSABLE_STANDBY else "standby_in_use"
        return [TokenAlert(code, subject)]
    if state == "testing" and health.needs_reauth_soon:
        return [TokenAlert("token_expiring", subject)]
    return []


def send_token_alerts(
    conn: psycopg.Connection, channels: AlertSender, alerts: list[TokenAlert]
) -> list[AlertCode]:
    """Send each alert not already delivered. Returns the codes delivered now."""
    delivered: list[AlertCode] = []
    for alert in alerts:
        if _already_sent(conn, alert):
            continue
        if not channels.alert(alert.code):
            log.warning("alert %s not delivered; the next check tries again", alert.code)
            continue
        conn.execute(
            "INSERT INTO alerts_sent (code, subject) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (alert.code, alert.subject),
        )
        delivered.append(alert.code)
    return delivered


def _already_sent(conn: psycopg.Connection, alert: TokenAlert) -> bool:
    row = conn.execute(
        "SELECT 1 FROM alerts_sent WHERE code = %s AND subject = %s",
        (alert.code, alert.subject),
    ).fetchone()
    return row is not None
