from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.google.tokens import TokenNotFoundError, TokenStore


@pytest.fixture
def store(tmp_path: Path) -> TokenStore:
    return TokenStore(tmp_path / "nested" / "token.enc", Fernet.generate_key().decode())


def test_roundtrip_preserves_payload(store: TokenStore) -> None:
    store.save('{"refresh_token": "abc"}')
    credentials, _ = store.load()
    assert credentials == '{"refresh_token": "abc"}'


def test_token_is_encrypted_at_rest(store: TokenStore) -> None:
    store.save('{"refresh_token": "super-secret-value"}')
    assert b"super-secret-value" not in store.path.read_bytes()


def test_save_creates_parent_directories(store: TokenStore) -> None:
    store.save("{}")
    assert store.path.exists()


def test_load_without_token_raises(store: TokenStore) -> None:
    with pytest.raises(TokenNotFoundError):
        store.load()


def test_refresh_does_not_extend_the_seven_day_clock(store: TokenStore) -> None:
    """The failure mode this whole design exists to catch.

    Refreshing an access token reuses the same refresh token, so its deadline
    must not move. Only a fresh consent flow resets it.
    """
    original = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    store.save("{}", issued_at=original)

    store.save('{"access_token": "refreshed"}')  # simulates a routine refresh

    _, issued_at = store.load()
    assert issued_at == original


def test_explicit_issued_at_resets_the_clock(store: TokenStore) -> None:
    store.save("{}", issued_at=datetime(2026, 8, 10, tzinfo=UTC))
    store.save("{}", issued_at=datetime(2026, 8, 16, tzinfo=UTC))
    assert store.load()[1] == datetime(2026, 8, 16, tzinfo=UTC)


def test_health_counts_down_from_seven_days(store: TokenStore) -> None:
    issued = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    store.save("{}", issued_at=issued)

    health = store.health(now=issued + timedelta(days=1))

    assert health.expires_at == issued + timedelta(days=7)
    assert health.days_remaining == pytest.approx(6.0)
    assert not health.expired
    assert not health.needs_reauth_soon


def test_health_warns_two_days_out(store: TokenStore) -> None:
    issued = datetime(2026, 8, 16, tzinfo=UTC)
    store.save("{}", issued_at=issued)
    assert store.health(now=issued + timedelta(days=5)).needs_reauth_soon


def test_health_reports_expiry_after_seven_days(store: TokenStore) -> None:
    issued = datetime(2026, 8, 16, tzinfo=UTC)
    store.save("{}", issued_at=issued)

    health = store.health(now=issued + timedelta(days=8))

    assert health.expired
    assert health.days_remaining < 0
