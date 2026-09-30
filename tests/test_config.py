from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings

REQUIRED = ("DATABASE_URL", "GEMINI_API_KEY")


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in REQUIRED:
        monkeypatch.delenv(name, raising=False)


def _settings(gemini_api_key: str = "test-key") -> Settings:
    """Build Settings without reading the developer's real .env."""
    return Settings(
        _env_file=None,
        database_url="postgresql://localhost/test",
        gemini_api_key=gemini_api_key,
    )


def test_missing_required_settings_fail_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_dry_run_defaults_to_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    settings = _settings()
    assert settings.dry_run is True
    assert settings.app_env == "dev"


def test_secrets_are_not_stringified(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    settings = _settings(gemini_api_key="AIza-super-secret")
    assert "AIza-super-secret" not in repr(settings)
    assert settings.gemini_api_key.get_secret_value() == "AIza-super-secret"


def test_allowlist_defaults_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    assert _settings().allowed_chat_ids == []


def _gateway_settings(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> Settings:
    _clear(monkeypatch)
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("CLASSIFY_MODEL", raising=False)
    return Settings(
        _env_file=None,
        database_url="postgresql://localhost/test",
        gemini_api_key="test-key",
        **overrides,  # type: ignore[arg-type]
    )


def test_gemini_triage_needs_no_gateway_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Booting still needs only DATABASE_URL and GEMINI_API_KEY."""
    assert _gateway_settings(monkeypatch).ai_gateway_api_key is None


def test_a_gateway_classifier_without_its_key_fails_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError, match="AI_GATEWAY_API_KEY"):
        _gateway_settings(monkeypatch, classify_model="typesafe-ai/jev")


def test_a_blank_gateway_key_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """`AI_GATEWAY_API_KEY=` in a .env file is not a key -- the lesson of the
    blank webhook secret."""
    with pytest.raises(ValidationError, match="AI_GATEWAY_API_KEY"):
        _gateway_settings(monkeypatch, classify_model="typesafe-ai/jev", ai_gateway_api_key="")


def test_the_gateway_key_is_not_stringified(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _gateway_settings(
        monkeypatch, classify_model="typesafe-ai/jev", ai_gateway_api_key="vck_super_secret"
    )
    assert "vck_super_secret" not in repr(settings)
    assert settings.ai_gateway_api_key is not None
    assert settings.ai_gateway_api_key.get_secret_value() == "vck_super_secret"
