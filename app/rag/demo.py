"""What retrieval finds for a text, beside what extraction makes of it.

    python -m app.rag.demo "Ahmed suggested we do the review Thursday at 3pm"

Once (M11) this ran the same extraction twice, with and without the
`search_context` tool. Since M18 no model that reads mail holds a tool
(decision 3): the extraction runs as production runs it, and the search runs
on its own, showing what M19's planner -- which reads the owner's request,
not the mail -- will have to work with.

The text is an argument rather than a fixture because a convincing example
names a real person from your own mailbox, and a real address does not belong
in a committed file.

Costs one classify call, one extraction call and one embedding.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from app.config import get_settings
from app.contracts import EmailMessage, ExtractionResult
from app.extraction.pipeline import build_pipeline
from app.policy import models
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


def _show(result: ExtractionResult) -> None:
    print("\n--- extraction, no tools ---")
    print(f"  is_meeting  {result.is_meeting}")
    print(f"  title       {result.title}")
    print(f"  start_utc   {result.start_utc}")
    print(f"  attendees   {result.attendees or '(none)'}")
    print(f"  reasoning   {result.reasoning}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Show what retrieval finds beside extraction.")
    parser.add_argument("body", help="The email body to extract from and search for.")
    parser.add_argument("--subject", default="Quick follow-up")
    parser.add_argument("--sender", default="someone@example.com")
    args = parser.parse_args()

    settings = get_settings()
    now = datetime.now(UTC)
    email = _email(args.body, args.sender, args.subject, now)

    # One gate for every call this run makes (M17, D5).
    meter = models.local_gate(settings)
    pipeline = build_pipeline(owner_email=settings.owner_email, gate=meter)
    _show(pipeline(email, now_utc=now, user_timezone=settings.user_timezone))

    with connect(settings.database_url) as conn:
        search = build_context_search(conn, settings, gate=meter)
        print("\n--- search_context, on its own ---")
        for hit in search(args.body):
            print(f"  {hit}")


if __name__ == "__main__":
    main()
