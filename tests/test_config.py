from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings, transaction_pooler_problem

REQUIRED = ("DATABASE_URL", "GEMINI_API_KEY")

SUPABASE_TRANSACTION = (
    "postgresql://postgres.abc:hunter2@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres"
)
SUPABASE_SESSION = (
    "postgresql://postgres.abc:hunter2@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres"
)
SUPABASE_DIRECT = "postgresql://postgres:hunter2@db.abc.supabase.co:5432/postgres"
NEON_POOLED = (
    "postgresql://u:hunter2@ep-cool-name-123-pooler.ap-southeast-1.aws.neon.tech/neondb"
    "?sslmode=require"
)
NEON_DIRECT = (
    "postgresql://u:hunter2@ep-cool-name-123.ap-southeast-1.aws.neon.tech/neondb?sslmode=require"
)


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


# --- connection strings ------------------------------------------------------


@pytest.mark.parametrize("url", [SUPABASE_TRANSACTION, NEON_POOLED])
def test_transaction_mode_poolers_are_named_as_a_problem(url: str) -> None:
    """They break the prepared statements LangGraph's checkpointer relies on."""
    problem = transaction_pooler_problem(url)

    assert problem is not None
    assert "hunter2" not in problem  # startup errors end up in hosted logs


@pytest.mark.parametrize(
    "url",
    [
        SUPABASE_SESSION,
        SUPABASE_DIRECT,
        NEON_DIRECT,
        "postgresql://mailagent:mailagent@localhost:5432/mailagent",
        "host=localhost dbname=mailagent",
    ],
)
def test_direct_and_session_connections_are_accepted(url: str) -> None:
    assert transaction_pooler_problem(url) is None


def test_startup_refuses_a_transaction_pooler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", SUPABASE_TRANSACTION)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="session") as caught:
            get_settings()
        assert "hunter2" not in str(caught.value)
    finally:
        get_settings.cache_clear()
