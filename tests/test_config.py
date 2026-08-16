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
