"""Typed application settings.

Settings are validated once, at startup, via ``get_settings()`` -- not at import
time. A missing ``ANTHROPIC_API_KEY`` should fail when the process boots, not
three nodes into a graph run.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["dev", "prod"] = "dev"

    dry_run: bool = True
    """Master kill switch for external writes.

    Defaults to True so that forgetting to set it is safe rather than
    destructive. Every Calendar write path must check this. Only M06 onwards
    runs with dry_run=False.
    """

    # --- Required -----------------------------------------------------------
    database_url: str
    gemini_api_key: SecretStr

    # --- Models (M03) -------------------------------------------------------
    # Verify what this key can reach with `.\tasks.ps1 models` -- availability
    # varies by key and region, and a wrong name fails at call time.
    #
    # Pin exact versions, never the `-latest` aliases. A frozen eval baseline is
    # only meaningful if the model that produced it can be re-run; an alias
    # silently moves underneath you and the "before" number stops being real.
    #
    # On the free tier this choice is quota-bound, not capability-bound: the
    # newest models allow as few as 20 requests per *day*, which one eval run
    # exhausts. Splitting the two stages across different models also splits the
    # per-model daily quota, which is worth more here than any capability edge.
    #
    # `models.list()` advertises models the key cannot call -- verify with
    # `.\tasks.ps1 models --probe` before changing these.
    extraction_model: str = "gemini-3.6-flash"
    classify_model: str = "gemini-3.5-flash-lite"

    user_timezone: str = "UTC"
    """IANA zone used to ground relative dates and render approval cards."""

    owner_email: str = ""
    """The mailbox owner. Stripped from extracted attendee lists -- you are not
    an attendee of your own meeting."""

    # --- Google (M01) -------------------------------------------------------
    google_client_secrets_path: str | None = None
    google_token_path: str | None = None
    test_calendar_id: str | None = None
    fernet_key: SecretStr | None = None

    # --- Telegram (M06) -----------------------------------------------------
    telegram_bot_token: SecretStr | None = None
    telegram_webhook_secret: SecretStr | None = None
    allowed_chat_ids: list[int] = Field(default_factory=list)
    """Allowlist. An empty list means nobody may talk to the bot.

    Not optional hardening: anyone who finds the bot's username can message it,
    and this bot reads your mailbox. The first entry also receives unsolicited
    proposals.
    """

    public_url: str | None = None
    """Externally reachable base URL, for registering the Telegram webhook."""

    # --- Scheduling (M07) ---------------------------------------------------
    run_scheduler: bool = False
    """Start the in-process poller. Off by default so local `serve` and tests
    do not quietly start processing real mail."""

    poll_interval_minutes: int = 10
    poll_batch_size: int = 10
    """Gmail's own limits are generous; the constraint is the per-day Gemini
    quota, which a tight loop over a busy inbox would burn through by lunchtime."""

    migrate_on_boot: bool = False
    """Apply pending migrations at startup. Convenient on a single-instance
    deploy, wrong the moment there are two -- both would race."""

    @field_validator(
        "telegram_bot_token",
        "telegram_webhook_secret",
        "fernet_key",
        mode="before",
    )
    @classmethod
    def _blank_secret_is_unset(cls, value: object) -> object:
        """An empty secret means "not configured", never "configured as empty".

        A blank `TELEGRAM_WEBHOOK_SECRET=` in a .env file otherwise becomes
        `SecretStr("")`, which is not None -- so the webhook read it as
        configured and then matched an empty header, authenticating anyone who
        sent one. Verified as a live 200 before this validator existed.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and validate settings. Cached; raises ValidationError on bad config."""
    return Settings()
