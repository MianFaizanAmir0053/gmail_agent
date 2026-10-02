"""The spend cap (M17, D5): one gate every metered model call asks first.

Before a call the gate refuses:
- a model with no rate today (`UnpricedModelError`). Fail closed: a model
  nobody priced could spend without limit;
- any call once this month's spend has reached the cap (`BudgetExhaustedError`);
- a call for a message that has already spent its ceiling
  (`MessageTooCostlyError`).

A refusal writes a `model_spend` row marked refused, at no cost, so "no model
was called" can be checked afterwards. After a call, `record` writes one row:
the model, the message it served, token counts, the cost and whether the cost
is an estimate. No content.

This month's spend is the sum of `model_spend` over the UTC calendar month.
The gate reads it from the database at most once a minute, and adds what it
metered itself since. It needs a database: there is no gate without one.

New work waits while the month's spend plus a reserve reaches the cap, or
while a model in use has no price (`allows_new_work`). Where spending stands
-- `ok`, `warning` from 80% of the cap, `exhausted` once new work has stopped
for want of budget -- is written to the `control` row when it changes, and
audited (`record_state`): the web app's header reads it there.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

import psycopg

from app.obs.pricing import rate_at
from app.policy import audit

READ_EVERY = timedelta(minutes=1)
"""How stale the month's total may be. Calls metered by this gate since the
read are added to it, so only spend through other gates -- the other jobs'
sessions in this process, and other processes -- can lag, by at most this
long."""

RESERVE_USD = Decimal("0.10")
"""Kept back when deciding whether to start new work, so a message begun
just under the cap can usually finish rather than stop half way. Checked, not
set aside: every gate checks it against its own reading, and one message may
spend up to its ceiling, so a month can end a little over the cap. Every call
is still refused once the month's spend reaches the cap itself."""

CENT = Decimal("0.01")

WARNING_SHARE = Decimal("0.8")
"""The share of the cap at which the owner is warned (D5)."""

Refusal = Literal["unpriced", "exhausted", "too_costly"]

BudgetState = Literal["ok", "warning", "exhausted"]

_STATE_KINDS: dict[BudgetState, audit.Kind] = {
    "ok": "budget_ok",
    "warning": "budget_warning",
    "exhausted": "budget_exhausted",
}


class UnpricedModelError(RuntimeError):
    """The model has no rate today. Nothing was called."""


class BudgetExhaustedError(RuntimeError):
    """This month's spend has reached the cap. Nothing was called."""


class MessageTooCostlyError(RuntimeError):
    """The message has already spent its ceiling. Nothing was called."""


SPENDING_STOPPED = (UnpricedModelError, BudgetExhaustedError)
"""Refusals that stop work until spending is allowed again: what was begun
goes back to wait, rather than failing."""


@dataclass(frozen=True, slots=True)
class Spend:
    """One metered call's numbers. Token counts are as the API reported them,
    or estimated from characters (`estimated`)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    thinking_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    estimated: bool = False


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class Gate:
    conn: psycopg.Connection
    cap_usd: Decimal
    ceiling_usd: Decimal
    clock: Callable[[], datetime] = _utcnow
    models_in_use: tuple[str, ...] = ()
    """The models the running app calls (`app.policy.models.in_use`). New work
    waits while any has no price: a run that reached it would be refused part
    way, and run again from the start, paying again for the calls before."""
    _month: datetime | None = field(default=None, init=False)
    _total: Decimal = field(default=Decimal(0), init=False)
    _read_at: datetime | None = field(default=None, init=False)

    def allows_new_work(self, now: datetime | None = None) -> bool:
        """Whether a new message, an edit or an ingestion batch may start
        (D5): every model in use has a price, and the month's spend plus
        `RESERVE_USD` is under the cap."""
        return not self.unpriced(now) and self._under_cap(now)

    def unpriced(self, now: datetime | None = None) -> list[str]:
        """The models in use with no rate at `now`."""
        at = now or self.clock()
        return sorted({model for model in self.models_in_use if rate_at(model, at) is None})

    def allows_message(self, message_id: str) -> bool:
        """Whether this message may still spend: it is under its ceiling."""
        return self.message_spend(message_id) < self.ceiling_usd

    def state(self, now: datetime | None = None) -> BudgetState:
        """Where this month's spending stands (D5): `exhausted` once new work
        has stopped for want of budget, `warning` from 80% of the cap, `ok`
        below that. A model with no price stops new work too, but spends
        nothing: `/health` reports it."""
        if not self._under_cap(now):
            return "exhausted"
        if self.month_spend(now) >= self.cap_usd * WARNING_SHARE:
            return "warning"
        return "ok"

    def alert_subject(self, now: datetime | None = None) -> str:
        """What a budget alert is about: the month of `now`, and the cap. A
        new month, or a raised cap, is a new subject, and so re-arms the
        alerts (D5)."""
        at = now or self.clock()
        return f"{at.astimezone(UTC):%Y-%m}:{self.cap_usd.quantize(CENT)}"

    def _under_cap(self, now: datetime | None = None) -> bool:
        return self.month_spend(now) + RESERVE_USD < self.cap_usd

    def check(self, model: str, message_id: str | None) -> None:
        """Raise the first refusal that applies, after recording it."""
        now = self.clock()
        if rate_at(model, now) is None:
            self._refused(model, message_id, "unpriced")
            raise UnpricedModelError(f"{model} has no rate")
        if self.month_spend(now) >= self.cap_usd:
            self._refused(model, message_id, "exhausted")
            raise BudgetExhaustedError("this month's model spending cap is reached")
        if message_id is not None and self.message_spend(message_id) >= self.ceiling_usd:
            self._refused(model, message_id, "too_costly")
            raise MessageTooCostlyError("this message has spent its ceiling")

    def record(self, model: str, message_id: str | None, spend: Spend) -> None:
        """One row for one call made."""
        self.conn.execute(
            """
            INSERT INTO model_spend
                   (at, model, message_id, input_tokens, output_tokens, cached_tokens,
                    thinking_tokens, cost_usd, estimated)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                self.clock(),
                model,
                message_id,
                spend.input_tokens,
                spend.output_tokens,
                spend.cached_tokens,
                spend.thinking_tokens,
                spend.cost_usd,
                spend.estimated,
            ),
        )
        self._total += spend.cost_usd

    def month_spend(self, now: datetime | None = None) -> Decimal:
        """This UTC month's spend: the database's total, read at most once a
        minute, plus what this gate has metered since."""
        now = now or self.clock()
        month = now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if self._month != month or self._read_at is None or now - self._read_at >= READ_EVERY:
            row = self.conn.execute(
                "SELECT coalesce(sum(cost_usd), 0) FROM model_spend WHERE at >= %s AND at < %s",
                (month, _next_month(month)),
            ).fetchone()
            self._month, self._read_at = month, now
            self._total = Decimal(row[0]) if row else Decimal(0)
        return self._total

    def message_spend(self, message_id: str) -> Decimal:
        row = self.conn.execute(
            "SELECT coalesce(sum(cost_usd), 0) FROM model_spend WHERE message_id = %s",
            (message_id,),
        ).fetchone()
        return Decimal(row[0]) if row else Decimal(0)

    def _refused(self, model: str, message_id: str | None, refusal: Refusal) -> None:
        self.conn.execute(
            "INSERT INTO model_spend (at, model, message_id, refused) VALUES (%s, %s, %s, %s)",
            (self.clock(), model, message_id, refusal),
        )


def record_state(conn: psycopg.Connection, state: BudgetState) -> bool:
    """Write `state` to the `control` row, where the web app reads it, and
    audit the change in the same transaction. Nothing is written when it is
    unchanged. Returns whether it changed."""
    with conn.transaction():
        changed = conn.execute(
            """
            UPDATE control SET budget_state = %s
             WHERE id = 1 AND budget_state <> %s
            RETURNING id
            """,
            (state, state),
        ).fetchone()
        if changed is not None:
            audit.record(conn, _STATE_KINDS[state])
    return changed is not None


def _next_month(month: datetime) -> datetime:
    if month.month == 12:
        return month.replace(year=month.year + 1, month=1)
    return month.replace(month=month.month + 1)
