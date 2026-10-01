"""Remove a confirmed contact (M17, D4).

    python -m app.jobs.contacts --remove sara@example.com

Allowing is one tap on the card, or `approve --allow`. Removing is here only,
on the command line: a guest removed by mistake is one Allow away, while one
allowed by mistake should take a deliberate step to undo. A proposal that
needs the address again shows it as outside once more.
"""

from __future__ import annotations

import argparse

from app.config import get_settings
from app.policy import contacts
from app.policy.hashing import args_key
from app.store.db import connect_autocommit


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove a confirmed contact.")
    parser.add_argument("--remove", metavar="ADDRESS", required=True)
    args = parser.parse_args()
    settings = get_settings()
    if settings.fernet_key is None:
        raise SystemExit("FERNET_KEY must be set: it keys the audit log's record of a contact.")
    key = args_key(settings.fernet_key.get_secret_value())

    with connect_autocommit(settings.database_url) as conn:
        try:
            removed = contacts.remove(conn, args.remove, key=key)
        except ValueError as exc:
            raise SystemExit(f"{args.remove!r}: {exc}") from None
    name = contacts.guest_key(args.remove)
    print(f"Removed {name}." if removed else f"{name} was not a confirmed contact.")


if __name__ == "__main__":
    main()
