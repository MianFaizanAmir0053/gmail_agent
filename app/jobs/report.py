"""Cost and latency report from the trace tables.

    python -m app.jobs.report            # last 7 days
    python -m app.jobs.report --days 30

A stand-in for M09's dashboard, and the thing that reconciles against the
provider's own billing page. Any gap between the two means the pricing table is
stale or a model is unpriced -- both of which this prints rather than hides.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
from typing import Any

import psycopg

from app.config import get_settings
from app.obs.pricing import PRICING_CHECKED_ON, unpriced_models
from app.store.db import connect


def _rows(conn: psycopg.Connection, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
    return list(conn.execute(sql, params).fetchall())


def report(conn: psycopg.Connection, days: int) -> None:
    since = f"{days} days"

    runs = _rows(
        conn,
        """
        SELECT status, count(*), round(avg(duration_ms)), sum(total_cost_usd)
          FROM runs
         WHERE started_at > now() - %s::interval
         GROUP BY status ORDER BY count(*) DESC
        """,
        (since,),
    )

    print(f"Runs (last {days}d)")
    if not runs:
        print("  none")
    for status, count, avg_ms, cost in runs:
        avg = f"{int(avg_ms)}ms" if avg_ms else "-"
        print(f"  {status:<18} {count:>4}   avg {avg:>8}   ${cost or 0:.4f}")

    nodes = _rows(
        conn,
        """
        SELECT node,
               count(*),
               round(avg(latency_ms)),
               round(percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)),
               sum(input_tokens), sum(output_tokens), sum(thinking_tokens),
               sum(cached_tokens), sum(cost_usd),
               count(*) FILTER (WHERE status = 'error')
          FROM spans
         WHERE started_at > now() - %s::interval
         GROUP BY node ORDER BY sum(cost_usd) DESC NULLS LAST, count(*) DESC
        """,
        (since,),
    )

    header = (
        f"  {'node':<16}{'n':>5}{'avg':>8}{'p95':>8}{'in':>9}"
        f"{'out':>8}{'think':>8}{'cached':>8}{'cost':>10}{'err':>5}"
    )
    print("\nNodes")
    print(header)
    for node, n, avg, p95, tin, tout, think, cached, cost, errors in nodes:
        print(
            f"  {node:<16}{n:>5}{int(avg or 0):>7}ms{int(p95 or 0):>7}ms"
            f"{tin or 0:>9}{tout or 0:>8}{think or 0:>8}{cached or 0:>8}"
            f"{'$' + format(cost or 0, '.4f'):>10}{errors:>5}"
        )

    daily = _rows(
        conn,
        """
        SELECT date_trunc('day', started_at)::date, count(*), sum(total_cost_usd)
          FROM runs
         WHERE started_at > now() - %s::interval
         GROUP BY 1 ORDER BY 1
        """,
        (since,),
    )

    print("\nPer day")
    for day, count, cost in daily:
        print(f"  {day}  {count:>4} runs   ${cost or 0:.4f}")

    seen = {row[0] for row in _rows(conn, "SELECT DISTINCT model FROM spans", ()) if row[0]}
    missing = unpriced_models(seen)

    total = conn.execute(
        "SELECT sum(total_cost_usd) FROM runs WHERE started_at > now() - %s::interval", (since,)
    ).fetchone()

    print(f"\nTotal: ${(total[0] if total and total[0] else Decimal(0)):.4f}")
    print(f"Rates last checked {PRICING_CHECKED_ON} -- verify before quoting these.")
    if missing:
        # Unpriced spans record NULL, so they are missing from the total rather
        # than counted as free. Say so instead of letting the figure look complete.
        print(f"!! Unpriced models seen in traces: {', '.join(sorted(missing))}")
        print("   Their spans are excluded from the total. Add them to app/obs/pricing.py.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Trace cost and latency report.")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    with connect(get_settings().database_url) as conn:
        report(conn, args.days)


if __name__ == "__main__":
    main()
