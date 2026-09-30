from __future__ import annotations

from typing import Any

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


@pytest.mark.parametrize("name", ["web_api_secret", "vapid_private_key"])
@pytest.mark.parametrize("blank", ["", "   "])
def test_the_web_channels_blank_secrets_count_as_unset(name: str, blank: str) -> None:
    """M16's secrets get the webhook's fix: blank is not configured."""
    fields: dict[str, Any] = {
        "_env_file": None,
        "database_url": "postgresql://x/y",
        "gemini_api_key": "k",
        name: blank,
    }
    assert getattr(Settings(**fields), name) is None


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
