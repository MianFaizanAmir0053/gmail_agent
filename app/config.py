"""Typed application settings.

Settings are validated once, at startup, via ``get_settings()`` -- not at import
time. A missing ``ANTHROPIC_API_KEY`` should fail when the process boots, not
three nodes into a graph run.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
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

    ai_gateway_api_key: SecretStr | None = None
    """Vercel AI Gateway key. Needed only when `classify_model` is an evaluation
    model the gateway serves, such as `typesafe-ai/jev`, rather than a Gemini one."""

    monthly_budget_usd: float = Field(default=40.0, ge=0, allow_inf_nan=False)
    """Model spending stops here each UTC month (M17, D5). Raising it means
    setting `MONTHLY_BUDGET_USD` and restarting. Zero stops every call."""

    message_ceiling_usd: float = Field(default=0.50, ge=0, allow_inf_nan=False)
    """What one message may spend on model calls before it is skipped as too
    costly to read (M17, D5)."""

    # --- Retrieval (M10) ----------------------------------------------------
    embedding_model: str = "gemini-embedding-001"
    """Pinned to the GA model rather than `gemini-embedding-2`.

    Changing this invalidates the entire corpus: vectors from two models are not
    comparable, so a swap means re-embedding everything before search works
    again. Stability is worth more here than a benchmark point, and M12's
    retrieval baseline is meaningless if the embedder moves underneath it.
    """

    embedding_dimensions: int = 1536
    """Must equal the width of `chunks.embedding`; ingestion refuses to start
    otherwise. See `migrations/004_pgvector.sql` for why it is not 3072."""

    retrieval_mode: Literal["vector", "hybrid"] = "vector"
    """How `search_context` ranks (M12).

    `vector` is the default because it is what the measurement supports, not
    because fusion was never built. Over 29 hand-labelled queries the keyword
    half never surfaced a relevant message vector search had missed -- not even
    on bare order references, the one category it was expected to win -- so
    fusion could only displace correct results, and hit@5 fell from 100% to 93%.
    `results/retrieval-comparison.md` has the table.
    """

    search_context_enabled: bool = True
    """Give the extractor the `search_context` tool (M11).

    Not free: a message the model decides to research costs an extra round trip
    plus the retrieved text as input tokens. Worth it when the mailbox has
    history to draw on, and switchable off both to save quota and to measure the
    difference -- the eval harness runs with it off by default, so the frozen
    baseline stays a like-for-like comparison.
    """

    user_timezone: str = "UTC"
    """IANA zone used to ground relative dates and render approval cards."""

    owner_email: str = ""
    """The mailbox owner. Stripped from extracted attendee lists -- you are not
    an attendee of your own meeting."""

    owner_aliases: list[str] = Field(default_factory=list)
    """Other addresses that reach the owner (M15 measurement), as a JSON list.
    Mail to an alias missing from here is not recognised as an ask, so the
    loose-ends count undercounts by whatever it misses."""

    measure_exclude_senders: list[str] = Field(default_factory=list)
    """Senders the M15 measurement ignores, as a JSON list. For the owner's
    own test mail, such as the planted day-1 meeting."""

    # --- Google (M01) -------------------------------------------------------
    google_client_secrets_path: str | None = None
    google_token_path: str | None = None
    google_token_standby_path: str | None = None
    """A second token, minted a few days after the primary (M15).

    If Google rejects the primary, polling carries on with this one, so the
    unattended window survives the primary's death. That death is still
    recorded, because it is the evidence being gathered."""
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

    # --- Web channel (M16) ----------------------------------------------------
    web_api_secret: SecretStr | None = None
    """The bearer secret the web app's server sends to `/api/*`. It lives only
    in the Next.js server environment and here; the browser never holds it.
    Unset means the API answers 503."""

    vapid_private_key: SecretStr | None = None
    """Signs web pushes. Generate the pair with `.\\tasks.ps1 vapid`."""

    web_app_url: str | None = None
    """The web app's URL (Vercel). Web push sends it to Apple and Google as the
    VAPID subject, in place of the owner's email. Push is off until both this
    and `vapid_private_key` are set."""

    pairing_enabled: bool = False
    """Serve `/api/pairing/*`, the iPhone sign-in fallback (D4).

    Off by default, so the routes answer 404 as if they did not exist. Turn it
    on only if Google sign-in fails inside the installed iPhone app, and set
    the web app's `PAIRING_ENABLED` to match. A pairing code admits whoever
    types it, so the fewer days it can be asked for, the better."""

    # --- Scheduling (M07) ---------------------------------------------------
    run_scheduler: bool = False
    """Start the in-process poller. Off by default so local `serve` and tests
    do not quietly start processing real mail."""

    poll_interval_minutes: int = 10
    poll_batch_size: int = 10
    """Gmail's own limits are generous; the constraint is the per-day Gemini
    quota, which a tight loop over a busy inbox would burn through by lunchtime."""

    # --- Scheduled ingestion (M14) -------------------------------------------
    ingest_enabled: bool = False
    """Run retrieval ingestion on a timer.

    Off by default because it is the only scheduled job that spends money
    without a human having asked for anything. A poll that finds no mail costs
    nothing; an ingest that finds new mail always embeds it.
    """

    ingest_interval_hours: int = 24
    ingest_window_days: int = 2
    """How far back the incremental query reaches.

    Deliberately wider than the interval. The overlap is free -- content-hash
    dedupe means a second pass over the same mail embeds nothing -- and it is
    what stops a single missed run leaving a permanent hole in the corpus.
    """

    ingest_limit: int = 100
    ingest_backfill_limit: int = 500

    migrate_on_boot: bool = False
    """Apply pending migrations at startup. Convenient on a single-instance
    deploy, wrong the moment there are two -- both would race."""

    @field_validator("web_app_url", mode="before")
    @classmethod
    def _web_app_origin(cls, value: object) -> object:
        """The bare `https://host` that becomes the VAPID subject.

        py_vapid accepts nothing more -- no path, no port, not even a trailing
        slash -- and a subject it rejects fails every push with only a warning
        in the log. A trailing slash is forgiven; anything else fails at boot.
        """
        if not isinstance(value, str):
            return value
        origin = value.strip().rstrip("/")
        if not origin:
            return None
        if not re.fullmatch(r"https://[\w-]+(\.[\w-]+)+", origin):
            raise ValueError(
                "WEB_APP_URL must be the web app's bare https origin, such as "
                "https://example.vercel.app, with no path or port: web push "
                "sends it as the VAPID subject"
            )
        return origin

    @field_validator(
        "telegram_bot_token",
        "telegram_webhook_secret",
        "fernet_key",
        "ai_gateway_api_key",
        "web_api_secret",
        "vapid_private_key",
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

    @model_validator(mode="after")
    def _gateway_classifier_needs_its_key(self) -> Settings:
        """Fail at boot rather than on the first email, where every triage call
        would come back 401."""
        # Imported here so loading settings never drags in the HTTP client.
        from app.extraction.evaluation import EVALUATION_MODELS

        if self.classify_model in EVALUATION_MODELS and self.ai_gateway_api_key is None:
            raise ValueError(
                f"CLASSIFY_MODEL={self.classify_model} runs on Vercel AI Gateway; "
                "set AI_GATEWAY_API_KEY"
            )
        return self


def transaction_pooler_problem(url: str) -> str | None:
    """Why `url` cannot be used, if it points at a transaction-mode pooler.

    A transaction-mode pooler hands each statement to whichever server
    connection is free, so the prepared statements psycopg creates for
    LangGraph's checkpointer vanish between calls. Every checkpoint write then
    fails at runtime, hours after a clean boot. Catching it here moves that
    failure to startup.

    Only the two poolers this project is likely to be pointed at are
    recognised. The message never repeats the URL: it carries the password,
    and startup errors end up in hosted logs.
    """
    if "://" not in url:
        return None  # a libpq key=value DSN; nothing to recognise

    parts = urlsplit(url)
    host = parts.hostname or ""
    if host.endswith(".pooler.supabase.com") and parts.port == 6543:
        return (
            "points at Supabase's transaction-mode pooler (port 6543), which breaks the "
            "prepared statements LangGraph's checkpointer uses. Use the direct connection "
            "(db.<project>.supabase.co:5432) or the session pooler on port 5432."
        )
    if host.endswith(".neon.tech") and "-pooler." in host:
        return (
            "points at Neon's pooled endpoint (PgBouncer in transaction mode), which breaks "
            "the prepared statements LangGraph's checkpointer uses. Use the same host "
            "without '-pooler'."
        )
    return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and validate settings. Cached.

    Raises `ValidationError` on bad config, and `RuntimeError` for a database
    URL no connection in this app can use.
    """
    settings = Settings()
    if problem := transaction_pooler_problem(settings.database_url):
        # Raised outside pydantic on purpose: a ValidationError would echo the
        # input value, and this one contains the database password.
        raise RuntimeError(f"DATABASE_URL {problem}")
    return settings
