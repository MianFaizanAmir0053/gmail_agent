"""Recompute stored costs from stored token counts.

    python -m app.jobs.reprice             # reprice spans, then re-total finished runs
    python -m app.jobs.reprice --dry-run   # the same pass, rolled back

`spans` keeps raw tokens beside the dollars so that a wrong rate table can be
recovered from (see `app.obs.pricing`); this is the recovery. Each span is
priced by the call the tracer makes when it writes one -- same tokens, same
`started_at` -- so a span the current table already priced does not move, and
a second run changes nothing.

Run it after the corrected table is deployed, not before: a process still on the
old table goes on writing old costs. Running it again picks those up.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

import psycopg

from app.config import get_settings
from app.obs.pricing import PRICING_CHECKED_ON, cost_usd
from app.store.db import connect


@dataclass(frozen=True, slots=True)
class Repricing:
    """What one pass found. A `None` total is SQL's NULL: nothing in it was priced."""

    spans_checked: int
    spans_changed: int
    runs_checked: int
    runs_changed: int
    spans_total_before: Decimal | None
    spans_total_after: Decimal | None
    runs_total_before: Decimal | None
    runs_total_after: Decimal | None
    unpriced: dict[str, int]
    """Model -> spans left NULL because the table has no rate for it."""


def reprice(conn: psycopg.Connection, *, dry_run: bool = False) -> Repricing:
    """Reprice every span that has a model, then re-total every finished run.

    One transaction, so nothing ever reads corrected spans under run totals
    still summed from the old ones. A dry run is the same pass rolled back, so
    what it reports is what a real run does rather than a separate guess at it.
    """
    with conn.transaction():
        repricing = _reprice(conn)
        if dry_run:
            raise psycopg.Rollback()
    return repricing


def _reprice(conn: psycopg.Connection) -> Repricing:
    spans_before, runs_before = _totals(conn)

    rows = conn.execute(
        """
        SELECT id, model, started_at, input_tokens, output_tokens,
               cached_tokens, thinking_tokens, cost_usd
          FROM spans
         WHERE model IS NOT NULL
         ORDER BY id
        """
    ).fetchall()

    changed: list[tuple[Decimal | None, int]] = []
    unpriced: Counter[str] = Counter()
    for span_id, model, started_at, input_, output, cached, thinking, stored in rows:
        try:
            cost = cost_usd(
                model,
                at=started_at,
                input_tokens=input_,
                output_tokens=output,
                cached_tokens=cached,
                thinking_tokens=thinking,
            )
        except ValueError as exc:
            # Tokens that cannot be priced are a recording bug, not something to
            # guess around. Name the row; the transaction keeps nothing.
            raise ValueError(f"span {span_id}: {exc}") from exc

        if cost is None:
            # NULL, never 0 -- written over an old figure too, since a model
            # the table cannot price has no current rate to vouch for it.
            unpriced[model] += 1
        if cost != stored:
            changed.append((cost, span_id))

    with conn.cursor() as cursor:
        cursor.executemany("UPDATE spans SET cost_usd = %s WHERE id = %s", changed)

    # `Tracer.finish_run`'s total, for the runs it has already finished. Those
    # are marked by `ended_at`, not by a non-NULL total: finish_run leaves the
    # total NULL when nothing in the run was priced, and such a run needs one
    # once its spans are. Runs still in flight are left to finish_run itself,
    # which will sum the corrected spans when it gets there.
    runs_changed = conn.execute(
        """
        WITH fresh AS (
            SELECT runs.trace_id, SUM(spans.cost_usd) AS total
              FROM runs
              LEFT JOIN spans ON spans.trace_id = runs.trace_id
             WHERE runs.ended_at IS NOT NULL
             GROUP BY runs.trace_id
        )
        UPDATE runs
           SET total_cost_usd = fresh.total
          FROM fresh
         WHERE runs.trace_id = fresh.trace_id
           AND runs.total_cost_usd IS DISTINCT FROM fresh.total
        """
    ).rowcount

    finished = conn.execute("SELECT count(*) FROM runs WHERE ended_at IS NOT NULL").fetchone()
    spans_after, runs_after = _totals(conn)

    return Repricing(
        spans_checked=len(rows),
        spans_changed=len(changed),
        runs_checked=finished[0] if finished else 0,
        runs_changed=runs_changed,
        spans_total_before=spans_before,
        spans_total_after=spans_after,
        runs_total_before=runs_before,
        runs_total_after=runs_after,
        unpriced=dict(unpriced),
    )


def _totals(conn: psycopg.Connection) -> tuple[Decimal | None, Decimal | None]:
    spans, runs = conn.execute(
        "SELECT (SELECT SUM(cost_usd) FROM spans), (SELECT SUM(total_cost_usd) FROM runs)"
    ).fetchone() or (None, None)
    return spans, runs


def _usd(value: Decimal | None) -> str:
    # A NULL sum means nothing in it was priced; "$0" would say it was free.
    return "NULL" if value is None else f"${value:.6f}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Recompute stored costs from stored tokens.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do the whole pass and report it, then roll it back.",
    )
    args = parser.parse_args()

    with connect(get_settings().database_url) as conn:
        result = reprice(conn, dry_run=args.dry_run)

    if args.dry_run:
        print("Dry run: rolled back, nothing written.\n")
    print(f"  spans with a model   {result.spans_checked:>6}")
    print(f"    cost changed       {result.spans_changed:>6}")
    print(f"  finished runs        {result.runs_checked:>6}")
    print(f"    total changed      {result.runs_changed:>6}")

    print(f"\n  {'':<21}{'before':>12}{'after':>12}")
    for label, before, after in (
        ("spans.cost_usd", result.spans_total_before, result.spans_total_after),
        ("runs.total_cost_usd", result.runs_total_before, result.runs_total_after),
    ):
        print(f"  {label:<21}{_usd(before):>12}{_usd(after):>12}")

    print(f"\nRates last checked {PRICING_CHECKED_ON} -- verify before quoting these.")
    if result.unpriced:
        listed = ", ".join(f"{model} ({n} spans)" for model, n in sorted(result.unpriced.items()))
        print(f"!! Unpriced models, left NULL: {listed}")
        print("   Their spans are excluded from both totals. Add them to app/obs/pricing.py.")


if __name__ == "__main__":
    main()
