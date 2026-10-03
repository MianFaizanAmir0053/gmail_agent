"""Approval cards, callback routing, and the allowlist.

Since M16 the handler records decisions through `decide()` and never resumes a
thread; the worker applies them. `decide()` is stubbed in the unit tests, and
one integration test runs it for real.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from gmail_payloads import load_cases

from app.channel.decide import DecisionResult
from app.channel.park import ProposalRecord, proposal_from, write_park
from app.policy.hashing import Binding, args_key
from app.store.ledger import MessageLedger, MessageStatus
from app.telegram import cards
from app.telegram.handler import NotAllowedError, TelegramHandler
from app.telegram.notify import send_approval_card

CHAT = 4242
OTHER_CHAT = 9999
MESSAGE_ID = "1a00a9d35c937b2f"

START = datetime(2026, 8, 19, 11, 0, tzinfo=UTC)

PENDING: dict[str, Any] = {
    "message_id": MESSAGE_ID,
    "proposed": {
        "is_meeting": True,
        "title": "Design review",
        "start_utc": START.isoformat(),
        "end_utc": (START + timedelta(hours=1)).isoformat(),
        "timezone": "Asia/Karachi",
        "attendees": ["sara@example.com"],
        "location": None,
        "confidence": 0.9,
        "reasoning": "Sara wrote: Wednesday 4pm PKT.",
    },
    "conflicts": [],
    "dry_run": True,
    "review_issues": [],
    "action_type": "calendar_invite",
    "pipeline_version": "0123456789ab",
}


BINDING = Binding(calendar_id="test-calendar", key=args_key("test-key"))


def _record(revision: int = 2, **pending: Any) -> ProposalRecord:
    return proposal_from(MESSAGE_ID, PENDING | pending, revision, BINDING)


TOKEN = cards.record_token(_record())
assert TOKEN is not None


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
class Decisions:
    """Stands in for `decide()`: records what the handler asked for."""

    answer: DecisionResult = field(default_factory=lambda: DecisionResult("queued", 7))
    made: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, conn: Any, message_id: str, **kwargs: Any) -> DecisionResult:
        self.made.append({"message_id": message_id, **kwargs})
        return self.answer


@pytest.fixture
def decisions(monkeypatch: pytest.MonkeyPatch) -> Decisions:
    fake = Decisions()
    monkeypatch.setattr("app.telegram.handler.decide", fake)
    return fake


def _handler(
    bot: FakeBot, *, allowed: set[int] | None = None, queued: list[int] | None = None
) -> TelegramHandler:
    sink = queued if queued is not None else []
    return TelegramHandler(
        conn=object(),
        bot=bot,
        allowed_chat_ids=frozenset(allowed if allowed is not None else {CHAT}),
        on_queued=lambda: sink.append(1),
    )


def _callback(data: str, chat_id: int = CHAT) -> dict[str, Any]:
    return {
        "callback_query": {
            "id": "cb1",
            "data": data,
            "message": {"chat": {"id": chat_id}, "message_id": 7},
        }
    }


def _button(
    action: str, revision: int = 2, chat_id: int = CHAT, token: str | None = TOKEN
) -> dict[str, Any]:
    """A tap on a current card: its Confirm carries the card's token."""
    carried = token if action == cards.CONFIRM else None
    return _callback(cards.callback_data(action, MESSAGE_ID, revision, carried), chat_id)


def _reply(text: str, replied_to: str) -> dict[str, Any]:
    return {
        "message": {
            "chat": {"id": CHAT},
            "text": text,
            "reply_to_message": {"text": replied_to},
        }
    }


# --- cards -----------------------------------------------------------------


def test_card_renders_times_in_the_users_zone_not_utc() -> None:
    """Approving 11:00 when you meant 16:00 is the failure this step exists to stop."""
    text = cards.approval_card(_record(), zone="Asia/Karachi")
    assert "16:00" in text
    assert "11:00" not in text


def test_card_shows_conflicts_and_the_reviewers_issues() -> None:
    record = _record(
        conflicts=["Overlaps 1 existing event(s)"], review_issues=["the stated zone was ignored"]
    )
    text = cards.approval_card(record, zone="Asia/Karachi")
    assert "Overlaps 1 existing event(s)" in text
    assert "the stated zone was ignored" in text


def test_card_shows_the_dry_run_and_the_revision() -> None:
    text = cards.approval_card(_record(revision=2), zone="UTC")
    assert "dry run" in text
    assert "revision 2" in text


def test_card_never_shows_the_models_reasoning() -> None:
    """The reasoning can quote the email; the stored card leaves it out."""
    text = cards.approval_card(_record(), zone="UTC")
    assert "Sara wrote" not in text
    assert "confidence" not in text


def test_card_escapes_html_from_email_content() -> None:
    """Subjects are attacker-controlled; HTML parse mode would otherwise break."""
    record = _record(proposed=dict(PENDING["proposed"], title="<b>bold</b> & co"))
    text = cards.approval_card(record, zone="UTC")
    assert "&lt;b&gt;bold&lt;/b&gt; &amp; co" in text


def test_card_survives_a_missing_title() -> None:
    record = _record(proposed={"start_utc": None, "end_utc": None})
    assert "(untitled)" in cards.approval_card(record, zone="UTC")


def test_callback_data_carries_the_revision_within_the_64_byte_limit() -> None:
    data = cards.callback_data("cancel", MESSAGE_ID, 3)
    assert len(data.encode()) <= 64
    assert cards.parse_callback(data) == ("cancel", 3, None, MESSAGE_ID)


def test_a_confirm_carries_the_cards_token_within_the_64_byte_limit() -> None:
    """What the owner saw travels with the tap (M17, D2)."""
    data = cards.callback_data("confirm", MESSAGE_ID, 3, TOKEN)
    assert len(data.encode()) <= 64
    assert cards.parse_callback(data) == ("confirm", 3, TOKEN, MESSAGE_ID)


def test_a_proposal_with_nothing_to_bind_gets_no_confirm_button() -> None:
    keyboard = cards.keyboard(MESSAGE_ID, 1, None)
    assert [button["text"] for button in keyboard[0]] == ["✏️ Edit", "✖️ Cancel"]


def test_callback_data_rejects_an_oversized_id() -> None:
    with pytest.raises(ValueError, match="too long"):
        cards.callback_data("confirm", "x" * 80, 1)


def test_a_card_from_before_m16_parses_without_a_revision() -> None:
    assert cards.parse_callback(f"confirm:{MESSAGE_ID}") == ("confirm", None, None, MESSAGE_ID)


def test_edit_prompt_round_trips_the_message_and_the_revision() -> None:
    prompt = cards.edit_prompt(MESSAGE_ID, 2)
    assert cards.edit_target(prompt) == (MESSAGE_ID, 2)


def test_an_edit_prompt_from_before_m16_has_no_revision() -> None:
    assert cards.edit_target(f"Correction for {MESSAGE_ID}\nReply to this message") == (
        MESSAGE_ID,
        None,
    )


def test_unrelated_reply_has_no_edit_target() -> None:
    assert cards.edit_target("hello there") is None


def test_a_sent_card_carries_buttons_for_its_revision() -> None:
    bot = FakeBot()

    send_approval_card(bot, CHAT, _record(revision=2), zone="UTC")

    keyboard = bot.sent[-1]["keyboard"]
    assert keyboard is not None
    assert all(":2:" in button["callback_data"] for button in keyboard[0])


# --- allowlist -------------------------------------------------------------


def test_unknown_chat_is_rejected(decisions: Decisions) -> None:
    """This bot reads your email. The allowlist is not optional hardening."""
    with pytest.raises(NotAllowedError):
        _handler(FakeBot()).handle(_button(cards.CONFIRM, chat_id=OTHER_CHAT))

    assert decisions.made == []


def test_empty_allowlist_rejects_everyone(decisions: Decisions) -> None:
    with pytest.raises(NotAllowedError):
        _handler(FakeBot(), allowed=set()).handle(_button(cards.CONFIRM))


# --- buttons ---------------------------------------------------------------


def test_a_cancel_records_a_decision_on_the_cards_revision(decisions: Decisions) -> None:
    bot = FakeBot()
    queued: list[int] = []

    _handler(bot, queued=queued).handle(_button(cards.CANCEL, revision=2))

    assert decisions.made == [
        {
            "message_id": MESSAGE_ID,
            "action": "cancel",
            "revision": 2,
            "correction": "",
            "via": "telegram",
        }
    ]
    assert "Queued" in bot.texts[-1]
    assert queued == [1]  # the worker is woken


def test_a_confirm_records_the_cards_token_and_this_process_mode(decisions: Decisions) -> None:
    bot = FakeBot()

    _handler(bot).handle(_button(cards.CONFIRM, revision=2))

    assert decisions.made == [
        {
            "message_id": MESSAGE_ID,
            "action": "confirm",
            "revision": 2,
            "via": "telegram",
            "token": TOKEN,
            "dry_run": True,
        }
    ]
    assert "Queued" in bot.texts[-1]


def test_a_confirm_button_from_before_m17_is_refused_with_a_pointer(
    decisions: Decisions,
) -> None:
    """Without a token there is no telling what it approves."""
    bot = FakeBot()

    _handler(bot).handle(_callback(cards.callback_data("confirm", MESSAGE_ID, 2)))

    assert decisions.made == []
    assert "web app" in bot.texts[-1]


def test_callback_is_answered_before_work_starts(decisions: Decisions) -> None:
    """An unanswered callback is re-delivered, which reads as a second tap."""
    bot = FakeBot()

    _handler(bot).handle(_button(cards.CONFIRM))

    assert bot.answered == ["cb1"]


def test_edit_asks_for_a_correction_without_deciding(decisions: Decisions) -> None:
    bot = FakeBot()

    _handler(bot).handle(_button(cards.EDIT, revision=2))

    assert decisions.made == []
    assert bot.sent[-1]["force_reply"] is True
    assert cards.edit_target(bot.sent[-1]["text"]) == (MESSAGE_ID, 2)


def test_a_card_from_before_m16_is_refused_with_a_pointer(decisions: Decisions) -> None:
    """Without a revision there is no telling which version it approves."""
    bot = FakeBot()

    _handler(bot).handle(_callback(f"confirm:{MESSAGE_ID}"))

    assert decisions.made == []
    assert "web app" in bot.texts[-1]


def test_a_stale_card_is_reported_not_decided(decisions: Decisions) -> None:
    decisions.answer = DecisionResult("stale", current_revision=3)
    bot = FakeBot()
    queued: list[int] = []

    _handler(bot, queued=queued).handle(_button(cards.CONFIRM, revision=2))

    assert "out of date" in bot.texts[-1]
    assert queued == []


def test_unknown_action_is_ignored(decisions: Decisions) -> None:
    outcome = _handler(FakeBot()).handle(_callback(f"detonate:2:{MESSAGE_ID}"))

    assert "unknown action" in outcome
    assert decisions.made == []


# --- corrections -----------------------------------------------------------


def test_correction_reply_records_an_edit_on_the_prompts_revision(decisions: Decisions) -> None:
    bot = FakeBot()

    _handler(bot).handle(_reply("4pm not 3pm", cards.edit_prompt(MESSAGE_ID, 2)))

    assert decisions.made == [
        {
            "message_id": MESSAGE_ID,
            "action": "edit",
            "revision": 2,
            "correction": "4pm not 3pm",
            "via": "telegram",
        }
    ]


def test_a_reply_to_an_old_edit_prompt_is_refused(decisions: Decisions) -> None:
    bot = FakeBot()

    _handler(bot).handle(_reply("4pm", f"Correction for {MESSAGE_ID}\nReply to this"))

    assert decisions.made == []
    assert "web app" in bot.texts[-1]


def test_chatter_is_not_treated_as_a_correction(decisions: Decisions) -> None:
    outcome = _handler(FakeBot()).handle(_reply("hi", "some unrelated message"))

    assert decisions.made == []
    assert "not a correction" in outcome


def test_an_empty_correction_is_ignored(decisions: Decisions) -> None:
    outcome = _handler(FakeBot()).handle(_reply("   ", cards.edit_prompt(MESSAGE_ID, 2)))

    assert decisions.made == []
    assert "empty" in outcome


# --- for real (Postgres) ---------------------------------------------------------


@pytest.mark.integration
def test_a_tap_lands_in_the_queue_as_a_telegram_decision(conn: psycopg.Connection) -> None:
    MessageLedger(conn).claim(MESSAGE_ID, MESSAGE_ID)
    with conn.transaction():
        write_park(conn, _record(revision=1), ledger_status=MessageStatus.CLAIMED)
    handler = TelegramHandler(
        conn=conn, bot=FakeBot(), allowed_chat_ids=frozenset({CHAT}), dry_run=True
    )

    handler.handle(_button(cards.CONFIRM, revision=1, token=cards.record_token(_record(1))))

    row = conn.execute("SELECT action, revision, via FROM decisions").fetchone()
    assert row == ("confirm", 1, "telegram")


def test_the_bot_token_stays_out_of_reprs() -> None:
    """A repr lands in logs, assertion messages and error reports."""
    from app.telegram.client import TelegramClient

    assert "123:bot-token" not in repr(TelegramClient("123:bot-token"))


def test_card_marks_guests_outside_the_thread() -> None:
    """Allowed in the web app only (M17, D4)."""
    text = cards.approval_card(_record(outside_guests=["new@example.net"]), zone="UTC")

    assert "Not in this email thread: new@example.net" in text


# --- what the model wrote (M18, D6) ---------------------------------------------------


OUTPUT = load_cases("output")


def _output(case_id: str) -> dict[str, Any]:
    return next(case for case in OUTPUT if case["id"] == case_id)


def _as_received(card: str) -> str:
    """A card's text as Telegram gives it back in `reply_to_message`: the HTML
    parsed away."""
    return html.unescape(re.sub(r"<[^>]+>", "", card))


def test_every_card_opens_with_the_fixed_label() -> None:
    assert cards.approval_card(_record(), zone="UTC").split("\n")[0] == cards.CARD_LABEL


def test_a_title_shaped_like_an_edit_prompt_routes_no_reply(decisions: Decisions) -> None:
    """output-title-edit-prompt: a reply to the card is not taken as a
    correction to the proposal the title names."""
    title = _output("output-title-edit-prompt")["title"]
    card = cards.approval_card(_record(proposed=dict(PENDING["proposed"], title=title)), zone="UTC")

    assert cards.edit_target(_as_received(card)) is None
    outcome = _handler(FakeBot()).handle(_reply("4pm not 3pm", _as_received(card)))

    assert decisions.made == []
    assert "not a correction" in outcome


@pytest.mark.parametrize("case", OUTPUT, ids=lambda case: case["id"])
def test_the_card_shows_the_title_and_location_scrubbed(case: dict[str, Any]) -> None:
    """A failure names the case and the expectation's index, never the text."""
    proposed = dict(PENDING["proposed"], title=case["title"], location=case["location"])
    text = _as_received(cards.approval_card(_record(proposed=proposed), zone="UTC"))

    lost = [index for index, kept in enumerate(case["expect"]["kept"]) if kept not in text]
    left = [index for index, gone in enumerate(case["expect"]["gone"]) if gone in text]
    assert not lost, f"{case['id']}: kept {lost} lost"
    assert not left, f"{case['id']}: gone {left} left"


def test_every_call_that_sends_text_turns_link_previews_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A preview has Telegram fetch a link from the text."""
    from app.telegram.client import TelegramClient

    posted: list[tuple[str, dict[str, Any]]] = []

    def post(url: str, *, json: dict[str, Any], timeout: float) -> Any:
        posted.append((url.rsplit("/", 1)[-1], json))
        return SimpleNamespace(json=lambda: {"ok": True, "result": {}})

    monkeypatch.setattr("app.telegram.client.httpx.post", post)
    bot = TelegramClient("123:bot-token")

    bot.send_message(CHAT, "a card", keyboard=cards.keyboard(MESSAGE_ID, 1, TOKEN))
    bot.send_message(CHAT, "an edit prompt", force_reply=True)
    bot.send_message(CHAT, "a reply")
    bot.edit_message_text(CHAT, 7, "an edited card")

    assert [method for method, _ in posted] == ["sendMessage"] * 3 + ["editMessageText"]
    assert all(body["link_preview_options"] == {"is_disabled": True} for _, body in posted)
