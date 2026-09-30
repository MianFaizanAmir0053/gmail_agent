"""M15's mail measurement: counts only, from metadata only.

Every rule here decides whether a thread counts as a loose end, so every rule
gets a test. The last tests check that no header value ever reaches the
output.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.google.gmail import MessageMeta
from app.jobs.measure import (
    MailWindow,
    WindowError,
    is_automated,
    normalise_address,
    summarise_mail,
    to_markdown,
)

OWNER = "meowner@gmail.com"
KARACHI = ZoneInfo("Asia/Karachi")
SINCE = datetime(2026, 9, 14, tzinfo=UTC)
UNTIL = datetime(2026, 9, 28, tzinfo=UTC)
NOW = UNTIL + timedelta(days=2)
WINDOW = MailWindow(since=SINCE, until=UNTIL, timezone=KARACHI)


def _meta(
    message_id: str,
    thread_id: str,
    at: datetime,
    *,
    labels: tuple[str, ...] = ("INBOX", "CATEGORY_PERSONAL"),
    **headers: str,
) -> MessageMeta:
    names = {
        "from_": "From",
        "to": "To",
        "cc": "Cc",
        "subject": "Subject",
        "list_unsubscribe": "List-Unsubscribe",
        "auto_submitted": "Auto-Submitted",
        "precedence": "Precedence",
    }
    return MessageMeta(
        id=message_id,
        thread_id=thread_id,
        label_ids=frozenset(labels),
        internal_date=at,
        headers=MappingProxyType({names[key]: value for key, value in headers.items()}),
    )


def _ask(message_id: str, thread_id: str, at: datetime, **headers: str) -> MessageMeta:
    """A human writing to the owner in the Primary tab."""
    defaults = {"from_": "sara@example.com", "to": OWNER, "subject": "Contract"}
    merged: dict[str, Any] = defaults | headers
    return _meta(message_id, thread_id, at, **merged)


def _reply(message_id: str, thread_id: str, at: datetime) -> MessageMeta:
    return _meta(message_id, thread_id, at, labels=("SENT",), from_=OWNER, to="sara@example.com")


def _summary(
    *threads: list[MessageMeta], owners: frozenset[str] = frozenset({OWNER})
) -> dict[str, Any]:
    return summarise_mail(list(threads), WINDOW, owners=owners, excluded=frozenset(), now=NOW)


T = SINCE + timedelta(days=2)


# --- who wrote it, to whom -------------------------------------------------


def test_an_unanswered_ask_is_flagged() -> None:
    assert _summary([_ask("a", "t", T)])["flagged_threads"] == 1


def test_a_reply_within_48_hours_closes_it() -> None:
    thread = [_ask("a", "t", T), _reply("r", "t", T + timedelta(hours=47))]
    assert _summary(thread)["flagged_threads"] == 0


def test_a_reply_after_48_hours_is_flagged_and_answered_later() -> None:
    summary = _summary([_ask("a", "t", T), _reply("r", "t", T + timedelta(hours=49))])
    assert summary["flagged_threads"] == 1
    assert summary["flagged_answered_later"] == 1


def test_the_sent_label_marks_the_owner_whatever_the_from_address() -> None:
    """Aliases that send through Gmail carry SENT; the From header varies."""
    reply = _meta("r", "t", T + timedelta(hours=1), labels=("SENT",), from_="alias@work.com")
    assert _summary([_ask("a", "t", T), reply])["flagged_threads"] == 0


def test_a_draft_is_not_a_reply() -> None:
    draft = _meta("d", "t", T + timedelta(hours=1), labels=("DRAFT",), from_=OWNER)
    assert _summary([_ask("a", "t", T), draft])["flagged_threads"] == 1


def test_mail_not_addressed_to_the_owner_is_not_an_ask() -> None:
    """A list the owner reads is not someone waiting on them."""
    assert _summary([_ask("a", "t", T, to="team@company.com")])["flagged_threads"] == 0


def test_gmail_address_variants_are_the_same_owner() -> None:
    assert normalise_address("Me.Owner+work@GoogleMail.com") == OWNER
    assert _summary([_ask("a", "t", T, to="Me.Owner+work@googlemail.com")])["flagged_threads"] == 1


def test_an_alias_counts_as_the_owner() -> None:
    summary = _summary(
        [_ask("a", "t", T, to="faizan@work.com")], owners=frozenset({OWNER, "faizan@work.com"})
    )
    assert summary["flagged_threads"] == 1


# --- machines --------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {"list_unsubscribe": "<mailto:unsub@x.com>"},
        {"auto_submitted": "auto-generated"},
        {"precedence": "bulk"},
        {"from_": "no-reply@shop.com"},
        {"from_": "calendar-notification@google.com"},
        {"subject": "Invitation: Design review @ Wed 4pm"},
    ],
)
def test_automated_mail_is_not_an_ask(headers: dict[str, str]) -> None:
    assert is_automated(_ask("a", "t", T, **headers), excluded=frozenset())


def test_auto_submitted_no_means_a_human_sent_it() -> None:
    assert not is_automated(_ask("a", "t", T, auto_submitted="no"), excluded=frozenset())


def test_an_excluded_sender_is_ignored() -> None:
    """The planted day-1 meeting must not become an ask."""
    ask = _ask("a", "t", T, from_="Planted <planted@example.com>")
    assert is_automated(ask, excluded=frozenset({"planted@example.com"}))


def test_other_tabs_are_counted_but_not_asks() -> None:
    update = _meta(
        "a", "t", T, labels=("INBOX", "CATEGORY_UPDATES"), from_="sara@example.com", to=OWNER
    )
    summary = _summary([update])
    assert summary["flagged_threads"] == 0
    assert summary["inbound_other_category"] == 1


# --- threads, days, rates --------------------------------------------------


def test_a_thread_counts_once_however_many_asks_it_holds() -> None:
    thread = [_ask("a1", "t", T), _ask("a2", "t", T + timedelta(hours=2))]
    assert _summary(thread)["flagged_threads"] == 1


def test_days_are_the_owners_days() -> None:
    """20:00 UTC on the 16th is already the 17th in Karachi."""
    late = datetime(2026, 9, 16, 20, 0, tzinfo=UTC)
    summary = _summary([_ask("a", "t", late)])
    assert summary["inbound_per_day"]["2026-09-17"] == 1


def test_the_weekly_rate_divides_by_the_window() -> None:
    threads = [[_ask(f"a{i}", f"t{i}", T + timedelta(hours=i))] for i in range(6)]
    assert _summary(*threads)["flagged_per_week"] == 3.0  # 6 over two weeks


def test_mail_without_category_labels_is_counted() -> None:
    """With Gmail's tabs turned off there are no CATEGORY_* labels at all,
    and 'Primary' means nothing. The count says how much that affected."""
    bare = _meta("a", "t", T, labels=("INBOX",), from_="sara@example.com", to=OWNER)
    assert _summary([bare])["inbound_no_category"] == 1


def test_messages_outside_the_window_are_not_counted() -> None:
    early = _ask("a", "t", SINCE - timedelta(days=1))
    assert _summary([early])["inbound_total"] == 0


# --- the window itself -----------------------------------------------------


def test_a_window_ending_within_48_hours_cannot_be_judged() -> None:
    with pytest.raises(WindowError):
        MailWindow.checked(
            since=NOW - timedelta(days=14),
            until=NOW - timedelta(hours=10),
            timezone=KARACHI,
            now=NOW,
        )


def test_a_window_that_ends_before_it_starts_is_refused() -> None:
    with pytest.raises(WindowError):
        MailWindow.checked(since=UNTIL, until=SINCE, timezone=KARACHI, now=NOW)


# --- nothing personal leaves -----------------------------------------------


def test_no_header_value_reaches_the_output() -> None:
    secret = "ZEBRA-7731"
    thread = [
        _ask(
            "a",
            "t",
            T,
            from_=f"{secret} <{secret.lower()}@example.com>",
            subject=f"Re: {secret} salary",
        )
    ]
    summary = _summary(thread)

    assert secret.lower() not in json.dumps(summary).lower()
    assert secret.lower() not in to_markdown(summary).lower()


def test_the_default_utc_zone_is_refused_without_an_explicit_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counting the owner's days in UTC would move every evening to the wrong date."""
    from app.config import Settings
    from app.jobs import measure

    settings = Settings(_env_file=None, database_url="postgresql://x/y", gemini_api_key="k")
    monkeypatch.setattr("app.config.get_settings", lambda: settings)

    with pytest.raises(SystemExit, match="--timezone"):
        measure.main(["mail", "--since", "2026-09-14", "--until", "2026-09-28"])
