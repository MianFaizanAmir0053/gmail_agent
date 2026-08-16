"""Approval cards, callback routing, and the allowlist. No network."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.store.ledger import MessageStatus
from app.telegram import cards
from app.telegram.handler import NotAllowedError, TelegramHandler

CHAT = 4242
OTHER_CHAT = 9999
MESSAGE_ID = "1a00a9d35c937b2f"

START = datetime(2026, 8, 19, 11, 0, tzinfo=UTC)

PENDING: dict[str, Any] = {
    "message_id": MESSAGE_ID,
    "proposed": {
        "title": "Design review",
        "start_utc": START.isoformat(),
        "end_utc": (START + timedelta(hours=1)).isoformat(),
        "timezone": "Asia/Karachi",
        "attendees": ["sara@example.com"],
        "location": None,
        "confidence": 0.9,
        "reasoning": "Wednesday 4pm PKT.",
    },
    "conflicts": [],
}


@dataclass
class FakeBot:
    sent: list[dict[str, Any]] = field(default_factory=list)
    answered: list[str] = field(default_factory=list)

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        keyboard: list[list[dict[str, str]]] | None = None,
        force_reply: bool = False,
    ) -> dict[str, Any]:
        self.sent.append(
            {"chat_id": chat_id, "text": text, "keyboard": keyboard, "force_reply": force_reply}
        )
        return {"message_id": len(self.sent)}

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.answered.append(callback_id)

    def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None:
        self.sent.append({"chat_id": chat_id, "text": text, "edit": message_id})

    @property
    def texts(self) -> list[str]:
        return [s["text"] for s in self.sent]


@dataclass
class FakeEntry:
    status: MessageStatus
    calendar_event_id: str | None = None


@dataclass
class FakeConn:
    entry: FakeEntry | None = None


@dataclass
class FakeSession:
    """Stands in for GraphSession. `pending_queue` drives what `pending()` sees
    across successive calls, which is how an edit-then-reprompt is modelled."""

    pending_queue: list[dict[str, Any] | None] = field(default_factory=list)
    resumed: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    entry: FakeEntry | None = None
    conn: Any = None

    def pending(self, message_id: str) -> dict[str, Any] | None:
        return self.pending_queue.pop(0) if self.pending_queue else None

    def resume(self, message_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        self.resumed.append((message_id, decision))
        return {}


def _handler(
    session: FakeSession, bot: FakeBot, allowed: set[int] | None = None
) -> TelegramHandler:
    return TelegramHandler(
        session=session,
        bot=bot,
        allowed_chat_ids=frozenset(allowed if allowed is not None else {CHAT}),
        user_timezone="Asia/Karachi",
    )


def _callback(action: str, chat_id: int = CHAT) -> dict[str, Any]:
    return {
        "callback_query": {
            "id": "cb1",
            "data": cards.callback_data(action, MESSAGE_ID),
            "message": {"chat": {"id": chat_id}, "message_id": 7},
        }
    }


def _monkeypatch_ledger(monkeypatch: pytest.MonkeyPatch, entry: FakeEntry | None) -> None:
    monkeypatch.setattr(
        "app.telegram.handler.MessageLedger",
        lambda conn: type("L", (), {"get": lambda s, m: entry})(),
    )


# --- cards -----------------------------------------------------------------


def test_card_renders_times_in_the_users_zone_not_utc() -> None:
    """Approving 11:00 when you meant 16:00 is the failure this step exists to stop."""
    text = cards.approval_card(PENDING, zone="Asia/Karachi")
    assert "16:00" in text
    assert "11:00" not in text


def test_card_shows_conflicts() -> None:
    payload = PENDING | {"conflicts": ["Overlaps 1 existing event(s): 2026-08-19 11:30-12:30 UTC"]}
    assert "⚠️" in cards.approval_card(payload, zone="Asia/Karachi")


def test_card_escapes_html_from_email_content() -> None:
    """Subjects are attacker-controlled; HTML parse mode would otherwise break."""
    payload = {"proposed": dict(PENDING["proposed"], title="<b>bold</b> & co")}
    text = cards.approval_card(payload, zone="UTC")
    assert "&lt;b&gt;bold&lt;/b&gt; &amp; co" in text


def test_card_survives_a_missing_title() -> None:
    payload = {"proposed": {"start_utc": None, "end_utc": None}}
    assert "(untitled)" in cards.approval_card(payload, zone="UTC")


def test_callback_data_stays_within_the_64_byte_limit() -> None:
    assert len(cards.callback_data("confirm", MESSAGE_ID).encode()) <= 64


def test_callback_data_rejects_an_oversized_id() -> None:
    with pytest.raises(ValueError, match="too long"):
        cards.callback_data("confirm", "x" * 80)


def test_edit_prompt_round_trips_the_message_id() -> None:
    prompt = cards.edit_prompt(MESSAGE_ID)
    assert cards.message_id_from_edit_prompt(prompt) == MESSAGE_ID


def test_unrelated_reply_yields_no_message_id() -> None:
    assert cards.message_id_from_edit_prompt("hello there") is None


# --- allowlist -------------------------------------------------------------


def test_unknown_chat_is_rejected() -> None:
    """This bot reads your email. The allowlist is not optional hardening."""
    session = FakeSession()
    handler = _handler(session, FakeBot())

    with pytest.raises(NotAllowedError):
        handler.handle(_callback(cards.CONFIRM, chat_id=OTHER_CHAT))

    assert session.resumed == []


def test_empty_allowlist_rejects_everyone() -> None:
    handler = _handler(FakeSession(), FakeBot(), allowed=set())
    with pytest.raises(NotAllowedError):
        handler.handle(_callback(cards.CONFIRM))


# --- buttons ---------------------------------------------------------------


def test_confirm_resumes_the_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession(pending_queue=[PENDING, None])
    bot = FakeBot()
    _monkeypatch_ledger(monkeypatch, FakeEntry(MessageStatus.CREATED, "evt_1"))

    _handler(session, bot).handle(_callback(cards.CONFIRM))

    assert session.resumed == [(MESSAGE_ID, {"action": "confirm"})]
    assert "evt_1" in bot.texts[-1]


def test_cancel_resumes_with_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession(pending_queue=[PENDING, None])
    bot = FakeBot()
    _monkeypatch_ledger(monkeypatch, FakeEntry(MessageStatus.REJECTED))

    _handler(session, bot).handle(_callback(cards.CANCEL))

    assert session.resumed == [(MESSAGE_ID, {"action": "cancel"})]
    assert "Cancelled" in bot.texts[-1]


def test_callback_is_answered_before_work_starts() -> None:
    """An unanswered callback is re-delivered, which reads as a second tap on a
    button that books calendar events."""
    session = FakeSession(pending_queue=[PENDING, None])
    bot = FakeBot()

    _handler(session, bot).handle(_callback(cards.EDIT))

    assert bot.answered == ["cb1"]


def test_edit_asks_for_a_correction_without_resuming() -> None:
    session = FakeSession(pending_queue=[PENDING])
    bot = FakeBot()

    _handler(session, bot).handle(_callback(cards.EDIT))

    assert session.resumed == []
    assert bot.sent[-1]["force_reply"] is True
    assert MESSAGE_ID in bot.sent[-1]["text"]


def test_stale_proposal_is_reported_not_resumed() -> None:
    session = FakeSession(pending_queue=[None])
    bot = FakeBot()

    _handler(session, bot).handle(_callback(cards.CONFIRM))

    assert session.resumed == []
    assert "no longer waiting" in bot.texts[-1]


def test_unknown_action_is_ignored() -> None:
    session = FakeSession(pending_queue=[PENDING])
    bot = FakeBot()
    update = _callback(cards.CONFIRM)
    update["callback_query"]["data"] = f"detonate:{MESSAGE_ID}"

    outcome = _handler(session, bot).handle(update)

    assert "unknown action" in outcome
    assert session.resumed == []


# --- corrections -----------------------------------------------------------


def _reply(text: str, replied_to: str) -> dict[str, Any]:
    return {
        "message": {
            "chat": {"id": CHAT},
            "text": text,
            "reply_to_message": {"text": replied_to},
        }
    }


def test_correction_reply_resumes_with_edit() -> None:
    session = FakeSession(pending_queue=[PENDING, PENDING])
    bot = FakeBot()

    _handler(session, bot).handle(_reply("4pm not 3pm", cards.edit_prompt(MESSAGE_ID)))

    assert session.resumed == [(MESSAGE_ID, {"action": "edit", "correction": "4pm not 3pm"})]


def test_revised_proposal_comes_back_as_a_new_card() -> None:
    session = FakeSession(pending_queue=[PENDING, PENDING])
    bot = FakeBot()

    _handler(session, bot).handle(_reply("4pm not 3pm", cards.edit_prompt(MESSAGE_ID)))

    assert bot.sent[-1]["keyboard"] is not None


def test_chatter_is_not_treated_as_a_correction() -> None:
    session = FakeSession()
    bot = FakeBot()

    outcome = _handler(session, bot).handle(_reply("hi", "some unrelated message"))

    assert session.resumed == []
    assert "not a correction" in outcome
