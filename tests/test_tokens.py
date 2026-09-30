from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.google.tokens import MintedUnder, TokenNotFoundError, TokenStore


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


# --- metadata that survives (M15) ------------------------------------------


def test_a_routine_save_keeps_how_the_token_was_minted(store: TokenStore) -> None:
    """Access tokens are refreshed hourly. If each refresh dropped `minted_under`,
    the evidence M15 depends on would be gone within the hour."""
    issued = datetime(2026, 10, 6, tzinfo=UTC)
    store.save('{"refresh_token": "abc"}', issued_at=issued, minted_under="production")

    store.save('{"refresh_token": "abc", "token": "refreshed"}')

    metadata = store.metadata()
    assert metadata.minted_under == "production"
    assert metadata.issued_at == issued
    assert store.load()[0] == '{"refresh_token": "abc", "token": "refreshed"}'


def test_a_token_from_before_m15_reads_as_testing(tmp_path: Path) -> None:
    """Every token minted before this field existed was minted in Testing."""
    key = Fernet.generate_key().decode()
    path = tmp_path / "token.enc"
    legacy = '{"credentials": "{}", "issued_at": "2026-09-01T00:00:00+00:00"}'
    path.write_bytes(Fernet(key.encode()).encrypt(legacy.encode()))

    assert TokenStore(path, key).metadata().minted_under == "testing"


def test_fields_this_version_does_not_know_survive_a_save(tmp_path: Path) -> None:
    """A later version's metadata must not be erased by an older process refreshing."""
    key = Fernet.generate_key().decode()
    path = tmp_path / "token.enc"
    newer = (
        '{"credentials": "{}", "issued_at": "2026-09-01T00:00:00+00:00",'
        ' "minted_under": "production", "added_later": 7}'
    )
    path.write_bytes(Fernet(key.encode()).encrypt(newer.encode()))

    TokenStore(path, key).save('{"token": "refreshed"}')

    raw = Fernet(key.encode()).decrypt(path.read_bytes()).decode()
    assert '"added_later": 7' in raw


def test_reauth_refuses_to_guess_how_the_token_was_minted() -> None:
    from app.google import reauth

    with pytest.raises(SystemExit) as exit_info:
        reauth.main([])

    assert exit_info.value.code == 2


def test_reauth_records_the_publishing_status_it_was_given(
    monkeypatch: pytest.MonkeyPatch, store: TokenStore
) -> None:
    from app.google import reauth

    seen: list[str] = []

    def _consent(settings: object, *, minted_under: MintedUnder) -> None:
        seen.append(minted_under)
        store.save("{}", issued_at=datetime.now(UTC), minted_under=minted_under)

    monkeypatch.setattr(reauth, "get_settings", lambda: object())
    monkeypatch.setattr(reauth, "run_consent_flow", _consent)
    monkeypatch.setattr(reauth, "token_store", lambda settings: store)

    reauth.main(["--minted-under", "production"])

    assert seen == ["production"]
    assert store.metadata().minted_under == "production"
