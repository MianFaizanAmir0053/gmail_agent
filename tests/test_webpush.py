"""Web push (M16, D6): generic payloads, bounded sends, dead subscriptions removed."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest
from py_vapid import Vapid
from pydantic import SecretStr
from pywebpush import WebPushException

from app.channel import webpush as channel
from app.channel.channels import configured_channels
from app.channel.park import proposal_from
from app.channel.webpush import (
    ALERT_PUSH,
    PROPOSAL_PUSH,
    DatabaseSubscriptions,
    StoredSubscription,
    WebPushChannel,
)
from app.config import Settings
from app.jobs.vapid import generate_keys

APP_URL = "https://mailagent-owner.vercel.app"

SECRET_CONTENT: dict[str, Any] = {
    "proposed": {
        "title": "Salary review with HR",
        "attendees": ["hr@example.com"],
        "location": "Room 4B",
    },
    "conflicts": ["Overlaps Therapy"],
    "dry_run": True,
    "action_type": "calendar_invite",
    "pipeline_version": "0123456789ab",
}

FCM = StoredSubscription(
    endpoint="https://fcm.googleapis.com/fcm/send/owner-android-token",
    p256dh="p256dh-android",
    auth="auth-android",
)
APPLE = StoredSubscription(
    endpoint="https://web.push.apple.com/owner-iphone-token",
    p256dh="p256dh-iphone",
    auth="auth-iphone",
)


@dataclass
class FakeSubscriptions:
    stored: list[StoredSubscription] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def all(self) -> list[StoredSubscription]:
        return [s for s in self.stored if s.endpoint not in self.removed]

    def remove(self, endpoint: str) -> None:
        self.removed.append(endpoint)


@dataclass
class Response:
    status_code: int
    text: str = ""


@dataclass
class FakeSend:
    """Stands in for `pywebpush.webpush`, including its habit of writing the
    push service's audience into the claims dict it is given."""

    failures: dict[str, int] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, **kwargs: Any) -> Response:
        claims = kwargs["vapid_claims"]
        self.calls.append(kwargs | {"vapid_claims": dict(claims)})
        claims["aud"] = kwargs["subscription_info"]["endpoint"].split("/fcm")[0]
        status = self.failures.get(kwargs["subscription_info"]["endpoint"])
        if status is not None:
            raise WebPushException("Push failed", response=Response(status))
        return Response(201)


def _channel(
    *subs: StoredSubscription, send: FakeSend | None = None
) -> tuple[WebPushChannel, FakeSubscriptions, FakeSend]:
    subscriptions = FakeSubscriptions(list(subs))
    sender = send or FakeSend()
    return (
        WebPushChannel(subscriptions=subscriptions, private_key="k", subject=APP_URL, send=sender),
        subscriptions,
        sender,
    )


def test_a_proposal_push_carries_no_proposal_content() -> None:
    """Payloads cross Apple's and Google's servers and land on a lock screen."""
    push, _, send = _channel(FCM)

    push.announce_proposal(proposal_from("m1", SECRET_CONTENT, 1))

    data = send.calls[0]["data"]
    assert json.loads(data) == PROPOSAL_PUSH
    for secret in ("Salary", "hr@example.com", "Room 4B", "Therapy", "m1"):
        assert secret not in data


def test_an_alert_push_is_generic_too() -> None:
    push, _, send = _channel(FCM)

    push.alert("token_expired")

    assert json.loads(send.calls[0]["data"]) == ALERT_PUSH


def test_every_push_is_bounded_kept_for_a_day_and_urgent() -> None:
    """pywebpush's defaults are no timeout and a TTL of 0: one hung service would
    block the poll, and a push to a sleeping phone would be dropped."""
    push, _, send = _channel(FCM)

    push.announce_proposal(proposal_from("m1", SECRET_CONTENT, 1))

    call = send.calls[0]
    assert call["timeout"] == 10
    assert call["ttl"] == 24 * 60 * 60
    assert call["headers"] == {"Urgency": "high"}
    assert call["vapid_claims"] == {"sub": APP_URL}


def test_each_push_gets_claims_of_its_own() -> None:
    """webpush writes the audience into the claims; shared claims would send
    Google's audience to Apple, which refuses it."""
    push, _, send = _channel(FCM, APPLE)

    push.announce_proposal(proposal_from("m1", SECRET_CONTENT, 1))

    assert [call["vapid_claims"] for call in send.calls] == [{"sub": APP_URL}, {"sub": APP_URL}]


@pytest.mark.parametrize("status", [404, 410])
def test_a_subscription_the_push_service_has_forgotten_is_deleted(status: int) -> None:
    push, subscriptions, _ = _channel(FCM, APPLE, send=FakeSend(failures={APPLE.endpoint: status}))

    push.announce_proposal(proposal_from("m1", SECRET_CONTENT, 1))

    assert subscriptions.removed == [APPLE.endpoint]


def test_a_passing_failure_keeps_the_subscription() -> None:
    push, subscriptions, _ = _channel(FCM, send=FakeSend(failures={FCM.endpoint: 503}))

    push.announce_proposal(proposal_from("m1", SECRET_CONTENT, 1))

    assert subscriptions.removed == []


def test_an_alert_is_delivered_only_once_a_push_service_accepted_it() -> None:
    assert _channel(FCM)[0].alert("token_expired") is True
    assert _channel()[0].alert("token_expired") is False  # nobody subscribed yet
    failing = FakeSend(failures={FCM.endpoint: 503})
    assert _channel(FCM, send=failing)[0].alert("token_expired") is False


def test_logs_name_the_push_service_never_the_endpoint(caplog: pytest.LogCaptureFixture) -> None:
    """An endpoint is a capability: whoever holds it can push to the phone."""
    push, _, _ = _channel(FCM, send=FakeSend(failures={FCM.endpoint: 503}))

    with caplog.at_level(logging.WARNING, logger="app.channel.webpush"):
        push.announce_proposal(proposal_from("m1", SECRET_CONTENT, 1))

    assert "fcm.googleapis.com" in caplog.text
    assert "owner-android-token" not in caplog.text


# --- configuration -----------------------------------------------------------------


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "database_url": "postgresql://localhost/test",
        "gemini_api_key": "test-key",
    }
    return Settings(**(base | overrides))


def test_web_push_needs_a_private_key_and_the_apps_url() -> None:
    key = SecretStr(generate_keys().private_key)

    assert configured_channels(_settings(vapid_private_key=key)).channels == []
    assert configured_channels(_settings(web_app_url=APP_URL)).channels == []
    both = configured_channels(_settings(vapid_private_key=key, web_app_url=APP_URL))
    assert [c.name for c in both.channels] == ["web_push"]


# --- keys (.\tasks.ps1 vapid) ------------------------------------------------------


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def test_generated_keys_are_a_pair_pywebpush_and_browsers_accept() -> None:
    keys = generate_keys()

    signer = Vapid.from_string(keys.private_key)  # how pywebpush loads it
    public = _b64url_decode(keys.public_key)  # the browser's applicationServerKey

    assert len(public) == 65 and public[0] == 0x04  # an uncompressed P-256 point
    derived = signer.public_key.public_numbers()
    assert public[1:33] == derived.x.to_bytes(32, "big")
    assert public[33:] == derived.y.to_bytes(32, "big")


def test_each_generation_is_a_new_pair() -> None:
    assert generate_keys().private_key != generate_keys().private_key


# --- the stored subscriptions (Postgres) ---------------------------------------------


@pytest.mark.integration
def test_stored_subscriptions_are_read_and_removed(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextmanager
    def _same(url: str) -> Iterator[psycopg.Connection]:
        yield conn

    monkeypatch.setattr(channel, "connect_autocommit", _same)
    conn.execute("DELETE FROM push_subscriptions")
    conn.execute(
        "INSERT INTO push_subscriptions (endpoint, p256dh, auth) VALUES (%s, %s, %s)",
        (FCM.endpoint, FCM.p256dh, FCM.auth),
    )
    store = DatabaseSubscriptions("postgresql://unused")

    assert store.all() == [FCM]

    store.remove(FCM.endpoint)

    assert store.all() == []


# --- review of the backend (2026-10-01) ------------------------------------------------


def test_an_endpoint_that_cannot_be_parsed_does_not_stop_the_others() -> None:
    broken = StoredSubscription(endpoint="https://[", p256dh="x", auth="y")
    push, _, send = _channel(broken, FCM)

    push.announce_proposal(proposal_from("m1", SECRET_CONTENT, 1))

    assert [call["subscription_info"]["endpoint"] for call in send.calls][-1] == FCM.endpoint


def test_a_round_of_sends_stops_at_its_budget() -> None:
    """Sends are sequential and each may take its full timeout; the round must
    end inside the time an alert waits for its channels."""
    now = [0.0]

    def clock() -> float:
        return now[0]

    send = FakeSend()

    def slow_send(**kwargs: Any) -> Response:
        now[0] += 20.0
        return send(**kwargs)

    third = StoredSubscription(
        endpoint="https://fcm.googleapis.com/fcm/send/third", p256dh="p", auth="a"
    )
    push = WebPushChannel(
        subscriptions=FakeSubscriptions([FCM, APPLE, third]),
        private_key="k",
        subject=APP_URL,
        send=slow_send,
        budget=30.0,
        clock=clock,
    )

    push.announce_proposal(proposal_from("m1", SECRET_CONTENT, 1))

    assert len(send.calls) == 2


def test_secrets_stay_out_of_reprs() -> None:
    """A repr lands in logs, assertion messages and error reports."""
    push = WebPushChannel(
        subscriptions=DatabaseSubscriptions("postgresql://owner:db-password@host/db"),
        private_key="vapid-private-key",
        subject=APP_URL,
    )

    text = repr(push)

    assert "vapid-private-key" not in text
    assert "db-password" not in text
