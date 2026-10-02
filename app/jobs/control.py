"""Pause or resume the agent, or show its switches (M17, D6).

    python -m app.jobs.control pause
    python -m app.jobs.control resume
    python -m app.jobs.control status

`.\\tasks.ps1 pause`, `.\\tasks.ps1 resume` and `.\\tasks.ps1 status` run it
locally, against the database `.env` names: the output names its host, so a
pause meant for production is never made on another database unnoticed. In
production it runs on the instance, through `fly ssh console`: the image has
no PowerShell.

A resume from here does not wake the worker, which runs in another process:
its next tick, within fifteen seconds, carries on.
"""

from __future__ import annotations

import argparse
from datetime import UTC

from psycopg.conninfo import conninfo_to_dict

from app.config import get_settings
from app.policy import control
from app.store.db import connect_autocommit


def main() -> None:
    parser = argparse.ArgumentParser(description="Pause or resume the agent, or show its state.")
    parser.add_argument("command", choices=("pause", "resume", "status"))
    args = parser.parse_args()
    settings = get_settings()

    with connect_autocommit(settings.database_url) as conn:
        if args.command == "pause":
            print("Paused." if control.switch(conn, paused=True, via="cli") else "Already paused.")
        elif args.command == "resume":
            print("Resumed." if control.switch(conn, paused=False, via="cli") else "Not paused.")
        state = control.read(conn)

    print(f"database: {_host(settings.database_url)}")
    since = state.changed_at.astimezone(UTC)
    print(
        f"paused: {'yes' if state.paused else 'no'} "
        f"(since {since:%Y-%m-%d %H:%M} UTC, via {state.changed_via or 'nothing yet'})"
    )
    print(f"model spending: {state.budget_state}")


def _host(database_url: str) -> str:
    """The database's host, and never the rest of the URL: it carries the
    password."""
    try:
        host = conninfo_to_dict(database_url).get("host")
    except Exception:
        return "(unreadable)"
    return str(host) if host else "(local socket)"


if __name__ == "__main__":
    main()
