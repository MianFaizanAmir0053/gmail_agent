"""Model pricing, deliberately fallible.

Two rules here, both learned the hard way by people who did not follow them:

**Raw token counts are the source of truth.** Rates change. Storing only dollars
means history can never be recomputed, and every past number silently starts
meaning something different from the day the rate moved.

**An unpriced model costs NULL, not zero.** Zero is a positive claim that
something was free. A model missing from this table would then quietly
under-report the total, and the first sign of trouble would be a bill that does
not match the dashboard. `unpriced_models()` exists so the gap is visible.

Rates are USD per million tokens and **must be checked against the current
pricing page** -- they are not an API-reported value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

PRICING_CHECKED_ON = date(2026, 8, 17)
"""When a human last reconciled these against the published rates.

Displayed in reports so a stale table is obvious rather than assumed current.
"""


@dataclass(frozen=True, slots=True)
class Rate:
    """USD per million tokens."""

    input_: Decimal
    output: Decimal
    cached_input: Decimal | None = None
    """Cached input usually bills far below fresh input. When unknown, cached
    tokens are charged at the input rate -- an overestimate, which is the safer
    direction for a cost figure to be wrong in."""


RATES: dict[str, Rate] = {
    # Placeholders pending reconciliation against the published price list.
    # Wrong numbers here are less dangerous than missing ones only because
    # `PRICING_CHECKED_ON` is printed next to every total.
    "gemini-2.5-flash": Rate(Decimal("0.30"), Decimal("2.50"), Decimal("0.075")),
    "gemini-2.5-flash-lite": Rate(Decimal("0.10"), Decimal("0.40"), Decimal("0.025")),
    "gemini-3.5-flash": Rate(Decimal("0.30"), Decimal("2.50"), Decimal("0.075")),
    "gemini-3.5-flash-lite": Rate(Decimal("0.10"), Decimal("0.40"), Decimal("0.025")),
    "gemini-3.6-flash": Rate(Decimal("0.30"), Decimal("2.50"), Decimal("0.075")),
    "gemini-3.7-flash": Rate(Decimal("0.30"), Decimal("2.50"), Decimal("0.075")),
}

MILLION = Decimal(1_000_000)


def cost_usd(
    model: str | None,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    thinking_tokens: int = 0,
) -> Decimal | None:
    """Cost for one call, or None when the model is not priced.

    Thinking tokens bill as output. Folding them into `output_tokens` upstream
    would work arithmetically but would lose the ability to see how much of the
    spend is reasoning -- which is the first thing to tune.
    """
    if model is None:
        return None
    rate = RATES.get(model)
    if rate is None:
        return None

    cached_rate = rate.cached_input if rate.cached_input is not None else rate.input_

    total = (
        Decimal(input_tokens) * rate.input_
        + Decimal(cached_tokens) * cached_rate
        + Decimal(output_tokens + thinking_tokens) * rate.output
    ) / MILLION

    return total.quantize(Decimal("0.000001"))


def unpriced_models(models: set[str]) -> set[str]:
    """Models seen in traces that this table cannot price."""
    return {m for m in models if m and m not in RATES}
