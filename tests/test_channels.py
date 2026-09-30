"""Channels (M16, D7): every configured channel hears about a proposal, and
none can stop the others -- by raising, or by hanging."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import SecretStr

from app.channel.channels import Channels, TelegramChannel, configured_channels
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
    announced: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    delivers: bool = True

    def announce_proposal(self, record: ProposalRecord) -> None:
        self.announced.append(record.message_id)

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

    release: threading.Event = field(default_factory=threading.Event)
    name: str = "hanging"

    def announce_proposal(self, record: ProposalRecord) -> None:
        self.release.wait(timeout=30)

    def alert(self, code: str) -> bool:
        self.release.wait(timeout=30)
        return True


def test_every_channel_hears_about_a_proposal() -> None:
    first, second = Recording("first"), Recording("second")

    Channels([first, second]).announce(RECORD)

    assert first.announced == second.announced == ["m1"]


def test_a_failing_channel_does_not_stop_the_others() -> None:
    survivor = Recording()

    Channels([Broken(), survivor]).announce(RECORD)

    assert survivor.announced == ["m1"]


def test_a_hanging_channel_does_not_stop_the_others() -> None:
    hanging = Hanging()
    survivor = Recording()
    try:
        started = time.monotonic()

        Channels([hanging, survivor], timeout=0.3).announce(RECORD)

        assert time.monotonic() - started < 5
        assert survivor.announced == ["m1"]
    finally:
        hanging.release.set()


def test_an_alert_counts_as_delivered_if_any_channel_delivered_it() -> None:
    assert Channels([Broken(), Recording(delivers=True)]).alert("token_expired") is True
    assert Channels([Recording(delivers=False)]).alert("token_expired") is False
    assert Channels([]).alert("token_expired") is False


def test_a_hanging_alert_is_not_counted_as_delivered() -> None:
    hanging = Hanging()
    try:
        assert Channels([hanging], timeout=0.3).alert("token_expired") is False
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


def test_telegram_alerts_in_words_the_owner_can_act_on() -> None:
    bot = FakeBot()

    delivered = TelegramChannel(bot=bot, chat_id=4242, zone="UTC").alert("token_expired")

    assert delivered is True
    assert "reauth" in bot.sent[-1]["text"]
