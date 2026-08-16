"""OAuth scopes.

Kept minimal on purpose. `gmail.readonly` is a *restricted* scope and
`calendar.events` is *sensitive*; both raise the verification bar, and asking
for more than we use would raise it further for nothing.

Changing this list invalidates the stored token -- the consent flow must be
re-run, which also resets the seven-day clock (see `tokens.py`).
"""

from __future__ import annotations

from typing import Final

GMAIL_READONLY: Final = "https://www.googleapis.com/auth/gmail.readonly"
CALENDAR_EVENTS: Final = "https://www.googleapis.com/auth/calendar.events"

SCOPES: Final[list[str]] = [GMAIL_READONLY, CALENDAR_EVENTS]
