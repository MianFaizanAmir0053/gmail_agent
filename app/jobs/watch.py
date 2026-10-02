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
from datetime import UTC, datetime, timedelta
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


RETRY_EVERY = timedelta(hours=1)
"""How often an alert some channel has not delivered is offered again."""


@dataclass(frozen=True, slots=True)
class Watched:
    state: BudgetState
    month_spend_usd: Decimal
    unconfirmed: int
    """Open decisions whose calendar write could not be confirmed (D3)."""
    sent: list[AlertCode]
    """Alerts that reached at least one channel they had not reached before."""


def watch(
    conn: psycopg.Connection,
    settings: Settings,
    channels: AlertSender,
    *,
    now: datetime | None = None,
    tried: dict[tuple[str, str], datetime] | None = None,
) -> Watched:
    """One look. `conn` must commit as it goes (`app.jobs.scheduler.run_watch`).

    The clock is read once, so a look that straddles midnight on the 1st
    files its state and its alert under the same month. The budget and the
    unconfirmed writes are each tried even if the other fails; the first
    failure is raised once both have been tried. With `tried`, an alert is
    offered to the channels at most once per `RETRY_EVERY`.
    """
    now = now or datetime.now(UTC)
    meter = models.gate(settings, conn)
    alerts: list[Alert] = []
    failures: list[Exception] = []

    state: BudgetState = "ok"
    spend = Decimal(0)
    try:
        state = meter.state(now)
        spend = meter.month_spend(now)
        record_state(conn, state)
        if state in BUDGET_ALERTS:
            alerts.append(Alert(BUDGET_ALERTS[state], meter.alert_subject(now)))
    except Exception as exc:
        failures.append(exc)

    unconfirmed: list[int] = []
    try:
        unconfirmed = unconfirmed_writes(conn)
        alerts += [Alert("write_unconfirmed", str(each)) for each in unconfirmed]
    except Exception as exc:
        failures.append(exc)

    if tried is not None:
        due = now - RETRY_EVERY
        alerts = [a for a in alerts if tried.get((a.code, a.subject), due) <= due]
        for alert in alerts:
            tried[(alert.code, alert.subject)] = now
    sent = send_alerts(conn, channels, alerts) if alerts else []
    if failures:
        raise failures[0]
    return Watched(state=state, month_spend_usd=spend, unconfirmed=len(unconfirmed), sent=sent)
