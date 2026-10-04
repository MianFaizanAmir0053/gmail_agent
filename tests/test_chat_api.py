"""`POST /api/chat` (M19, D1): a turn stored once, typed and pasted apart."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.planner import turns
from app.web_api import router

SECRET = "web-api-secret-for-tests"
KEY = "k" * 24
ZONE = "Asia/Karachi"


def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    settings = Settings(
        _env_file=None,
        database_url="postgresql://localhost/test",
        gemini_api_key="test-key",
        web_api_secret=SecretStr(SECRET),
        user_timezone=ZONE,
    )
    monkeypatch.setattr("app.web_api.get_settings", lambda: settings)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _post(client: TestClient, body: dict[str, Any], secret: str = SECRET) -> Any:
    return client.post("/api/chat", json=body, headers={"Authorization": f"Bearer {secret}"})


def _request(*parts: tuple[str, bool], key: str = KEY, zone: str = ZONE) -> dict[str, Any]:
    return {"key": key, "zone": zone, "parts": [{"text": t, "pasted": p} for t, p in parts]}


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Turns the API would store, kept here instead of in a database."""
    kept: list[dict[str, Any]] = []

    def store(conn: Any, **turn: Any) -> turns.Stored:
        kept.append(turn)
        return turns.Stored(len(kept), new=True)

    @contextmanager
    def no_connection(url: str) -> Iterator[object]:
        yield object()

    monkeypatch.setattr("app.web_api.turns.store", store)
    monkeypatch.setattr("app.web_api.connect_autocommit", no_connection)
    return kept


# --- what is stored ------------------------------------------------------------------


def test_typed_and_pasted_are_kept_apart(
    monkeypatch: pytest.MonkeyPatch, stored: list[dict[str, Any]]
) -> None:
    response = _post(
        _client(monkeypatch),
        _request(
            ("What does ", False),
            ("Please review the contract.", True),
            (" want from me?", False),
            ("Thanks, Kim", True),
        ),
    )

    assert response.status_code == 202
    assert response.json() == {"status": "queued", "turn_id": 1}
    assert stored == [
        {
            "key": KEY,
            "typed": "What does  want from me?",
            "pasted": "Please review the contract.\nThanks, Kim",
        }
    ]


def test_with_nothing_pasted_the_paste_is_none(
    monkeypatch: pytest.MonkeyPatch, stored: list[dict[str, Any]]
) -> None:
    _post(_client(monkeypatch), _request(("Am I free tomorrow at 3pm?", False)))
    assert stored[0]["pasted"] is None


def test_another_zone_is_refused_and_nothing_is_stored(
    monkeypatch: pytest.MonkeyPatch, stored: list[dict[str, Any]]
) -> None:
    """The two sides must agree on what "tomorrow" means (D1)."""
    response = _post(_client(monkeypatch), _request(("tomorrow?", False), zone="Europe/London"))
    assert response.status_code == 409
    assert response.json()["status"] == "zone_mismatch"
    assert stored == []


@pytest.mark.parametrize(
    "body",
    [
        _request(("   ", False), ("Only pasted.", True)),
        _request(("x" * 2_001, False)),
        _request(("Summarize", False), ("y" * 16_001, True)),
        _request(("Summarize", False), ("y" * 9_000, True), ("z" * 9_000, True)),
        _request(("Hi", False), key="short"),
        _request(("Hi", False), key="has spaces in it, not allowed"),
        {"key": KEY, "zone": ZONE, "parts": []},
        {"key": KEY, "zone": ZONE, "parts": [{"text": "Hi", "pasted": False, "extra": 1}]},
    ],
)
def test_a_request_out_of_bounds_is_refused(
    monkeypatch: pytest.MonkeyPatch, stored: list[dict[str, Any]], body: dict[str, Any]
) -> None:
    response = _post(_client(monkeypatch), body)
    assert response.status_code == 422
    assert stored == []


def test_a_refusal_never_echoes_what_the_owner_wrote(
    monkeypatch: pytest.MonkeyPatch, stored: list[dict[str, Any]]
) -> None:
    body = _request(("echo-marker " * 300, False))
    response = _post(_client(monkeypatch), body)
    assert response.status_code == 422
    assert "echo-marker" not in response.text


def test_nothing_is_read_without_the_secret(
    monkeypatch: pytest.MonkeyPatch, stored: list[dict[str, Any]]
) -> None:
    response = _post(_client(monkeypatch), _request(("Hi", False)), secret="wrong")
    assert response.status_code == 401
    assert stored == []


# --- on Postgres: one turn per key -----------------------------------------------------


@pytest.fixture
def on_postgres(monkeypatch: pytest.MonkeyPatch, conn: psycopg.Connection) -> TestClient:
    @contextmanager
    def the_test_connection(url: str) -> Iterator[psycopg.Connection]:
        yield conn

    conn.execute("DELETE FROM chat_turns")  # rolled back with the test
    monkeypatch.setattr("app.web_api.connect_autocommit", the_test_connection)
    return _client(monkeypatch)


@pytest.mark.integration
def test_a_resend_finds_the_turn_already_stored(
    on_postgres: TestClient, conn: psycopg.Connection
) -> None:
    """The web app gives up after ten seconds and may send again (D1)."""
    body = _request(("Summarize this", False), ("Please review.", True))
    first = _post(on_postgres, body).json()
    again = _post(on_postgres, body).json()

    assert first == again
    rows = conn.execute("SELECT typed, pasted, status FROM chat_turns").fetchall()
    assert rows == [("Summarize this", "Please review.", "queued")]


@pytest.mark.integration
def test_each_key_is_its_own_turn(on_postgres: TestClient, conn: psycopg.Connection) -> None:
    one = _post(on_postgres, _request(("First", False), key="a" * 20)).json()
    two = _post(on_postgres, _request(("Second", False), key="b" * 20)).json()
    assert one["turn_id"] != two["turn_id"]
    assert conn.execute("SELECT count(*) FROM chat_turns").fetchone() == (2,)
