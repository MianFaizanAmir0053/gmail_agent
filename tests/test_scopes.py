"""Every Google API method the agent calls is covered by the scopes it asks for.

A method outside the grant fails only when it runs, with a 403, in
production: the conflict check's `freebusy.query` did so on the first real
email (2026-10-02), because `calendar.events` does not cover it. The accepted
scopes are copied from each method's reference page, Authorization section:
https://developers.google.com/workspace/calendar/api/v3/reference and
https://developers.google.com/workspace/gmail/api/reference/rest.
"""

from __future__ import annotations

import pytest

from app.google.scopes import SCOPES

CALENDAR = "https://www.googleapis.com/auth/calendar"
GMAIL = "https://www.googleapis.com/auth/gmail"

ACCEPTED: dict[str, set[str]] = {
    # app/google/calendar.py and app/jobs/calendar_probe.py
    "calendar.events.insert": {
        f"{CALENDAR}",
        f"{CALENDAR}.events",
        f"{CALENDAR}.app.created",
        f"{CALENDAR}.events.owned",
    },
    "calendar.events.get": {
        f"{CALENDAR}",
        f"{CALENDAR}.readonly",
        f"{CALENDAR}.events",
        f"{CALENDAR}.events.readonly",
        f"{CALENDAR}.app.created",
        f"{CALENDAR}.events.owned",
        f"{CALENDAR}.events.owned.readonly",
    },
    "calendar.events.list": {
        f"{CALENDAR}",
        f"{CALENDAR}.readonly",
        f"{CALENDAR}.events",
        f"{CALENDAR}.events.readonly",
        f"{CALENDAR}.app.created",
        f"{CALENDAR}.events.owned",
        f"{CALENDAR}.events.owned.readonly",
    },
    "calendar.events.delete": {
        f"{CALENDAR}",
        f"{CALENDAR}.events",
        f"{CALENDAR}.app.created",
        f"{CALENDAR}.events.owned",
    },
    # The conflict check before a card is made (app/tools/calendar_tool.py).
    "calendar.freebusy.query": {
        f"{CALENDAR}",
        f"{CALENDAR}.readonly",
        f"{CALENDAR}.events.freebusy",
        f"{CALENDAR}.freebusy",
    },
    # app/google/gmail.py: every call is a read.
    "gmail.users.getProfile": {"https://mail.google.com/", f"{GMAIL}.readonly"},
    "gmail.users.history.list": {"https://mail.google.com/", f"{GMAIL}.readonly"},
    "gmail.users.messages.get": {"https://mail.google.com/", f"{GMAIL}.readonly"},
    "gmail.users.messages.list": {"https://mail.google.com/", f"{GMAIL}.readonly"},
    "gmail.users.threads.get": {"https://mail.google.com/", f"{GMAIL}.readonly"},
    "gmail.users.threads.list": {"https://mail.google.com/", f"{GMAIL}.readonly"},
}


@pytest.mark.parametrize("method", sorted(ACCEPTED))
def test_the_scopes_cover_every_method_the_agent_calls(method: str) -> None:
    assert ACCEPTED[method] & set(SCOPES), f"no requested scope lets the agent call {method}"


def test_free_busy_is_read_with_the_narrowest_scope_that_allows_it() -> None:
    """Only the owner's own availability: not every event, and not the
    availability of calendars shared with the owner."""
    assert f"{CALENDAR}.freebusy" in SCOPES
    assert not {f"{CALENDAR}", f"{CALENDAR}.readonly", f"{CALENDAR}.events.freebusy"} & set(SCOPES)
