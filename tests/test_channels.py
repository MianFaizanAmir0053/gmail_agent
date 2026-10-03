"""Channels (M16, D7): every configured channel hears about a proposal, and
none can stop or starve the others -- by raising, or by hanging."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, get_args

import pytest
from pydantic import SecretStr

from app.channel.channels import (
    TELEGRAM_ALERTS,
    AlertCode,
    Channels,
    TelegramChannel,
    configured_channels,
)
from app.channel.park import ProposalRecord, proposal_from
from app.config import Settings

PENDING: dict[str, Any] = {
    "proposed": {"title": "Design review", "attendees": ["sara@example.com"]},
    "conflicts": [],
    "dry_run": True,
    "action_type": "calendar_invite",
    "pipeline_version": "0123456789ab",
}

RECORD = proposal_from("m1", PENDING, 2)


@dataclass
class Recording:
    name: str = "recording"
    delivers: bool = True
    heard: threading.Event = field(default_factory=threading.Event)
    announced: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)

    def announce_proposal(self, record: ProposalRecord) -> None:
        self.announced.append(record.message_id)
        self.heard.set()

    def alert(self, code: str) -> bool:
        self.alerts.append(code)
        return self.delivers


@dataclass
class Broken:
    name: str = "broken"

    def announce_proposal(self, record: ProposalRecord) -> None:
        raise RuntimeError("push service down")

    def alert(self, code: str) -> bool:
        raise RuntimeError("push service down")


@dataclass
class Hanging:
    """Blocks until released, as a push service that accepts and never answers."""

    name: str = "hanging"
    release: threading.Event = field(default_factory=threading.Event)

    def announce_proposal(self, record: ProposalRecord) -> None:
        self.release.wait(timeout=30)

    def alert(self, code: str) -> bool:
        self.release.wait(timeout=30)
        return True


def test_every_channel_hears_about_a_proposal() -> None:
    first, second = Recording("first"), Recording("second")

    Channels([first, second]).announce(RECORD)

    assert first.heard.wait(5) and second.heard.wait(5)
    assert first.announced == second.announced == ["m1"]


def test_a_failing_channel_does_not_stop_the_others() -> None:
    survivor = Recording()

    Channels([Broken(), survivor]).announce(RECORD)

    assert survivor.heard.wait(5)


def test_announcing_never_waits_even_when_every_channel_hangs() -> None:
    """The caller is a poll, the worker or reconciliation; none of them should
    wait on a push service."""
    hanging = [Hanging(name=f"hanging-{n}") for n in range(3)]
    try:
        started = time.monotonic()

        Channels(list(hanging)).announce(RECORD)

        assert time.monotonic() - started < 1
    finally:
        for channel in hanging:
            channel.release.set()


def test_hung_channels_do_not_starve_a_healthy_one() -> None:
    """No shared pool for hung calls to fill: each call gets its own thread."""
    hanging = [Hanging(name=f"hanging-{n}") for n in range(8)]
    healthy = Recording()
    try:
        for channel in hanging:
            Channels([channel]).announce(RECORD)

        Channels([healthy]).announce(RECORD)

        assert healthy.heard.wait(5)
    finally:
        for channel in hanging:
            channel.release.set()


def test_an_alert_reports_which_channels_delivered_it() -> None:
    channels = Channels([Broken(), Recording("web_push"), Recording("telegram", delivers=False)])

    assert channels.alert("token_expired") == {"web_push"}
    assert Channels([]).alert("token_expired") == set()


def test_a_channel_that_already_delivered_is_not_asked_again() -> None:
    telegram, web_push = Recording("telegram"), Recording("web_push")

    delivered = Channels([telegram, web_push]).alert("token_expired", skip=frozenset({"telegram"}))

    assert delivered == {"web_push"}
    assert telegram.alerts == []


def test_a_hanging_alert_is_not_counted_as_delivered() -> None:
    hanging = Hanging()
    try:
        started = time.monotonic()

        assert Channels([hanging], alert_timeout=0.3).alert("token_expired") == set()

        assert time.monotonic() - started < 5
    finally:
        hanging.release.set()


# --- what is configured ------------------------------------------------------------


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "database_url": "postgresql://localhost/test",
        "gemini_api_key": "test-key",
    }
    return Settings(**(base | overrides))


def test_without_telegram_configured_there_is_no_telegram_channel() -> None:
    channels = configured_channels(_settings())

    assert [c.name for c in channels.channels] == []
    channels.announce(RECORD)  # nothing to tell, and nothing breaks


def test_telegram_needs_both_a_token_and_an_allowlist() -> None:
    assert configured_channels(_settings(telegram_bot_token=SecretStr("123:abc"))).channels == []
    assert configured_channels(_settings(allowed_chat_ids=[4242])).channels == []

    both = configured_channels(
        _settings(telegram_bot_token=SecretStr("123:abc"), allowed_chat_ids=[4242])
    )
    assert [c.name for c in both.channels] == ["telegram"]


@dataclass
class FakeBot:
    sent: list[dict[str, Any]] = field(default_factory=list)

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        keyboard: list[list[dict[str, str]]] | None = None,
        force_reply: bool = False,
    ) -> dict[str, Any]:
        self.sent.append({"chat_id": chat_id, "text": text, "keyboard": keyboard})
        return {}

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        return None

    def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None:
        return None


def test_telegram_announces_a_card_for_the_records_revision() -> None:
    bot = FakeBot()

    TelegramChannel(bot=bot, chat_id=4242, zone="UTC").announce_proposal(RECORD)

    keyboard = bot.sent[-1]["keyboard"]
    assert keyboard is not None
    assert all(":2:m1" in button["callback_data"] for button in keyboard[0])


OUTSIDER = proposal_from(
    "m1",
    PENDING
    | {
        "outside_guests": ["sara@example.com"],
        "guest_sources": {"sara@example.com": "email"},
    },
    2,
)


@dataclass
class Contacts:
    """Stands in for the contacts table: the guest keys the owner allowed."""

    keys: frozenset[str] = frozenset()
    broken: bool = False
    asked: list[list[str]] = field(default_factory=list)

    def allowed(self, guests: Any) -> frozenset[str]:
        self.asked.append(list(guests))
        if self.broken:
            raise ConnectionError("database unavailable")
        return self.keys & frozenset(guests)


def test_telegram_reads_the_allowed_contacts_as_it_sends_a_card() -> None:
    """A guest the owner allowed is shown as one, and not asked for (M18, D5)."""
    bot = FakeBot()
    contacts = Contacts(keys=frozenset({"sara@example.com"}))

    TelegramChannel(bot=bot, chat_id=4242, zone="UTC", contacts=contacts).announce_proposal(
        OUTSIDER
    )

    assert contacts.asked == [["sara@example.com"]]
    assert "sara@example.com: an allowed contact" in bot.sent[-1]["text"]
    assert "Not in this email thread" not in bot.sent[-1]["text"]


def test_telegram_still_sends_the_card_when_the_contacts_cannot_be_read() -> None:
    """It asks for the guest to be allowed, and `decide()` reads the contacts
    again at a Confirm."""
    bot = FakeBot()

    TelegramChannel(
        bot=bot, chat_id=4242, zone="UTC", contacts=Contacts(broken=True)
    ).announce_proposal(OUTSIDER)

    assert "Not in this email thread: sara@example.com" in bot.sent[-1]["text"]


def test_a_configured_telegram_channel_reads_the_contacts_table() -> None:
    from app.channel.channels import DatabaseContacts

    (channel,) = configured_channels(
        _settings(telegram_bot_token=SecretStr("123:abc"), allowed_chat_ids=[4242])
    ).channels
    assert isinstance(channel, TelegramChannel)
    assert isinstance(channel.contacts, DatabaseContacts)


def test_telegram_alerts_in_words_the_owner_can_act_on() -> None:
    bot = FakeBot()

    delivered = TelegramChannel(bot=bot, chat_id=4242, zone="UTC").alert("token_expired")

    assert delivered is True
    assert "reauth" in bot.sent[-1]["text"]


def test_every_alert_has_words_on_every_channel() -> None:
    """A code missing from either table would fail its channel at the moment
    the alert matters. New codes join both."""
    from app.channel.webpush import ALERT_PUSHES

    codes = set(get_args(AlertCode))

    assert {"mail_sync_missed", "mail_feed_stalled"} <= codes
    assert set(TELEGRAM_ALERTS) == codes
    assert set(ALERT_PUSHES) == codes


@pytest.mark.parametrize("code", ["mail_sync_missed", "mail_feed_stalled"])
def test_telegram_tells_the_owner_where_to_look_for_a_mail_alert(code: AlertCode) -> None:
    bot = FakeBot()

    assert TelegramChannel(bot=bot, chat_id=4242, zone="UTC").alert(code) is True
    assert "--status" in bot.sent[-1]["text"]


def test_the_budget_alerts_carry_no_amounts() -> None:
    """Words only (M17, D5): `/health`, with the bearer, shows the spend."""
    from app.channel.webpush import ALERT_PUSHES

    for code in ("budget_warning", "budget_exhausted"):
        for words in (TELEGRAM_ALERTS[code], ALERT_PUSHES[code]["body"]):
            assert "$" not in words and not any(c.isdigit() for c in words.replace("80%", ""))
