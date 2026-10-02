"""Pause or resume the agent, or show its switches (M17, D6).

    python -m app.jobs.control pause
    python -m app.jobs.control resume
    python -m app.jobs.control status

`.\\tasks.ps1 pause` and `.\\tasks.ps1 resume` run it locally, against the
database `.env` names. In production it runs on the instance, through
`fly ssh console`: the image has no PowerShell.
"""

from __future__ import annotations

import argparse

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

    print(
        f"paused: {'yes' if state.paused else 'no'} "
        f"(since {state.changed_at:%Y-%m-%d %H:%M} UTC, via {state.changed_via or 'nothing yet'})"
    )
    print(f"model spending: {state.budget_state}")


if __name__ == "__main__":
    main()
