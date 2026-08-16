"""Entrypoint stub.

Exists so the container has something to run and so misconfiguration surfaces
immediately. Replaced by the FastAPI app in M06/M07.
"""

from __future__ import annotations

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    print(f"mailagent ok | env={settings.app_env} dry_run={settings.dry_run}")


if __name__ == "__main__":
    main()
