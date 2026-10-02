"""The watch (M17, D5 and D3): where spending stands, and which calendar
writes could not be confirmed.

Run by the scheduler every few minutes, and at start. It writes the budget's
state to the `control` row when it changes, where the web app's header reads
it, audited; and it sends each alert once:
- `budget_warning` from 80% of the cap, and `budget_exhausted` once new work
  has stopped, once per month and cap value: a raised cap re-arms them;
- `write_unconfirmed` once per decision whose calendar write could not be
  confirmed.

It calls no model: the month's spend is read from `model_spend`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import psycopg

from app.channel.alerts import Alert, AlertSender, send_alerts
from app.channel.channels import AlertCode
from app.channel.worker import unconfirmed_writes
from app.config import Settings
from app.policy import models
from app.policy.budget import BudgetState, record_state

BUDGET_ALERTS: dict[BudgetState, AlertCode] = {
    "warning": "budget_warning",
    "exhausted": "budget_exhausted",
}
"""The alert each state sends. A budget that goes straight from `ok` to
`exhausted` sends only the second."""


@dataclass(frozen=True, slots=True)
class Watched:
    state: BudgetState
    month_spend_usd: Decimal
    sent: list[AlertCode]
    """Alerts that reached at least one channel they had not reached before."""


def watch(conn: psycopg.Connection, settings: Settings, channels: AlertSender) -> Watched:
    """One look. `conn` must commit as it goes (`app.jobs.scheduler.run_watch`)."""
    meter = models.gate(settings, conn)
    state = meter.state()
    record_state(conn, state)
    alerts = [Alert(BUDGET_ALERTS[state], meter.alert_subject())] if state in BUDGET_ALERTS else []
    alerts += [Alert("write_unconfirmed", str(each)) for each in unconfirmed_writes(conn)]
    sent = send_alerts(conn, channels, alerts)
    return Watched(state=state, month_spend_usd=meter.month_spend(), sent=sent)
