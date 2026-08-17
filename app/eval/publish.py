"""Publish eval results from `results/*.json` into Postgres.

    python -m app.eval.publish

Deliberately a separate step from `app.eval.run`. Scoring an extractor has
nothing to do with persistence, and the harness must stay runnable with no
database -- on a laptop, in CI, or anywhere the agent itself is not set up.

The JSON files remain the committed evidence. This is a published copy so the
dashboard can query eval history like everything else, rather than reading the
repository from a container that does not have it.

Idempotent: keyed on filename, so re-running publishes only what is new.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import psycopg

from app.config import get_settings
from app.eval.report import RESULTS_DIR
from app.store.db import connect


def publish(conn: psycopg.Connection, directory: Path = RESULTS_DIR) -> tuple[int, int]:
    """Returns `(published, skipped)`."""
    if not directory.exists():
        raise SystemExit(f"No results directory at {directory.resolve()}")

    published = skipped = 0

    for path in sorted(directory.glob("*.json")):
        try:
            data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"  skipped {path.name}: not valid JSON")
            skipped += 1
            continue

        if "headline_exact_match" not in data:
            skipped += 1
            continue

        if data.get("errors"):
            # A run where fixtures raised is not a measurement. Errors score as
            # "not a meeting", so publishing one would draw a quota outage on
            # the accuracy chart as though the extractor had got worse.
            print(f"  skipped {path.name}: {len(data['errors'])} fixture(s) errored")
            skipped += 1
            continue

        row = conn.execute(
            """
            INSERT INTO eval_runs (
                source_file, ran_at, extractor, fixtures, exact_match,
                is_meeting_f1, attendees_f1, fields, failures
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_file) DO NOTHING
            RETURNING id
            """,
            (
                path.name,
                data["timestamp"],
                data["extractor"],
                data["fixtures"],
                data["headline_exact_match"],
                (data.get("is_meeting") or {}).get("f1"),
                data.get("attendees_micro_f1"),
                json.dumps(data.get("fields") or {}),
                json.dumps(data.get("failures") or []),
            ),
        ).fetchone()

        if row is None:
            skipped += 1
        else:
            published += 1
            print(
                f"  published {path.name}  {data['extractor']} {data['headline_exact_match']:.1%}"
            )

    conn.commit()
    return published, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish eval results to Postgres.")
    parser.add_argument("--dir", type=Path, default=RESULTS_DIR)
    args = parser.parse_args()

    with connect(get_settings().database_url) as conn:
        published, skipped = publish(conn, args.dir)

    print(f"\nPublished {published}, already present or unusable {skipped}.")


if __name__ == "__main__":
    main()
