"""Channels (M16, D7): how the owner hears that something needs them.

Every configured channel is told, and none can stop or starve the others.
Each call gets a thread of its own -- no shared pool that hung calls could
fill -- so a channel that raises is logged, one that hangs is left behind, and
the rest are told regardless.

Announcing does not wait at all: the caller is a poll, the worker or
reconciliation, and none of them should wait on a push service. An alert does
wait, for longer than any channel's own timeouts, because its result decides
whether it is recorded as delivered.

A channel gets the stored proposal record: the card's fields, the revision
and the mode, never the raw interrupt payload, which carries the model's
reasoning and can quote the email.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Literal, Protocol

from app.channel.park import ProposalRecord
from app.channel.webpush import DatabaseSubscriptions, WebPushChannel
from app.config import Settings
from app.policy import contacts
from app.store.db import connect_autocommit
from app.telegram.client import Sender, TelegramClient
from app.telegram.notify import admin_chat_id, send_approval_card

log = logging.getLogger(__name__)

AlertCode = Literal[
    "token_expiring",
    "token_expired",
    "standby_in_use",
    # The mail recall's (M20, D5), once a day each.
    "mail_sync_missed",
    "mail_feed_stalled",
    # The spend cap's (M17, D5), once per month and cap value each.
    "budget_warning",
    "budget_exhausted",
    # A calendar write that could not be confirmed (M17, D3), once per decision.
    "write_unconfirmed",
]
"""Every alert a channel can be asked to deliver. A new code needs words in
`TELEGRAM_ALERTS` and a payload in `app.channel.webpush.ALERT_PUSHES`."""

ALERT_TIMEOUT = 60.0
"""Seconds an alert waits for its channels. Longer than any channel's own
worst case -- web push's send budget plus one send, Telegram's connect and
read timeouts -- so a delivery that succeeds is counted as one."""


class Channel(Protocol):
    name: str

    def announce_proposal(self, record: ProposalRecord) -> None: ...

    def alert(self, code: AlertCode) -> bool:
        """True once the alert was delivered, so it is not sent again."""
        ...


@dataclass
class Channels:
    channels: list[Channel]
    alert_timeout: float = ALERT_TIMEOUT

    @property
    def names(self) -> frozenset[str]:
        return frozenset(channel.name for channel in self.channels)

    def announce(self, record: ProposalRecord) -> None:
        """Tell every channel a proposal needs the owner, without waiting.
        Usable as the park step's `Announce`."""
        for channel in self.channels:
            _start(channel.name, "announce", partial(channel.announce_proposal, record))

    def alert(self, code: AlertCode, *, skip: frozenset[str] = frozenset()) -> set[str]:
        """Send an alert through every channel not in `skip`. Returns the names
        of the channels that delivered it within the timeout."""
        delivered: dict[str, Any] = {}
        threads = [
            _start(channel.name, f"alert {code}", partial(channel.alert, code), delivered)
            for channel in self.channels
            if channel.name not in skip
        ]
        deadline = time.monotonic() + self.alert_timeout
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                log.error("alert %s via %s did not answer in time", code, thread.name)
        return {name for name, result in dict(delivered).items() if result is True}


def _start(
    name: str, what: str, call: Callable[[], Any], results: dict[str, Any] | None = None
) -> threading.Thread:
    """Run one channel call in a daemon thread of its own.

    Daemon, so a push service that never answers cannot hold up the process
    when it shuts down.
    """

    def run() -> None:
        try:
            value = call()
        except Exception:
            log.exception("%s via %s failed", what, name)
            return
        if results is not None:
            results[name] = value

    thread = threading.Thread(target=run, name=name, daemon=True)
    thread.start()
    return thread


TELEGRAM_ALERTS: dict[AlertCode, str] = {
    "token_expiring": (
        "⚠️ The Google token expires within two days. "
        "Run <code>.\\tasks.ps1 reauth</code>, or mail stops being processed."
    ),
    "token_expired": (
        "⚠️ The Google token has expired, and mail is not being processed. "
        "Run <code>.\\tasks.ps1 reauth</code>."
    ),
    "standby_in_use": (
        "⚠️ The primary Google token failed, and the standby is in use. "
        "Run <code>.\\tasks.ps1 reauth</code> to replace the primary."
    ),
    "mail_sync_missed": (
        "⚠️ Mail sync missed messages. They are stored and fed now; "
        "<code>python -m app.mail.sync --status</code> on the instance shows the last recalls."
    ),
    "mail_feed_stalled": (
        "⚠️ The mail feed has stalled: mail waited over an hour without being processed. "
        "<code>python -m app.mail.sync --status</code> on the instance shows the last recalls."
    ),
    # No amounts (M17, D5): `/health`, with the bearer, shows the month's spend.
    "budget_warning": "⚠️ Model spending is at 80% of this month's cap.",
    "budget_exhausted": (
        "⚠️ Model spending cap reached: mail processing has stopped. It starts again next "
        "month, or once <code>MONTHLY_BUDGET_USD</code> is raised and the app restarted."
    ),
    "write_unconfirmed": (
        "⚠️ A calendar write could not be confirmed. Check the calendar: "
        "the agent asks Google again every hour, and settles it once Google answers."
    ),
}


class AllowedContacts(Protocol):
    def allowed(self, guests: Sequence[str]) -> frozenset[str]:
        """The guest keys, among `guests`, of contacts the owner allowed."""
        ...


@dataclass
class DatabaseContacts:
    database_url: str = field(repr=False)

    def allowed(self, guests: Sequence[str]) -> frozenset[str]:
        with connect_autocommit(self.database_url) as conn:
            return contacts.confirmed(conn, guests)


@dataclass
class TelegramChannel:
    """Optional: Telegram is blocked on the owner's network (M06 notes)."""

    bot: Sender
    chat_id: int
    zone: str
    contacts: AllowedContacts | None = None
    """Read as each card is sent, so a guest the owner has allowed is shown
    as one, and is not asked for again (M18, D5)."""
    name: str = field(default="telegram", init=False)

    def announce_proposal(self, record: ProposalRecord) -> None:
        send_approval_card(
            self.bot, self.chat_id, record, zone=self.zone, allowed=self._allowed(record)
        )

    def _allowed(self, record: ProposalRecord) -> frozenset[str]:
        """The card is sent even when the contacts cannot be read: it then asks
        for every guest outside the thread to be allowed, as before M18, and
        `decide()` reads the contacts again at a Confirm."""
        guests = [str(guest) for guest in record.payload.get("attendees") or []]
        if self.contacts is None or not guests:
            return frozenset()
        try:
            return self.contacts.allowed(guests)
        except Exception as exc:
            log.warning("could not read the allowed contacts (%s)", type(exc).__name__)
            return frozenset()

    def alert(self, code: AlertCode) -> bool:
        self.bot.send_message(self.chat_id, TELEGRAM_ALERTS[code])
        return True


def configured_channels(settings: Settings) -> Channels:
    """The channels the settings make possible. None configured is allowed:
    the timeline still shows every proposal."""
    channels: list[Channel] = []

    if settings.vapid_private_key is not None and settings.web_app_url is not None:
        channels.append(
            WebPushChannel(
                subscriptions=DatabaseSubscriptions(settings.database_url),
                private_key=settings.vapid_private_key.get_secret_value(),
                subject=settings.web_app_url,
            )
        )

    chat_id = admin_chat_id(settings.allowed_chat_ids)
    if settings.telegram_bot_token is not None and chat_id is not None:
        channels.append(
            TelegramChannel(
                bot=TelegramClient(settings.telegram_bot_token.get_secret_value()),
                chat_id=chat_id,
                zone=settings.user_timezone,
                contacts=DatabaseContacts(settings.database_url),
            )
        )
    return Channels(channels)
