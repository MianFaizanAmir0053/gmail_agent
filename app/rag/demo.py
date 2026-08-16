"""Before/after evidence for M11.

    python -m app.rag.demo "Ahmed suggested we do the review Thursday at 3pm"

Runs the same extraction twice over the same text -- once with `search_context`
withheld and once with it wired in -- and prints what changed. The exit criterion
for this module is a case the extractor demonstrably could not resolve before,
and a claim like that is worth being able to re-run rather than remember.

The email text is an argument rather than a fixture because a convincing example
names a real person from your own mailbox, and a real address does not belong in
a committed file.

Costs two extraction calls plus a turn for each search the model chooses to make.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from app.config import get_settings
from app.contracts import EmailMessage, ExtractionResult
from app.extraction.pipeline import build_pipeline
from app.rag.search import build_context_search
from app.store.db import connect


def _email(body: str, sender: str, subject: str, now: datetime) -> EmailMessage:
    return EmailMessage(
        id="demo",
        thread_id="demo",
        subject=subject,
        body_text=body,
        sender=sender,
        recipients=[get_settings().owner_email or "me@example.com"],
        received_at=now,
    )


def _show(label: str, result: ExtractionResult, searches: int) -> None:
    print(f"\n--- {label} ---")
    print(f"  is_meeting  {result.is_meeting}")
    print(f"  title       {result.title}")
    print(f"  start_utc   {result.start_utc}")
    print(f"  attendees   {result.attendees or '(none)'}")
    print(f"  searches    {searches}")
    print(f"  reasoning   {result.reasoning}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Show what retrieval adds to extraction.")
    parser.add_argument("body", help="The email body to extract from.")
    parser.add_argument("--subject", default="Quick follow-up")
    parser.add_argument("--sender", default="someone@example.com")
    args = parser.parse_args()

    settings = get_settings()
    now = datetime.now(UTC)
    email = _email(args.body, args.sender, args.subject, now)
    zone = settings.user_timezone

    blind = build_pipeline(owner_email=settings.owner_email)
    _show(
        "without search_context",
        blind.extract(email, now_utc=now, user_timezone=zone),
        blind.stats.search_calls,
    )

    with connect(settings.database_url) as conn:
        searching = build_pipeline(
            owner_email=settings.owner_email,
            searcher=build_context_search(conn, settings),
        )
        _show(
            "with search_context",
            searching.extract(email, now_utc=now, user_timezone=zone),
            searching.stats.search_calls,
        )


if __name__ == "__main__":
    main()
