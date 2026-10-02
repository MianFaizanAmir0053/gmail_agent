"""OAuth scopes.

Kept minimal on purpose. `gmail.readonly` is a *restricted* scope and
`calendar.events` is *sensitive*; both raise the verification bar, and asking
for more than we use would raise it further for nothing.

`calendar.freebusy` is for the conflict check that runs before a card is
made: `freebusy.query` accepts no scope narrower than it, and
`calendar.events` does not cover it at all. It reads only whether the owner's
own calendars are busy, never their events. `tests/test_scopes.py` checks
every method the agent calls against this list.

Changing this list invalidates the stored token -- the consent flow must be
re-run, which also resets the seven-day clock (see `tokens.py`).
"""

from __future__ import annotations

from typing import Final

GMAIL_READONLY: Final = "https://www.googleapis.com/auth/gmail.readonly"
CALENDAR_EVENTS: Final = "https://www.googleapis.com/auth/calendar.events"
CALENDAR_FREEBUSY: Final = "https://www.googleapis.com/auth/calendar.freebusy"

SCOPES: Final[list[str]] = [GMAIL_READONLY, CALENDAR_EVENTS, CALENDAR_FREEBUSY]
