"""The web process.

One FastAPI app carries the Telegram webhook and the health check. M07 adds the
scheduler to the same process -- webhook plus in-process scheduling is one
deployable and one failure mode, where long-polling plus a cron worker plus an
API would be three.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hmac import compare_digest
from typing import Any

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request

from app.bootstrap import materialise_secrets
from app.config import Settings, get_settings
from app.google.auth import token_store
from app.graph.runner import graph_session
from app.telegram.client import TelegramClient
from app.telegram.handler import NotAllowedError, TelegramHandler

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health")
def health() -> dict[str, Any]:
    """Liveness plus the two things that silently rot: the database and the
    seven-day Google token."""
    settings = get_settings()
    status: dict[str, Any] = {"status": "ok", "dry_run": settings.dry_run}

    try:
        health_info = token_store(settings).health()
        status["token_days_remaining"] = round(health_info.days_remaining, 1)
        if health_info.needs_reauth_soon:
            status["status"] = "degraded"
            status["warning"] = "Google token expires soon -- run `tasks.ps1 reauth`"
    except Exception as exc:  # a health check that raises is not a health check
        status["status"] = "degraded"
        status["token_error"] = str(exc)

    return status


@router.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, str]:
    settings = get_settings()
    _verify_secret(settings, x_telegram_bot_api_secret_token)

    update: dict[str, Any] = await request.json()

    with graph_session(settings) as session:
        handler = TelegramHandler(
            session=session,
            bot=TelegramClient(_require_token(settings)),
            allowed_chat_ids=frozenset(settings.allowed_chat_ids),
            user_timezone=settings.user_timezone,
        )
        try:
            outcome = handler.handle(update)
        except NotAllowedError:
            # Deliberately 200: a 4xx makes Telegram retry, and there is no
            # point retrying an unauthorised chat. Logged, then dropped.
            return {"status": "rejected"}

    return {"status": "ok", "detail": outcome}


def _verify_secret(settings: Settings, provided: str | None) -> None:
    """Without this, the webhook URL is the only thing standing between the
    internet and a bot that can write to your calendar.

    `compare_digest` rather than `!=` so the comparison does not leak the
    secret's prefix through response timing. `Settings` guarantees a configured
    secret is non-blank, so an empty header can never match.
    """
    if settings.telegram_webhook_secret is None:
        raise HTTPException(status_code=503, detail="TELEGRAM_WEBHOOK_SECRET is not configured")
    expected = settings.telegram_webhook_secret.get_secret_value()
    if provided is None or not compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail="bad secret token")


def _require_token(settings: Settings) -> str:
    if settings.telegram_bot_token is None:
        raise HTTPException(status_code=503, detail="TELEGRAM_BOT_TOKEN is not configured")
    return settings.telegram_bot_token.get_secret_value()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Before settings: `get_settings()` validates paths that only exist once the
    # base64 credential vars have been decoded to disk.
    for path in materialise_secrets():
        log.info("materialised %s", path)

    settings = get_settings()  # fail fast on misconfiguration, not on first request

    if settings.migrate_on_boot:
        from app.store.db import connect, migrate

        with connect(settings.database_url) as conn:
            applied = migrate(conn)
        log.info("migrations applied: %s", ", ".join(applied) or "none pending")

    scheduler = None
    if settings.run_scheduler:
        from app.jobs.scheduler import build_scheduler

        scheduler = build_scheduler(settings)
        scheduler.start()
        log.info("scheduler started: poll every %d min", settings.poll_interval_minutes)

    try:
        yield
    finally:
        if scheduler is not None:
            # Without this, a redeploy can leave a poll mid-flight holding a
            # claimed message that no longer has a process behind it.
            scheduler.shutdown(wait=True)


def create_app() -> FastAPI:
    app = FastAPI(title="mailagent", lifespan=lifespan)
    app.include_router(router)
    return app


app = create_app()
