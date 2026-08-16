"""M01 exit criterion, as a runnable check.

    python -m app.google.smoke

Lists ten unread messages and creates-then-deletes an event on the *test*
calendar. Run it twice: once with DRY_RUN=true (must perform zero writes) and
once with DRY_RUN=false.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.config import get_settings
from app.google.auth import build_service, load_credentials, token_store
from app.google.calendar import CalendarClient
from app.google.gmail import GmailClient


def main() -> None:
    settings = get_settings()
    if settings.test_calendar_id is None:
        raise SystemExit("TEST_CALENDAR_ID must be set. Never smoke-test the primary calendar.")

    credentials = load_credentials(settings)

    health = token_store(settings).health()
    flag = "  <-- RUN `python -m app.google.reauth`" if health.needs_reauth_soon else ""
    print(f"Token: {health.days_remaining:.1f} days remaining{flag}\n")

    gmail = GmailClient(build_service("gmail", "v1", credentials))
    ids = gmail.list_unread(max_results=10)
    print(f"Unread messages: {len(ids)}")
    for message_id in ids:
        msg = gmail.get_message(message_id)
        print(f"  [{msg.received_at:%Y-%m-%d %H:%M}] {msg.sender:<32} {msg.subject[:60]}")

    calendar = CalendarClient(
        build_service("calendar", "v3", credentials),
        settings.test_calendar_id,
        dry_run=settings.dry_run,
    )

    start = datetime.now(UTC) + timedelta(days=1)
    print(f"\nCalendar (dry_run={calendar.dry_run}):")
    event_id = calendar.create_event(
        title="mailagent smoke test",
        start_utc=start,
        end_utc=start + timedelta(minutes=30),
        timezone=settings.user_timezone,
        description="Created by app.google.smoke. Safe to delete.",
    )

    if event_id is None:
        print("  dry run -- no event created, no event deleted")
    else:
        print(f"  created {event_id}")
        calendar.delete_event(event_id)
        print(f"  deleted {event_id}")


if __name__ == "__main__":
    main()
