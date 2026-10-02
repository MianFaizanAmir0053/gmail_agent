"""The API the web app calls (M16, D4).

Reachable from the open internet, and it records decisions that act on the
owner's calendar. Like the webhook, its authentication gets more tests than
its happy path.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.channel.decide import DecisionResult
from app.channel.pairing import IssuedCode
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
            "token": None,
            "dry_run": True,
        }
    ]
    assert decisions.woken == 1


def test_a_confirm_carries_the_cards_token_and_this_process_mode(
    client: TestClient, decisions: Decisions
) -> None:
    """What the owner saw travels with the decision (M17, D2); `decide()` holds
    the claim to it, and to the setting Fly runs under."""
    body = DECISION | {"token": "3f9a1c07be42-dry-1"}

    assert client.post("/api/decisions", json=body, headers=_bearer()).status_code == 202
    assert decisions.made[0]["token"] == "3f9a1c07be42-dry-1"
    assert decisions.made[0]["dry_run"] is True


def test_a_proposal_still_being_prepared_is_a_409_that_says_so(
    client: TestClient, decisions: Decisions
) -> None:
    decisions.answer = DecisionResult("not_ready", detail="the proposal is being prepared")

    response = client.post("/api/decisions", json=DECISION, headers=_bearer())

    assert response.status_code == 409
    assert response.json()["status"] == "not_ready"
    assert decisions.woken == 0


def test_an_overlong_token_is_a_422(client: TestClient, decisions: Decisions) -> None:
    body = DECISION | {"token": "x" * 65}
    assert client.post("/api/decisions", json=body, headers=_bearer()).status_code == 422
    assert decisions.made == []


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


# --- pairing, the iPhone fallback --------------------------------------------------


ISSUED = IssuedCode(code="012345", expires_at=datetime(2026, 10, 1, 12, 5, tzinfo=UTC))

PAIRING = [
    ("/api/pairing/codes", {"issued_to": "web"}),
    ("/api/pairing/redeem", {"code": "123456"}),
]


@dataclass
class Pairing:
    issued_to: list[str] = field(default_factory=list)
    tried: list[str] = field(default_factory=list)
    accepts: bool = True


@pytest.fixture
def pairing(monkeypatch: pytest.MonkeyPatch) -> Pairing:
    """Stands in for the database work, which `tests/test_pairing.py` covers."""
    fake = Pairing()

    def _issue_pairing_code(settings: Settings, issued_to: str) -> IssuedCode:
        fake.issued_to.append(issued_to)
        return ISSUED

    def _redeem_pairing_code(settings: Settings, code: str) -> bool:
        fake.tried.append(code)
        return fake.accepts

    monkeypatch.setattr("app.web_api._issue_pairing_code", _issue_pairing_code)
    monkeypatch.setattr("app.web_api._redeem_pairing_code", _redeem_pairing_code)
    return fake


@pytest.fixture
def paired(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A client for an API with pairing switched on."""
    return _client(monkeypatch, pairing_enabled=True)


@pytest.mark.parametrize(("path", "body"), PAIRING)
def test_pairing_is_not_found_while_switched_off(
    client: TestClient, pairing: Pairing, path: str, body: dict[str, Any]
) -> None:
    """Off by default: the fallback is built, but answers as if it were not."""
    assert client.post(path, json=body, headers=_bearer()).status_code == 404
    assert pairing.issued_to == pairing.tried == []


@pytest.mark.parametrize(("path", "body"), PAIRING)
@pytest.mark.parametrize("enabled", [False, True])
def test_pairing_checks_the_secret_before_the_switch(
    monkeypatch: pytest.MonkeyPatch,
    pairing: Pairing,
    path: str,
    body: dict[str, Any],
    enabled: bool,
) -> None:
    """A caller without the secret cannot even tell whether pairing is on."""
    client = _client(monkeypatch, pairing_enabled=enabled)
    assert client.post(path, json=body).status_code == 401
    assert client.post(path, json=body, headers=_bearer("not-the-secret")).status_code == 401

    unset = _client(monkeypatch, pairing_enabled=enabled, web_api_secret="  ")
    assert unset.post(path, json=body, headers=_bearer()).status_code == 503

    assert pairing.issued_to == pairing.tried == []


@pytest.mark.parametrize("path", [path for path, _ in PAIRING])
def test_pairing_reads_no_body_before_the_secret_and_the_switch(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    junk = b"{not json"
    as_json = {"Content-Type": "application/json"}

    on = _client(monkeypatch, pairing_enabled=True)
    assert on.post(path, content=junk, headers=as_json).status_code == 401

    off = _client(monkeypatch)
    assert off.post(path, content=junk, headers=as_json | _bearer()).status_code == 404


def test_a_pairing_code_is_issued_with_a_201(paired: TestClient, pairing: Pairing) -> None:
    response = paired.post("/api/pairing/codes", json={"issued_to": "web"}, headers=_bearer())

    assert response.status_code == 201
    assert response.json() == {"code": "012345", "expires_at": "2026-10-01T12:05:00+00:00"}
    assert response.headers["cache-control"] == "no-store"
    assert pairing.issued_to == ["web"]


def test_a_redeemed_code_is_a_204(paired: TestClient, pairing: Pairing) -> None:
    response = paired.post("/api/pairing/redeem", json={"code": "123456"}, headers=_bearer())

    assert response.status_code == 204
    assert response.content == b""
    assert pairing.tried == ["123456"]


@pytest.mark.parametrize("code", ["123456", "12345", "not a code"])
def test_a_refused_code_is_a_403_that_never_says_why(
    paired: TestClient, pairing: Pairing, code: str
) -> None:
    """Wrong, expired, spent, absent or malformed: one answer for all."""
    pairing.accepts = False

    response = paired.post("/api/pairing/redeem", json={"code": code}, headers=_bearer())

    assert response.status_code == 403
    assert response.json() == {"status": "refused"}


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/pairing/codes", {}),
        ("/api/pairing/codes", {"issued_to": ""}),
        ("/api/pairing/codes", {"issued_to": "x" * 65}),
        ("/api/pairing/codes", {"issued_to": "web\x00"}),
        ("/api/pairing/codes", {"issued_to": "web\nforged log line"}),
        ("/api/pairing/codes", {"issued_to": "web", "surprise": True}),
        ("/api/pairing/redeem", {}),
        ("/api/pairing/redeem", {"code": 123456}),
        ("/api/pairing/redeem", {"code": "1" * 65}),
        ("/api/pairing/redeem", {"code": "123456", "surprise": True}),
        ("/api/pairing/redeem", ["123456"]),
    ],
)
def test_a_malformed_pairing_request_is_a_422(
    paired: TestClient, pairing: Pairing, path: str, body: Any
) -> None:
    assert paired.post(path, json=body, headers=_bearer()).status_code == 422
    assert pairing.issued_to == pairing.tried == []


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/pairing/codes", {"issued_to": "private-label-" + "x" * 64}),
        ("/api/pairing/redeem", {"code": "private-code-" + "1" * 64}),
    ],
)
def test_a_pairing_422_never_echoes_the_input(
    paired: TestClient, pairing: Pairing, path: str, body: dict[str, Any]
) -> None:
    response = paired.post(path, json=body, headers=_bearer())

    assert response.status_code == 422
    assert "private-" not in response.text


@pytest.mark.integration
def test_a_code_from_the_api_redeems_once_through_the_api(
    paired: TestClient, conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextmanager
    def _same(url: str) -> Iterator[psycopg.Connection]:
        yield conn

    monkeypatch.setattr("app.web_api.connect_autocommit", _same)
    conn.execute("DELETE FROM pairing_codes")  # rolled back with the test

    issued = paired.post("/api/pairing/codes", json={"issued_to": "web"}, headers=_bearer())
    assert issued.status_code == 201
    code = issued.json()["code"]
    expires_at = datetime.fromisoformat(issued.json()["expires_at"])
    assert expires_at.tzinfo is not None

    for expected in (204, 403):  # once, and never again
        redeemed = paired.post("/api/pairing/redeem", json={"code": code}, headers=_bearer())
        assert redeemed.status_code == expected


def test_the_pairing_database_work_runs_off_the_event_loop(
    paired: TestClient, pairing: Pairing, monkeypatch: pytest.MonkeyPatch
) -> None:
    offloaded: list[str] = []

    async def _in_threadpool(fn: Any, *args: Any) -> Any:
        offloaded.append(fn.__name__)
        return fn(*args)

    monkeypatch.setattr("app.web_api.run_in_threadpool", _in_threadpool)

    paired.post("/api/pairing/codes", json={"issued_to": "web"}, headers=_bearer())
    paired.post("/api/pairing/redeem", json={"code": "123456"}, headers=_bearer())

    assert offloaded == ["_issue_pairing_code", "_redeem_pairing_code"]


def test_a_confirm_with_guests_outside_the_thread_is_a_422_that_says_so(
    client: TestClient, decisions: Decisions
) -> None:
    decisions.answer = DecisionResult(
        "outside", detail="allow or remove the guests outside the thread first"
    )

    response = client.post("/api/decisions", json=DECISION, headers=_bearer())

    assert response.status_code == 422
    assert response.json() == {
        "status": "outside",
        "detail": "allow or remove the guests outside the thread first",
    }


# --- contacts (M17, D4) --------------------------------------------------------------


@dataclass
class Allowed:
    made: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, conn: Any, address: str, **kwargs: Any) -> None:
        if "@" not in address:
            raise ValueError("not an email address")
        self.made.append({"address": address, **kwargs})


@pytest.fixture
def allowed(monkeypatch: pytest.MonkeyPatch) -> Allowed:
    fake = Allowed()
    monkeypatch.setattr("app.web_api.contacts.allow", fake)
    monkeypatch.setattr("app.web_api.connect_autocommit", _no_connection)
    return fake


def _with_key(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    return _client(monkeypatch, fernet_key=SecretStr("a-fernet-key-for-tests"))


def test_an_allowed_guest_is_recorded_as_the_web_s(
    monkeypatch: pytest.MonkeyPatch, allowed: Allowed
) -> None:
    client = _with_key(monkeypatch)

    response = client.post(
        "/api/contacts",
        json={"address": "sara@example.com", "message_id": "m1"},
        headers=_bearer(),
    )

    assert response.status_code == 204
    [made] = allowed.made
    assert (made["address"], made["via"], made["message_id"]) == ("sara@example.com", "web", "m1")


def test_something_that_is_not_an_address_is_a_422(
    monkeypatch: pytest.MonkeyPatch, allowed: Allowed
) -> None:
    client = _with_key(monkeypatch)

    response = client.post("/api/contacts", json={"address": "no address"}, headers=_bearer())

    assert response.status_code == 422
    assert allowed.made == []


def test_allowing_needs_the_secret(monkeypatch: pytest.MonkeyPatch, allowed: Allowed) -> None:
    client = _with_key(monkeypatch)

    response = client.post("/api/contacts", json={"address": "sara@example.com"})

    assert response.status_code == 401
    assert allowed.made == []


def test_allowing_without_a_key_is_a_503(monkeypatch: pytest.MonkeyPatch, allowed: Allowed) -> None:
    """The key is what the audit log's record of a contact is hashed with."""
    response = _client(monkeypatch).post(
        "/api/contacts", json={"address": "sara@example.com"}, headers=_bearer()
    )

    assert response.status_code == 503


# --- pause, resume and withdraw (M17, D6) -------------------------------------------------------


@dataclass
class Switches:
    calls: list[tuple[str, str]] = field(default_factory=list)
    woken: int = 0

    def switch(self, conn: Any, *, paused: bool, via: str) -> bool:
        self.calls.append(("pause" if paused else "resume", via))
        return True


@pytest.fixture
def switches(monkeypatch: pytest.MonkeyPatch) -> Switches:
    fake = Switches()
    monkeypatch.setattr("app.web_api.control", fake)
    monkeypatch.setattr("app.web_api.connect_autocommit", _no_connection)

    def _woken() -> bool:
        fake.woken += 1
        return True

    monkeypatch.setattr("app.web_api.wake_decisions", _woken)
    return fake


def test_pause_and_resume_are_recorded_as_the_webs(client: TestClient, switches: Switches) -> None:
    assert client.post("/api/pause", headers=_bearer()).status_code == 204
    assert client.post("/api/resume", headers=_bearer()).status_code == 204

    assert switches.calls == [("pause", "web"), ("resume", "web")]
    assert switches.woken == 1  # a held decision moves on at once


@pytest.mark.parametrize("path", ["/api/pause", "/api/resume", "/api/decisions/withdraw"])
def test_the_switches_need_the_secret(client: TestClient, switches: Switches, path: str) -> None:
    assert client.post(path, json={"decision_id": 7}).status_code == 401
    assert switches.calls == []


@pytest.mark.parametrize(
    ("answer", "code"), [("requested", 202), ("settled", 409), ("not_found", 404)]
)
def test_a_withdraw_request_says_what_became_of_it(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    switches: Switches,
    answer: str,
    code: int,
) -> None:
    asked: list[int] = []

    def _request(conn: Any, decision_id: int) -> str:
        asked.append(decision_id)
        return answer

    monkeypatch.setattr("app.web_api.request_withdraw", _request)

    response = client.post("/api/decisions/withdraw", json={"decision_id": 7}, headers=_bearer())

    assert (response.status_code, response.json()) == (code, {"status": answer})
    assert asked == [7]
    assert switches.woken == (1 if answer == "requested" else 0)


def test_a_withdraw_names_a_decision_not_a_proposal(client: TestClient, switches: Switches) -> None:
    """A request from an old card cannot reach a decision made since."""
    response = client.post("/api/decisions/withdraw", json={"message_id": "m1"}, headers=_bearer())

    assert response.status_code == 422
