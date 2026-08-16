"""Pull real mail into `data/raw_emails/` for labelling.

    python -m app.eval.fetch --query "is:unread" --max 20
    python -m app.eval.fetch --query "meeting OR calendar OR sync" --max 40

Output is real, unredacted mail and is gitignored. Run `app.eval.anonymize`
before anything reaches `data/fixtures/`.

The live inbox skews almost entirely to job alerts and receipts, which is good
coverage for the false-positive half of the set but yields few real meetings --
expect to search the archive deliberately rather than take whatever is unread.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from app.config import get_settings
from app.google.auth import build_service, load_credentials
from app.google.gmail import GmailClient

RAW_DIR = Path("data/raw_emails")


def main() -> None:
    parser = argparse.ArgumentParser(description="Download messages for eval labelling.")
    parser.add_argument("--query", default="is:unread", help="Gmail search query")
    parser.add_argument("--max", type=int, default=20, dest="max_results")
    args = parser.parse_args()

    settings = get_settings()
    service = build_service("gmail", "v1", load_credentials(settings))
    gmail = GmailClient(service)

    listing = cast(
        dict[str, Any],
        service.users()
        .messages()
        .list(userId="me", q=args.query, maxResults=args.max_results)
        .execute(),
    )
    message_ids = [cast(str, m["id"]) for m in listing.get("messages", [])]

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    for message_id in message_ids:
        email = gmail.get_message(message_id)
        (RAW_DIR / f"{email.id}.json").write_text(
            json.dumps(email.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    print(f"Wrote {len(message_ids)} messages to {RAW_DIR}/ (gitignored, unredacted).")
    print("Next: python -m app.eval.anonymize")


if __name__ == "__main__":
    main()
