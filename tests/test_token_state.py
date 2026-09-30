"""Which state a Google token is in, and the evidence behind it (M15)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials

from app.config import Settings
from app.google import auth
from app.google.tokens import RefreshOutcome, TokenMetadata, token_state
from app.obs.liveness import TokenEvidence

ISSUED = datetime(2026, 10, 6, tzinfo=UTC)
LAPSE = ISSUED + timedelta(days=7)


@pytest.fixture(autouse=True)
def _fresh_dead_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_dead_tokens` is per process. Without this, the token one test kills
    stays dead for every later test minted at the same instant."""
    monkeypatch.setattr(auth, "_dead_tokens", set())


def _meta(minted_under: Any) -> TokenMetadata:
    return TokenMetadata(issued_at=ISSUED, minted_under=minted_under)


# --- the four states -------------------------------------------------------


def test_a_testing_token_counts_down() -> None:
    state = token_state(
        _meta("testing"), now=ISSUED + timedelta(days=3), last_ok_refresh_at=None, rejected=False
    )
    assert state == "testing"


def test_a_testing_token_is_expired_after_seven_days() -> None:
    state = token_state(
        _meta("testing"), now=LAPSE + timedelta(hours=1), last_ok_refresh_at=None, rejected=False
    )
    assert state == "expired"


def test_a_production_token_is_unconfirmed_until_it_outlives_seven_days() -> None:
    state = token_state(
        _meta("production"),
        now=LAPSE + timedelta(hours=1),
        last_ok_refresh_at=LAPSE - timedelta(hours=1),
        rejected=False,
    )
    assert state == "production-unconfirmed"


def test_a_refresh_after_seven_days_confirms_a_production_token() -> None:
    state = token_state(
        _meta("production"),
        now=LAPSE + timedelta(hours=2),
        last_ok_refresh_at=LAPSE + timedelta(hours=1),
        rejected=False,
    )
    assert state == "production-confirmed"


def test_google_rejecting_the_refresh_token_is_expired_whatever_the_status() -> None:
    state = token_state(
        _meta("production"),
        now=LAPSE + timedelta(hours=2),
        last_ok_refresh_at=LAPSE + timedelta(hours=1),
        rejected=True,
    )
    assert state == "expired"


# --- evidence, per token ---------------------------------------------------


def test_evidence_is_kept_per_token() -> None:
    """An old token's death must never be read as the new token's."""
    evidence = TokenEvidence()
    old, new = ISSUED, ISSUED + timedelta(days=4)
    evidence.record(RefreshOutcome(issued_at=old, at=LAPSE, ok=False, rejected=True))

    assert evidence.for_token(old).rejected is True
    assert evidence.for_token(new).rejected is False


def test_a_later_successful_refresh_clears_a_rejection() -> None:
    evidence = TokenEvidence()
    evidence.record(RefreshOutcome(issued_at=ISSUED, at=LAPSE, ok=False, rejected=True))
    evidence.record(
        RefreshOutcome(issued_at=ISSUED, at=LAPSE + timedelta(hours=1), ok=True, rejected=False)
    )

    assert evidence.for_token(ISSUED).rejected is False
    assert evidence.for_token(ISSUED).last_ok_at == LAPSE + timedelta(hours=1)


# --- load_credentials reports what happened --------------------------------


class _FakeCredentials:
    valid = False
    expired = True
    refresh_token = "r"

    def __init__(self, error: Exception | None) -> None:
        self._error = error

    def refresh(self, request: Any) -> None:
        if self._error is not None:
            raise self._error

    def to_json(self) -> str:
        return '{"refresh_token": "r", "token": "new"}'


def _settings_with_token(tmp_path: Path) -> Settings:
    key = Fernet.generate_key().decode()
    settings = Settings(
        _env_file=None,
        database_url="postgresql://localhost/test",
        gemini_api_key="k",
        google_token_path=str(tmp_path / "token.enc"),
        fernet_key=key,
    )
    auth.token_store(settings).save("{}", issued_at=ISSUED, minted_under="production")
    return settings


def _observe(monkeypatch: pytest.MonkeyPatch, error: Exception | None) -> list[RefreshOutcome]:
    seen: list[RefreshOutcome] = []
    monkeypatch.setattr(
        Credentials, "from_authorized_user_info", lambda info, scopes: _FakeCredentials(error)
    )
    monkeypatch.setattr(auth, "_refresh_observer", seen.append)
    return seen


def test_a_successful_refresh_is_reported_against_its_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _observe(monkeypatch, error=None)

    auth.load_credentials(_settings_with_token(tmp_path))

    assert [(o.issued_at, o.ok, o.rejected) for o in seen] == [(ISSUED, True, False)]


def test_invalid_grant_is_reported_as_a_rejection_and_still_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _observe(monkeypatch, error=RefreshError("invalid_grant: Token has been expired"))

    with pytest.raises(RefreshError):
        auth.load_credentials(_settings_with_token(tmp_path))

    assert [(o.ok, o.rejected) for o in seen] == [(False, True)]


def test_a_broken_observer_never_breaks_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _explode(outcome: RefreshOutcome) -> None:
        raise OSError("database down")

    monkeypatch.setattr(
        Credentials, "from_authorized_user_info", lambda info, scopes: _FakeCredentials(None)
    )
    monkeypatch.setattr(auth, "_refresh_observer", _explode)

    auth.load_credentials(_settings_with_token(tmp_path))  # must not raise
