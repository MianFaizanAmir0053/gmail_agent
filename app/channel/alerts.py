"""Alerts, each sent once (M16, D6; M17, D3 and D5).

Three token situations deserve a push, each once per state change:
- a Testing token within two days of expiry (`token_expiring`);
- a token that has expired with no usable standby, so mail has stopped
  (`token_expired`);
- failover to the standby, so mail still flows but the primary needs
  replacing (`standby_in_use`).

The spend cap's two (`budget_warning`, `budget_exhausted`) are sent once per
month and cap value, and a calendar write that could not be confirmed
(`write_unconfirmed`) once per decision (`app/jobs/watch.py`).

"Once" is kept in `alerts_sent`, keyed by the alert, what it is about and the
channel, so it survives restarts and a new subject alerts afresh. A row
is written only once that channel delivered the alert. Per channel, because
Telegram accepting a message says nothing about the phones only web push
reaches -- and Telegram is blocked on the owner's network. A channel that has
not delivered is asked again at the next check.

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
    @property
    def names(self) -> frozenset[str]: ...

    def alert(self, code: AlertCode, *, skip: frozenset[str] = ...) -> set[str]: ...


@dataclass(frozen=True, slots=True)
class Alert:
    code: AlertCode
    subject: str
    """What it is about: a token's `issued_at`, a month and cap, a decision's
    id. A new subject alerts afresh."""


def token_alerts(
    issued_at: datetime, state: TokenState, health: TokenHealth, *, standby: str | None
) -> list[Alert]:
    subject = issued_at.isoformat()
    if state == "expired":
        code: AlertCode = "token_expired" if standby in UNUSABLE_STANDBY else "standby_in_use"
        return [Alert(code, subject)]
    if state == "testing" and health.needs_reauth_soon:
        return [Alert("token_expiring", subject)]
    return []


def send_alerts(
    conn: psycopg.Connection, channels: AlertSender, alerts: list[Alert]
) -> list[AlertCode]:
    """Send each alert through the channels that have not yet delivered it.
    Returns the codes that reached at least one new channel."""
    sent: list[AlertCode] = []
    for alert in alerts:
        already = _delivered_to(conn, alert)
        if not channels.names - already:
            continue
        delivered = channels.alert(alert.code, skip=already)
        for name in sorted(delivered):
            conn.execute(
                """
                INSERT INTO alerts_sent (code, subject, channel)
                VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
                """,
                (alert.code, alert.subject, name),
            )
        if delivered:
            sent.append(alert.code)
        if channels.names - already - delivered:
            log.warning("alert %s not delivered everywhere; the next check tries again", alert.code)
    return sent


def _delivered_to(conn: psycopg.Connection, alert: Alert) -> frozenset[str]:
    rows = conn.execute(
        "SELECT channel FROM alerts_sent WHERE code = %s AND subject = %s",
        (alert.code, alert.subject),
    ).fetchall()
    return frozenset(row[0] for row in rows)
