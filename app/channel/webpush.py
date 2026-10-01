"""Web push (M16, D6): the channel that reaches both of the owner's phones.

What a push carries is generic -- "A proposal needs you" -- because it
crosses Apple's and Google's servers and lands on a lock screen. The owner
opens the app to see anything more.

Every send is bounded and kept: `pywebpush` defaults to no timeout, so one
hung push service would block the poll that sent it, and to a time-to-live of
0, so a push to a sleeping phone is dropped. A subscription the push service
has forgotten (404 or 410) is deleted; the app re-subscribes the next time it
opens.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlparse

from pywebpush import WebPushException, webpush

from app.channel.park import ProposalRecord
from app.store.db import connect_autocommit

if TYPE_CHECKING:
    from app.channel.channels import AlertCode

log = logging.getLogger(__name__)

PROPOSAL_PUSH = {
    "title": "mailagent",
    "body": "A proposal needs you.",
    "url": "/",
    "tag": "proposal",
}
ALERT_PUSH = {
    "title": "mailagent",
    "body": "Google sign-in needs attention.",
    "url": "/",
    "tag": "alert",
}
"""Generic words only: a push crosses Apple's and Google's servers and lands on
a lock screen. The tag is the kind, so the phone replaces a notification only
with one of the same kind (`dashboard/public/sw.js`)."""

ALERT_PUSHES: dict[AlertCode, dict[str, str]] = {
    "token_expiring": ALERT_PUSH,
    "token_expired": ALERT_PUSH,
    "standby_in_use": ALERT_PUSH,
    "mail_sync_missed": ALERT_PUSH | {"body": "Mail sync missed messages.", "tag": "mail-sync"},
    "mail_feed_stalled": ALERT_PUSH | {"body": "The mail feed has stalled.", "tag": "mail-feed"},
}
"""What each alert pushes. Every code has an entry. The mail alerts have tags
of their own: sent once a day each, neither may replace the other, nor an
unread sign-in alert, which is sent once. The worker knows each tag."""

PUSH_TIMEOUT = 10
"""Seconds per push service."""

PUSH_TTL = 24 * 60 * 60
"""How long a push service keeps a push for a phone that is asleep or off."""

PUSH_BUDGET = 30.0
"""Seconds one round of sends may take. Sends are one after another, each up
to `PUSH_TIMEOUT`, so a round stops starting new ones after this -- and so
stays inside the time an alert waits for its channels."""

GONE = frozenset({404, 410})


@dataclass(frozen=True, slots=True)
class StoredSubscription:
    endpoint: str
    p256dh: str
    auth: str


class Subscriptions(Protocol):
    def all(self) -> list[StoredSubscription]: ...

    def remove(self, endpoint: str) -> None: ...


@dataclass
class DatabaseSubscriptions:
    database_url: str = field(repr=False)

    def all(self) -> list[StoredSubscription]:
        with connect_autocommit(self.database_url) as conn:
            rows = conn.execute(
                "SELECT endpoint, p256dh, auth FROM push_subscriptions ORDER BY created_at"
            ).fetchall()
        return [StoredSubscription(*row) for row in rows]

    def remove(self, endpoint: str) -> None:
        with connect_autocommit(self.database_url) as conn:
            conn.execute("DELETE FROM push_subscriptions WHERE endpoint = %s", (endpoint,))


@dataclass
class WebPushChannel:
    subscriptions: Subscriptions
    private_key: str = field(repr=False)
    subject: str = field(kw_only=True)
    """The app's URL: the VAPID `sub`, sent to Apple and Google. Not the
    owner's email, which they have no need to see."""

    send: Callable[..., Any] = field(default=webpush, kw_only=True)
    budget: float = field(default=PUSH_BUDGET, kw_only=True)
    clock: Callable[[], float] = field(default=time.monotonic, kw_only=True, repr=False)
    name: str = field(default="web_push", init=False)

    def announce_proposal(self, record: ProposalRecord) -> None:
        # The record is deliberately unused: nothing about the proposal leaves.
        self._push(PROPOSAL_PUSH)

    def alert(self, code: AlertCode) -> bool:
        return self._push(ALERT_PUSHES[code]) > 0

    def _push(self, payload: dict[str, str]) -> int:
        """Send to every subscription. Returns how many push services accepted."""
        delivered = 0
        data = json.dumps(payload)
        started = self.clock()
        for subscription in self.subscriptions.all():
            if self.clock() - started > self.budget:
                log.warning("push round stopped at its %.0fs budget", self.budget)
                break
            service = _service(subscription.endpoint)
            try:
                self.send(
                    subscription_info={
                        "endpoint": subscription.endpoint,
                        "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
                    },
                    data=data,
                    vapid_private_key=self.private_key,
                    # A fresh dict for every send: webpush writes the push
                    # service's audience into it, and a shared one would carry
                    # the first service's audience to the next.
                    vapid_claims={"sub": self.subject},
                    ttl=PUSH_TTL,
                    headers={"Urgency": "high"},
                    timeout=PUSH_TIMEOUT,
                )
            except WebPushException as exc:
                # The endpoint is a capability -- whoever holds it can push to
                # the phone -- so logs name the service, never the endpoint.
                log.warning("push via %s failed with status %s", service, exc.status_code)
                if exc.status_code in GONE:
                    self.subscriptions.remove(subscription.endpoint)
            except Exception as exc:
                log.warning("push via %s failed: %s", service, type(exc).__name__)
            else:
                delivered += 1
        return delivered


def _service(endpoint: str) -> str:
    """The push service's host, for logs. Never raises: a malformed endpoint
    must not stop the pushes to the subscriptions after it."""
    try:
        return urlparse(endpoint).hostname or "unknown"
    except ValueError:
        return "unparseable"
