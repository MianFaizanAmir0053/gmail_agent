"""Prompt construction, ordered for cache hits.

The prompt is split stable-then-volatile:

    system_instruction   instructions + conventions + examples   <-- stable
    contents             grounding (today's date) + the email    <-- per call

Gemini 2.5 caches implicitly on a stable prefix rather than on an explicit
breakpoint, so the split still earns its keep -- but only if nothing volatile
leaks into the system instruction. Interpolating the current time there would
change the prefix on every request and silently defeat it. A test guards that.

The examples deliberately do not reuse any golden fixture. Teaching the
conventions is legitimate; teaching the answers would make the eval meaningless.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.contracts import EmailMessage

_CONVENTIONS = """\
Conventions, applied without exception:

- `is_meeting` means "this message should create a calendar event". It does not
  mean "this message mentions a meeting". A cancellation mentions one and must
  be false. A newsletter advertising a webinar the reader never signed up for is
  false. "Let's grab coffee sometime" has no committed time and is false.
- Report times as local wall-clock in `start_local` / `end_local`, with no offset
  and no Z, exactly as a human reading the email would say them. Put the IANA
  zone those times were expressed in into `timezone`. Never do timezone
  arithmetic yourself and never emit an offset like +05:00.
- If the sender states a zone ("9am Pacific"), that zone is the answer. If no
  zone is stated, use the recipient's zone given in the grounding block.
- When only a start time is given, assume 30 minutes if the message calls it
  quick or short, and 60 minutes otherwise.
- An all-day event runs from 00:00 to 24:00 local, so `end_local` is midnight on
  the following day.
- `attendees` lists the other participants' email addresses. Exclude the
  recipient themselves. Exclude distribution lists such as engineering@ or
  all-staff@ -- they are not people.
- When a message reschedules something, extract the NEW time. The old time is a
  decoy.
- Details may live in a quoted or forwarded section. Read the whole body.
- `reasoning` is one sentence stating which words in the email fixed the date,
  the time, and the zone.
"""

_EXAMPLES = """\
Worked examples.

Example A -- explicit, with a stated zone
Email: "Board prep is on 4 March at 09:30-10:15 CET, dial-in only."
From: nils@example.org  To: you@example.com
Output: is_meeting=true, title="Board prep", start_local="2026-03-04T09:30:00",
end_local="2026-03-04T10:15:00", timezone="Europe/Berlin",
attendees=["nils@example.org"], location="Dial-in".

Example B -- a decoy time
Email: "Skipping the Monday 9am standup this week, we'll resume next Monday."
Output: is_meeting=false. Nothing is being scheduled; a recurring slot is being
skipped, and "next Monday" refers to the existing series.

Example C -- start only, described as quick
Email: "Quick sync at 3 on Friday?"  (recipient zone Europe/London, Friday is 6 March)
Output: is_meeting=true, title="Sync", start_local="2026-03-06T15:00:00",
end_local="2026-03-06T15:30:00", timezone="Europe/London". Quick implies 30 minutes.
"""

CLASSIFY_SYSTEM = f"""\
You triage email. For each message decide only whether it should create a
calendar event. Be strict: a false positive puts a wrong entry on someone's
calendar, which is worse than missing an ambiguous one.

{_CONVENTIONS}
Answer only the is_meeting question. Do not extract times.
"""

EXTRACT_SYSTEM = f"""\
You extract calendar events from email.

{_CONVENTIONS}
{_EXAMPLES}
"""

SEARCH_SUFFIX = """\
You can search past email threads with the `search_context` tool.

Use it when the message names a person without giving their address, or refers
to something previously agreed. A first name plus the surrounding context is
usually enough to find them. If the search returns nothing, leave the attendee
out -- never invent an address.
"""
"""Appended to `EXTRACT_SYSTEM` only when the tool is actually wired in.

Kept out of the base prompt on purpose. The frozen baseline was measured against
`EXTRACT_SYSTEM` exactly as it stands, and adding instructions about a tool that
is not present would change that number for reasons unrelated to retrieval --
while also telling the model about a capability it does not have.
"""


def grounding_block(now_utc: datetime, user_timezone: str) -> str:
    """Volatile context. Belongs in the user turn, never in the system prompt.

    The weekday name is spelled out rather than left implicit: models resolve
    "this Thursday" far more reliably when they are not also inferring what day
    it is today.
    """
    local = now_utc.astimezone(ZoneInfo(user_timezone))
    return (
        "Grounding:\n"
        f"- Current time, recipient's zone: {local:%Y-%m-%d %H:%M} ({user_timezone})\n"
        f"- Today is a {local:%A}\n"
        f"- Current time, UTC: {now_utc:%Y-%m-%d %H:%M}\n"
        f"- Recipient's zone, for times with no stated zone: {user_timezone}\n"
    )


def email_block(email: EmailMessage) -> str:
    recipients = ", ".join(email.recipients) or "(none)"
    return (
        "Email:\n"
        f"From: {email.sender}\n"
        f"To: {recipients}\n"
        f"Subject: {email.subject}\n"
        f"Received: {email.received_at:%Y-%m-%d %H:%M} UTC\n"
        "Body:\n"
        f"{email.body_text}\n"
    )


def user_content(email: EmailMessage, *, now_utc: datetime, user_timezone: str) -> str:
    return f"{grounding_block(now_utc, user_timezone)}\n{email_block(email)}"
