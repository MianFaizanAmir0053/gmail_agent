"""Channels (M16, D7): how the owner hears that something needs them.

Every configured channel is told, and none can stop the others. Each call
runs on a small shared thread pool with a deadline, so a channel that raises
is logged and one that hangs is abandoned, while the rest are still told and
the caller -- a poll, the worker, reconciliation -- carries on.

A channel gets the stored proposal record: the card's fields, the revision
and the mode, never the raw interrupt payload, which carries the model's
reasoning and can quote the email.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.channel.park import ProposalRecord
from app.channel.webpush import DatabaseSubscriptions, WebPushChannel
from app.config import Settings
from app.telegram.client import Sender, TelegramClient
from app.telegram.notify import admin_chat_id, send_approval_card

log = logging.getLogger(__name__)

AlertCode = Literal["token_expiring", "token_expired", "standby_in_use"]

CHANNEL_TIMEOUT = 15.0
"""Seconds to wait for all channels. Each channel's own network calls have
shorter timeouts; this is the backstop for one that ignores them."""

_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="channel")


class Channel(Protocol):
    name: str

    def announce_proposal(self, record: ProposalRecord) -> None: ...

    def alert(self, code: AlertCode) -> bool:
        """True once the alert was delivered, so it is not sent again."""
        ...


@dataclass
class Channels:
    channels: list[Channel]
    timeout: float = CHANNEL_TIMEOUT

    def announce(self, record: ProposalRecord) -> None:
        """Tell every channel a proposal needs the owner. Usable as the park
        step's `Announce`."""
        self._each(lambda channel: channel.announce_proposal(record), "announce")

    def alert(self, code: AlertCode) -> bool:
        """True if at least one channel delivered the alert."""
        results = self._each(lambda channel: channel.alert(code), f"alert {code}")
        return any(result is True for result in results)

    def _each(self, call: Callable[[Channel], Any], what: str) -> list[Any]:
        futures = {_POOL.submit(call, channel): channel for channel in self.channels}
        done, not_done = wait(futures, timeout=self.timeout)
        results: list[Any] = []
        for future in done:
            try:
                results.append(future.result())
            except Exception:
                log.exception("%s via %s failed", what, futures[future].name)
        for future in not_done:
            log.error("%s via %s did not answer in %.0fs", what, futures[future].name, self.timeout)
        return results


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
}


@dataclass
class TelegramChannel:
    """Optional: Telegram is blocked on the owner's network (M06 notes)."""

    bot: Sender
    chat_id: int
    zone: str
    name: str = field(default="telegram", init=False)

    def announce_proposal(self, record: ProposalRecord) -> None:
        send_approval_card(self.bot, self.chat_id, record, zone=self.zone)

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
            )
        )
    return Channels(channels)
