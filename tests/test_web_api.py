"""The API the web app calls (M16, D4).

Reachable from the open internet, and it records decisions that act on the
owner's calendar. Like the webhook, its authentication gets more tests than
its happy path.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.channel.decide import DecisionResult
from app.config import Settings
from app.web_api import router

SECRET = "web-api-secret-for-tests"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "database_url": "postgresql://localhost/test",
        "gemini_api_key": "test-key",
        "web_api_secret": SecretStr(SECRET),
    }
    return Settings(**(base | overrides))


def _client(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> TestClient:
    monkeypatch.setattr("app.web_api.get_settings", lambda: _settings(**overrides))
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    return _client(monkeypatch)


def _bearer(secret: str = SECRET) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


DECISION = {"message_id": "m1", "revision": 2, "action": "confirm"}


@contextmanager
def _no_connection(url: str) -> Iterator[object]:
    yield object()


@dataclass
class Decisions:
    answer: DecisionResult = field(default_factory=lambda: DecisionResult("queued", 7))
    made: list[dict[str, Any]] = field(default_factory=list)
    woken: int = 0

    def __call__(self, conn: Any, message_id: str, **kwargs: Any) -> DecisionResult:
        self.made.append({"message_id": message_id, **kwargs})
        return self.answer


@pytest.fixture
def decisions(monkeypatch: pytest.MonkeyPatch) -> Decisions:
    fake = Decisions()
    monkeypatch.setattr("app.web_api.decide", fake)
    monkeypatch.setattr("app.web_api.connect_autocommit", _no_connection)

    def _woken() -> None:
        fake.woken += 1

    monkeypatch.setattr("app.web_api.decision_recorded", _woken)
    return fake


# --- authentication ----------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer not-the-secret"},
        {"Authorization": "Bearer "},
        {"Authorization": f"Basic {SECRET}"},
        {"Authorization": SECRET},
    ],
)
def test_a_missing_or_wrong_secret_is_a_401(
    client: TestClient, decisions: Decisions, headers: dict[str, str]
) -> None:
    assert client.post("/api/decisions", json=DECISION, headers=headers).status_code == 401
    assert decisions.made == []


@pytest.mark.parametrize("secret", [None, "", "   "])
def test_an_unset_or_blank_secret_is_a_503_not_an_open_door(
    monkeypatch: pytest.MonkeyPatch, secret: str | None
) -> None:
    """compare_digest("", "") is true: a blank secret must never count as configured."""
    client = _client(monkeypatch, web_api_secret=secret)

    assert client.post("/api/decisions", json=DECISION, headers=_bearer("")).status_code == 503
    assert client.post("/api/decisions", json=DECISION, headers=_bearer(SECRET)).status_code == 503


def test_the_secret_is_checked_before_the_body_is_read(client: TestClient) -> None:
    """An unauthenticated caller gets nothing parsed on its behalf."""
    response = client.post(
        "/api/decisions", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 401


# --- decisions -------------------------------------------------------------------------


def test_a_decision_is_queued_with_a_202_and_wakes_the_worker(
    client: TestClient, decisions: Decisions
) -> None:
    body = DECISION | {"action": "edit", "correction": "make it 5pm"}

    response = client.post("/api/decisions", json=body, headers=_bearer())

    assert response.status_code == 202
    assert response.json() == {"status": "queued", "decision_id": 7}
    assert decisions.made == [
        {
            "message_id": "m1",
            "action": "edit",
            "revision": 2,
            "correction": "make it 5pm",
            "via": "web",
        }
    ]
    assert decisions.woken == 1


def test_a_stale_card_is_a_409_that_names_the_current_revision(
    client: TestClient, decisions: Decisions
) -> None:
    decisions.answer = DecisionResult(
        "stale", detail="the proposal is deciding", current_revision=3
    )

    response = client.post("/api/decisions", json=DECISION, headers=_bearer())

    assert response.status_code == 409
    assert response.json()["current_revision"] == 3
    assert decisions.woken == 0


def test_a_message_with_no_proposal_is_a_404(client: TestClient, decisions: Decisions) -> None:
    decisions.answer = DecisionResult("not_found", detail="no proposal for this message")
    assert client.post("/api/decisions", json=DECISION, headers=_bearer()).status_code == 404


def test_a_decision_the_queue_refuses_is_a_422(client: TestClient, decisions: Decisions) -> None:
    decisions.answer = DecisionResult("invalid", detail="no edits left at this revision")

    response = client.post("/api/decisions", json=DECISION, headers=_bearer())

    assert response.status_code == 422
    assert response.json()["detail"] == "no edits left at this revision"


@pytest.mark.parametrize(
    "body",
    [
        {"message_id": "m1", "action": "confirm"},  # no revision
        DECISION | {"revision": 0},
        DECISION | {"action": "sweep"},  # an operator's, never a tap
        DECISION | {"action": "approve"},
        DECISION | {"surprise": True},
        DECISION | {"correction": "x" * 2001},
    ],
)
def test_a_malformed_request_is_a_422_before_the_queue_sees_it(
    client: TestClient, decisions: Decisions, body: dict[str, Any]
) -> None:
    assert client.post("/api/decisions", json=body, headers=_bearer()).status_code == 422
    assert decisions.made == []


def test_a_422_never_echoes_the_owners_text(client: TestClient, decisions: Decisions) -> None:
    """Validation errors go to logs and proxies; a correction can hold anything."""
    body = DECISION | {"correction": "private-note-" + "x" * 2001}

    response = client.post("/api/decisions", json=body, headers=_bearer())

    assert "private-note" not in response.text


def test_the_database_work_runs_off_the_event_loop(
    client: TestClient, decisions: Decisions, monkeypatch: pytest.MonkeyPatch
) -> None:
    offloaded: list[str] = []

    async def _in_threadpool(fn: Any, *args: Any) -> Any:
        offloaded.append(fn.__name__)
        return fn(*args)

    monkeypatch.setattr("app.web_api.run_in_threadpool", _in_threadpool)

    client.post("/api/decisions", json=DECISION, headers=_bearer())

    assert offloaded == ["_record_decision"]


# --- push subscriptions ---------------------------------------------------------------


SUBSCRIPTION = {
    "endpoint": "https://fcm.googleapis.com/fcm/send/abc123",
    "expirationTime": None,
    "keys": {"p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA", "auth": "tBHItJI5svbpez7KI4CCXg"},
}


@pytest.mark.parametrize(
    "body",
    [
        SUBSCRIPTION | {"endpoint": "http://fcm.googleapis.com/fcm/send/abc"},  # not https
        SUBSCRIPTION | {"endpoint": "https://" + "x" * 3000},
        {"endpoint": SUBSCRIPTION["endpoint"]},  # no keys
        SUBSCRIPTION | {"keys": {"p256dh": "", "auth": "x"}},
        # Fly would POST to whatever is stored: only real push services.
        SUBSCRIPTION | {"endpoint": "https://["},
        SUBSCRIPTION | {"endpoint": "https://evil.example.com/fcm/send/abc"},
        SUBSCRIPTION | {"endpoint": "https://fcm.googleapis.com.evil.example.com/x"},
        SUBSCRIPTION | {"endpoint": "https://owner:pw@fcm.googleapis.com/fcm/send/abc"},
        SUBSCRIPTION | {"endpoint": "https://fcm.googleapis.com:8443/fcm/send/abc"},
    ],
)
def test_a_malformed_subscription_is_a_422(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    monkeypatch.setattr("app.web_api.connect_autocommit", _no_connection)
    response = client.post("/api/push-subscriptions", json=body, headers=_bearer())
    assert response.status_code == 422


def test_subscriptions_need_the_secret_too(client: TestClient) -> None:
    assert client.post("/api/push-subscriptions", json=SUBSCRIPTION).status_code == 401
    assert client.request("DELETE", "/api/push-subscriptions", json=SUBSCRIPTION).status_code == 401


def _rows(conn: psycopg.Connection) -> list[tuple[Any, ...]]:
    return conn.execute("SELECT endpoint, p256dh, auth FROM push_subscriptions").fetchall()


@pytest.mark.integration
def test_a_subscription_is_stored_once_and_refreshed(
    client: TestClient, conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextmanager
    def _same(url: str) -> Iterator[psycopg.Connection]:
        yield conn

    monkeypatch.setattr("app.web_api.connect_autocommit", _same)
    conn.execute("DELETE FROM push_subscriptions")
    refreshed = SUBSCRIPTION | {"keys": {"p256dh": "new-p256dh", "auth": "new-auth"}}

    assert (
        client.post("/api/push-subscriptions", json=SUBSCRIPTION, headers=_bearer()).status_code
        == 204
    )
    assert (
        client.post("/api/push-subscriptions", json=refreshed, headers=_bearer()).status_code == 204
    )

    assert _rows(conn) == [(SUBSCRIPTION["endpoint"], "new-p256dh", "new-auth")]

    gone = client.request(
        "DELETE",
        "/api/push-subscriptions",
        json={"endpoint": SUBSCRIPTION["endpoint"]},
        headers=_bearer(),
    )
    assert gone.status_code == 204
    assert _rows(conn) == []


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://fcm.googleapis.com/fcm/send/abc",  # Chrome on Android
        "https://web.push.apple.com/QGuQyavXutnMei",  # iPhone
        "https://updates.push.services.mozilla.com/wpush/v2/abc",  # Firefox
        "https://wns2-db5p.notify.windows.com/w/?token=abc",  # Edge
    ],
)
def test_the_push_services_phones_use_are_accepted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    monkeypatch.setattr("app.web_api._store_subscription", lambda settings, subscription: True)

    response = client.post(
        "/api/push-subscriptions", json=SUBSCRIPTION | {"endpoint": endpoint}, headers=_bearer()
    )

    assert response.status_code == 204


@pytest.mark.integration
def test_subscriptions_are_capped_but_a_known_one_is_always_refreshed(
    client: TestClient, conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bounded fan-out: each push round sends to every row."""
    from app.web_api import MAX_SUBSCRIPTIONS

    @contextmanager
    def _same(url: str) -> Iterator[psycopg.Connection]:
        yield conn

    monkeypatch.setattr("app.web_api.connect_autocommit", _same)
    conn.execute("DELETE FROM push_subscriptions")
    for n in range(MAX_SUBSCRIPTIONS):
        conn.execute(
            "INSERT INTO push_subscriptions (endpoint, p256dh, auth) VALUES (%s, 'p', 'a')",
            (f"https://fcm.googleapis.com/fcm/send/{n}",),
        )

    one_more = SUBSCRIPTION | {"endpoint": "https://fcm.googleapis.com/fcm/send/new"}
    known = SUBSCRIPTION | {"endpoint": "https://fcm.googleapis.com/fcm/send/0"}

    assert (
        client.post("/api/push-subscriptions", json=one_more, headers=_bearer()).status_code == 409
    )
    assert client.post("/api/push-subscriptions", json=known, headers=_bearer()).status_code == 204
