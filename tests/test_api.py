"""Webhook authentication and health reporting.

The webhook is the one route reachable from the open internet, and it can cause
a calendar write. Its authentication is worth more tests than its happy path.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api import router
from app.bootstrap import SecretDecodeError, materialise_secrets
from app.config import Settings

SECRET = "s3cret-token"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "database_url": "postgresql://localhost/test",
        "gemini_api_key": "test-key",
        "telegram_webhook_secret": SecretStr(SECRET),
        "telegram_bot_token": SecretStr("123:abc"),
        "allowed_chat_ids": [4242],
    }
    return Settings(**(base | overrides))


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A bare app with just the router -- no lifespan, so no scheduler starts."""
    monkeypatch.setattr("app.api.get_settings", _settings)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _post(client: TestClient, secret: str | None, body: Any = None) -> Any:
    headers = {} if secret is None else {"X-Telegram-Bot-Api-Secret-Token": secret}
    return client.post("/telegram/webhook", json=body or {"update_id": 1}, headers=headers)


# --- webhook authentication ------------------------------------------------


def test_missing_secret_header_is_rejected(client: TestClient) -> None:
    assert _post(client, None).status_code == 403


def test_wrong_secret_is_rejected(client: TestClient) -> None:
    assert _post(client, "not-the-secret").status_code == 403


def test_empty_secret_header_is_rejected(client: TestClient) -> None:
    """Regression: this returned 200 in a live run.

    A blank `TELEGRAM_WEBHOOK_SECRET=` in .env became `SecretStr("")`, which is
    not None -- so the route read it as configured and then matched an empty
    header, authenticating anyone who sent one.
    """
    assert _post(client, "").status_code == 403


def test_blank_configured_secret_is_treated_as_unset() -> None:
    """The fix, at the settings layer: whitespace is not a secret."""
    assert _settings(telegram_webhook_secret="   ").telegram_webhook_secret is None
    assert _settings(telegram_bot_token="").telegram_bot_token is None


def test_a_blank_secret_makes_the_route_refuse_not_allow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.api.get_settings", lambda: _settings(telegram_webhook_secret=""))
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    assert _post(client, "").status_code == 503
    assert _post(client, "anything").status_code == 503


def test_unconfigured_secret_refuses_rather_than_allowing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failing open here would leave the webhook unauthenticated."""
    monkeypatch.setattr("app.api.get_settings", lambda: _settings(telegram_webhook_secret=None))
    app = FastAPI()
    app.include_router(router)

    assert _post(TestClient(app), SECRET).status_code == 503


def test_valid_secret_reaches_the_handler(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Session:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr("app.api.graph_session", lambda settings: _Session())
    monkeypatch.setattr("app.api.TelegramClient", lambda token: object())
    monkeypatch.setattr(
        "app.api.TelegramHandler",
        lambda **kwargs: type("H", (), {"handle": lambda self, u: "handled"})(),
    )

    response = _post(client, SECRET)

    assert response.status_code == 200
    assert response.json()["detail"] == "handled"


def test_unauthorised_chat_gets_200_not_an_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 4xx makes Telegram retry, and retrying a rejected chat is pointless."""
    from app.telegram.handler import NotAllowedError

    class _Session:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *args: object) -> None:
            return None

    def _raise(self: object, update: Any) -> str:
        raise NotAllowedError("nope")

    monkeypatch.setattr("app.api.graph_session", lambda settings: _Session())
    monkeypatch.setattr("app.api.TelegramClient", lambda token: object())
    monkeypatch.setattr(
        "app.api.TelegramHandler", lambda **kwargs: type("H", (), {"handle": _raise})()
    )

    response = _post(client, SECRET)

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"


# --- health ----------------------------------------------------------------


def test_health_reports_dry_run_state(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["dry_run"] is True


def test_health_degrades_rather_than_raising_when_the_token_is_unreadable(
    client: TestClient,
) -> None:
    """A health check that 500s tells a load balancer nothing useful."""
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert "token_error" in body


# --- secret materialisation ------------------------------------------------


def test_base64_secrets_are_written_to_disk(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "client_secret.json"
    written = materialise_secrets(
        {
            "GOOGLE_CLIENT_SECRETS_B64": base64.b64encode(b'{"installed":{}}').decode(),
            "GOOGLE_CLIENT_SECRETS_PATH": str(target),
        }
    )

    assert written == [str(target)]
    assert target.read_bytes() == b'{"installed":{}}'


def test_absent_variables_are_skipped_not_defaulted(tmp_path: Path) -> None:
    """Locally the real files already exist; overwriting them with nothing
    would be worse than doing nothing."""
    assert materialise_secrets({}) == []


def test_invalid_base64_fails_loudly() -> None:
    """Booting without credentials means a process that looks healthy and
    silently processes no mail."""
    with pytest.raises(SecretDecodeError, match="not valid base64"):
        materialise_secrets({"GOOGLE_TOKEN_B64": "!!!not base64!!!"})
