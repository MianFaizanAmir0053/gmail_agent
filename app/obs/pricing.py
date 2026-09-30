"""Model pricing, deliberately fallible.

Three rules here, all learned the hard way by people who did not follow them:

**Raw token counts are the source of truth.** Rates change. Storing only dollars
means history can never be recomputed, and every past number silently starts
meaning something different from the day the rate moved.

**An unpriced model costs NULL, not zero.** Zero is a positive claim that
something was free. A model missing from this table would then quietly
under-report the total, and the first sign of trouble would be a bill that does
not match the dashboard. `unpriced_models()` exists so the gap is visible.

**A call is priced at the rate in force when it ran.** Price changes are
announced ahead of time, so every rate carries the day it takes effect.
Recomputing old spans at today's rate instead would rewrite history the moment a
price moved -- the exact failure the first rule exists to prevent.

Rates are USD per million tokens and **must be checked against the current
pricing page** -- they are not an API-reported value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

PRICING_CHECKED_ON = date(2026, 9, 30)
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

    effective_from: date = date.min
    """First UTC day this rate applies. `date.min` means as far back as this
    table knows: older price lists were never recorded here, so a model's
    earliest known rate also prices anything that ran before it."""


_FLASH_PRICE_RISE = date(2027, 1, 1)

RATES: dict[str, tuple[Rate, ...]] = {
    # Paid Standard tier, text input, from https://ai.google.dev/gemini-api/docs/pricing
    # on PRICING_CHECKED_ON. Batch, Flex and Priority bill differently; every
    # call this app makes is Standard.
    #
    # gemini-2.5-pro is absent on purpose: it bills by prompt size (above and
    # below 200k tokens), which one flat rate cannot express, so its spans record
    # NULL and surface as unpriced rather than as a guess.
    "gemini-2.5-flash": (Rate(Decimal("0.30"), Decimal("2.50"), Decimal("0.03")),),
    "gemini-2.5-flash-lite": (Rate(Decimal("0.10"), Decimal("0.40"), Decimal("0.01")),),
    "gemini-3.1-flash-lite": (Rate(Decimal("0.25"), Decimal("1.50"), Decimal("0.025")),),
    "gemini-3.5-flash": (Rate(Decimal("1.50"), Decimal("9.00"), Decimal("0.15")),),
    "gemini-3.5-flash-lite": (Rate(Decimal("0.30"), Decimal("2.50"), Decimal("0.03")),),
    # 3.6-3.8 Flash are published at one price through 2026-12-31 and double
    # from 2027-01-01.
    "gemini-3.6-flash": (
        Rate(Decimal("0.75"), Decimal("3.75"), Decimal("0.075")),
        Rate(Decimal("1.50"), Decimal("7.50"), Decimal("0.15"), effective_from=_FLASH_PRICE_RISE),
    ),
    "gemini-3.7-flash": (
        Rate(Decimal("0.75"), Decimal("3.75"), Decimal("0.075")),
        Rate(Decimal("1.50"), Decimal("7.50"), Decimal("0.15"), effective_from=_FLASH_PRICE_RISE),
    ),
    "gemini-3.8-flash": (
        Rate(Decimal("0.75"), Decimal("3.75"), Decimal("0.075")),
        Rate(Decimal("1.50"), Decimal("7.50"), Decimal("0.15"), effective_from=_FLASH_PRICE_RISE),
    ),
}

MILLION = Decimal(1_000_000)


def _rate_day(at: datetime) -> date:
    """The day a dated rate is looked up by: `at`'s calendar day in UTC.

    Normalised here rather than trusting `at.date()`, because psycopg hands
    TIMESTAMPTZ back in the session's zone -- a stored `started_at` would
    otherwise reprice on a different day from the one it was written on.

    The pricing page does not say which timezone its change dates follow. If
    they follow US Pacific midnight instead, the hours between the two
    midnights get the new rate early: high for a rise like the one in this
    table, which is the safer direction to be wrong in.
    """
    if at.utcoffset() is None:
        raise ValueError("cannot price at a naive datetime: its calendar day is ambiguous")
    return at.astimezone(UTC).date()


def rate_at(model: str | None, at: datetime) -> Rate | None:
    """The rate in force for `model` at `at`, or None when the table has none."""
    day = _rate_day(at)
    if model is None:
        return None
    in_force = [rate for rate in RATES.get(model, ()) if rate.effective_from <= day]
    return max(in_force, key=lambda rate: rate.effective_from, default=None)


def cost_usd(
    model: str | None,
    *,
    at: datetime,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    thinking_tokens: int = 0,
) -> Decimal | None:
    """Cost for one call made at `at`, or None when the model is not priced then.

    `input_tokens` is the whole prompt and `cached_tokens` the share of it that
    was served from cache: Gemini's `prompt_token_count` already includes
    `cached_content_token_count`. The cached share is carved out of the input
    and billed once, at the cached rate. Adding it on top, as this once did,
    billed every cached token twice and made a cache hit dearer than a miss.

    Thinking tokens bill as output. Folding them into `output_tokens` upstream
    would work arithmetically but would lose the ability to see how much of the
    spend is reasoning -- which is the first thing to tune.
    """
    if cached_tokens > input_tokens:
        raise ValueError(
            f"cached_tokens ({cached_tokens}) exceeds input_tokens ({input_tokens}): "
            "cached tokens are part of the prompt, not additional to it"
        )

    rate = rate_at(model, at)
    if rate is None:
        return None

    cached_rate = rate.cached_input if rate.cached_input is not None else rate.input_

    total = (
        Decimal(input_tokens - cached_tokens) * rate.input_
        + Decimal(cached_tokens) * cached_rate
        + Decimal(output_tokens + thinking_tokens) * rate.output
    ) / MILLION

    return total.quantize(Decimal("0.000001"))


def unpriced_models(models: set[str]) -> set[str]:
    """Models seen in traces that this table cannot price."""
    return {m for m in models if m and m not in RATES}
