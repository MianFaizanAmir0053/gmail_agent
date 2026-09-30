"""Standby token failover (M15).

Around day four the owner mints a second production token. If the primary
dies on day seven -- which would prove that "In production" does not lift the
seven-day limit -- polling carries on with the standby, so the eight-day
window does not restart. The primary's death is still recorded: it is the
evidence.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials

from app.bootstrap import materialise_secrets
from app.config import Settings
from app.google import auth
from app.google.tokens import RefreshOutcome

PRIMARY_ISSUED = datetime(2026, 10, 6, tzinfo=UTC)
STANDBY_ISSUED = PRIMARY_ISSUED + timedelta(days=4)


class _FakeCredentials:
    valid = False
    expired = True
    refresh_token = "r"

    def __init__(self, which: str, dead: set[str]) -> None:
        self.which = which
        self._dead = dead

    def refresh(self, request: Any) -> None:
        if self.which in self._dead:
            raise RefreshError("invalid_grant: Token has been expired or revoked.")

    def to_json(self) -> str:
        return json.dumps({"which": self.which})


def _settings(tmp_path: Path, *, standby: bool) -> Settings:
    key = Fernet.generate_key().decode()
    settings = Settings(
        _env_file=None,
        database_url="postgresql://localhost/test",
        gemini_api_key="k",
        google_token_path=str(tmp_path / "token.enc"),
        google_token_standby_path=str(tmp_path / "token-standby.enc") if standby else None,
        fernet_key=key,
    )
    auth.token_store(settings).save(
        json.dumps({"which": "primary"}), issued_at=PRIMARY_ISSUED, minted_under="production"
    )
    standby_store = auth.standby_token_store(settings)
    if standby_store is not None:
        standby_store.save(
            json.dumps({"which": "standby"}), issued_at=STANDBY_ISSUED, minted_under="production"
        )
    return settings


@pytest.fixture
def outcomes(monkeypatch: pytest.MonkeyPatch) -> list[RefreshOutcome]:
    seen: list[RefreshOutcome] = []
    monkeypatch.setattr(auth, "_refresh_observer", seen.append)
    monkeypatch.setattr(auth, "_dead_tokens", set())
    return seen


def _kill(monkeypatch: pytest.MonkeyPatch, *dead: str) -> None:
    monkeypatch.setattr(
        Credentials,
        "from_authorized_user_info",
        lambda info, scopes: _FakeCredentials(info["which"], set(dead)),
    )


def test_a_dead_primary_fails_over_to_the_standby(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcomes: list[RefreshOutcome]
) -> None:
    _kill(monkeypatch, "primary")

    credentials = auth.load_credentials(_settings(tmp_path, standby=True))

    assert credentials.which == "standby"  # type: ignore[attr-defined]
    assert [(o.issued_at, o.ok, o.rejected) for o in outcomes] == [
        (PRIMARY_ISSUED, False, True),  # the evidence
        (STANDBY_ISSUED, True, False),
    ]


def test_a_dead_primary_is_not_retried_on_every_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcomes: list[RefreshOutcome]
) -> None:
    _kill(monkeypatch, "primary")
    settings = _settings(tmp_path, standby=True)

    auth.load_credentials(settings)
    auth.load_credentials(settings)

    assert [o.issued_at for o in outcomes].count(PRIMARY_ISSUED) == 1


def test_without_a_standby_a_dead_primary_still_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcomes: list[RefreshOutcome]
) -> None:
    _kill(monkeypatch, "primary")

    with pytest.raises(RefreshError):
        auth.load_credentials(_settings(tmp_path, standby=False))


def test_when_both_are_dead_the_error_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcomes: list[RefreshOutcome]
) -> None:
    _kill(monkeypatch, "primary", "standby")

    with pytest.raises(RefreshError):
        auth.load_credentials(_settings(tmp_path, standby=True))


def test_boot_writes_the_standby_token_too(tmp_path: Path) -> None:
    target = tmp_path / "secrets" / "token-standby.enc"

    written = materialise_secrets(
        {
            "GOOGLE_TOKEN_STANDBY_B64": "c3RhbmRieQ==",  # "standby"
            "GOOGLE_TOKEN_STANDBY_PATH": str(target),
        }
    )

    assert written == [str(target)]
    assert target.read_bytes() == b"standby"
