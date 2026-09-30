"""The record of every scheduled tick (M15).

Its one question is the one M15's exit criterion asks: over a window, what was
the longest stretch with no successful run? Everything else about a tick lives
in the logs and the spans; this table exists so that "eight unattended days" is
something a query can confirm or refute.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import psycopg


class JobRuns:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def record(
        self,
        job: str,
        started_at: datetime,
        finished_at: datetime,
        *,
        ok: bool,
        seen: int | None = None,
        started: int | None = None,
        failed: int | None = None,
        error: str | None = None,
        token_issued_at: datetime | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO job_runs
                (job, started_at, finished_at, ok, seen, started, failed, error, token_issued_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (job, started_at, finished_at, ok, seen, started, failed, error, token_issued_at),
        )

    def refresh_evidence(self, token_issued_at: datetime) -> tuple[datetime | None, bool]:
        """`(last successful refresh, rejected since)` for one token.

        "Rejected" means Google refused the refresh token (`invalid_grant`)
        after its last success. A later success clears it: the rejection is
        then history, not the token's current state.
        """
        row = self._conn.execute(
            """
            WITH mine AS (
                SELECT finished_at, ok, error FROM job_runs
                 WHERE job = 'token_refresh' AND token_issued_at = %(issued)s
            ), last_ok AS (
                SELECT max(finished_at) AS at FROM mine WHERE ok
            )
            SELECT last_ok.at,
                   EXISTS (
                       SELECT 1 FROM mine
                        WHERE NOT ok AND error = 'invalid_grant'
                          AND finished_at > coalesce(last_ok.at, '-infinity'::timestamptz)
                   )
              FROM last_ok
            """,
            {"issued": token_issued_at},
        ).fetchone()
        if row is None:
            return None, False
        return row[0], bool(row[1])

    def longest_gap(self, job: str, since: datetime, until: datetime) -> timedelta:
        """Longest stretch in `[since, until]` without a successful run of `job`.

        The window's own edges count as boundaries. Without them, a machine
        that came up late or died early would pass: its few ticks are close
        together, and the silence around them is never measured.
        """
        row = self._conn.execute(
            """
            WITH boundaries AS (
                SELECT finished_at AS at
                  FROM job_runs
                 WHERE job = %(job)s AND ok
                   AND finished_at >= %(since)s AND finished_at <= %(until)s
                UNION ALL SELECT %(since)s::timestamptz
                UNION ALL SELECT %(until)s::timestamptz
            )
            SELECT max(at - previous)
              FROM (SELECT at, lag(at) OVER (ORDER BY at) AS previous FROM boundaries) gaps
            """,
            {"job": job, "since": since, "until": until},
        ).fetchone()
        gap = row[0] if row is not None else None
        return gap if gap is not None else timedelta(0)
