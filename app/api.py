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
from datetime import UTC, datetime, timedelta
from hmac import compare_digest
from typing import Any, cast

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.bootstrap import materialise_secrets
from app.config import Settings, get_settings
from app.google.auth import observe_refreshes, standby_token_store, token_store
from app.google.tokens import RefreshOutcome
from app.jobs.scheduler import decision_recorded
from app.mail.sync import SYNC_EVERY as MAIL_SYNC_EVERY
from app.obs.liveness import LIVENESS, MAIL_SYNC, TOKEN_EVIDENCE, configure_logging
from app.obs.token_report import UNUSABLE_STANDBY, standby_state, token_report
from app.store.db import connect_autocommit
from app.telegram.client import TelegramClient
from app.telegram.handler import NotAllowedError, TelegramHandler
from app.web_api import router as web_router

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health")
def health(authorization: str | None = Header(default=None)) -> JSONResponse:
    """Whether the process is doing its job, from what it already knows.

    503 when polling has stalled or no Google token is usable, so that the
    platform and an external uptime monitor both treat it as down. Nothing here
    touches the database: every successful poll already proves the database
    works, and this endpoint is public and called every few seconds. Reasons
    are fixed phrases; exception text stays in the server log, where hosts and
    paths belong.
    """
    settings = get_settings()
    body: dict[str, Any] = {"status": "ok", "dry_run": settings.dry_run}
    problems: list[str] = []

    if settings.run_scheduler:
        now = datetime.now(UTC)
        last_ok = LIVENESS.last_poll_ok_at
        body["last_poll_ok_at"] = last_ok.isoformat() if last_ok else None
        interval = timedelta(minutes=settings.poll_interval_minutes)
        if LIVENESS.poll_overdue(now, interval):
            problems.append("no successful poll in three intervals")

        if LIVENESS.decision_stuck(now):
            problems.append("a decision has been open for over an hour")

        # The owner's business, not the internet's: whether a proposal is
        # waiting, and whether any phone would hear a push. The platform and
        # the uptime monitor need only the status code.
        if _is_owner(settings, authorization):
            oldest = LIVENESS.oldest_open_decision_at
            body["oldest_open_decision_seconds"] = (
                round((now - oldest).total_seconds()) if oldest else None
            )
            # Zero is visible, not a failure: before the first phone subscribes
            # there is simply nobody to push to.
            body["push_subscriptions"] = LIVENESS.push_subscriptions

        # The mail sync (M20, D6): judged by its last pass that reached the
        # end of history, so a sync that is alive but behind shows too.
        if MAIL_SYNC.overdue(now, MAIL_SYNC_EVERY):
            problems.append("no mail sync pass reached the end of history in three intervals")
        # Each pass can reach the end of history while every fetch fails.
        if MAIL_SYNC.fetches_failing(now, MAIL_SYNC_EVERY):
            problems.append("every mail sync fetch has failed for three intervals")
        if _is_owner(settings, authorization):
            body["mail_sync"] = MAIL_SYNC.report(now)

    try:
        state, countdown = token_report(token_store(settings), TOKEN_EVIDENCE)
    except Exception:  # a health check that raises is not a health check
        log.exception("token health check failed")
        problems.append("google token unreadable")
    else:
        body["token_state"] = state
        if state in ("testing", "production-unconfirmed"):
            # For an unconfirmed production token this is the deadline a
            # Testing token would have had: the moment the claim gets tested.
            body["token_days_remaining"] = round(countdown.days_remaining, 1)
        if state == "testing" and countdown.needs_reauth_soon:
            body["warning"] = "Google token expires soon -- run `tasks.ps1 reauth`"

        standby = standby_state(standby_token_store(settings), TOKEN_EVIDENCE)
        if standby is not None:
            body["standby_token_state"] = standby
        if state != "expired":
            body["token_in_use"] = "primary"
        elif standby not in UNUSABLE_STANDBY:
            # The primary's death is the evidence M15 is gathering; the standby
            # is what keeps the window from restarting because of it.
            body["token_in_use"] = "standby"
        else:
            problems.append("google token expired")

    if problems:
        body["status"] = "degraded"
        body["problems"] = problems
        return JSONResponse(body, status_code=503)
    return JSONResponse(body)


def _is_owner(settings: Settings, authorization: str | None) -> bool:
    """Whether the request carries `WEB_API_SECRET`. Never raises."""
    if settings.web_api_secret is None or authorization is None:
        return False
    scheme, _, token = authorization.partition(" ")
    expected = settings.web_api_secret.get_secret_value().encode()
    return scheme == "Bearer" and compare_digest(token.encode(), expected)


@router.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, str]:
    settings = get_settings()
    # Checked before the body is read: an unauthenticated caller gets nothing
    # parsed on its behalf.
    _verify_secret(settings, x_telegram_bot_api_secret_token)

    update: dict[str, Any] = await request.json()

    try:
        # The handler's database work blocks, so it runs in the thread pool
        # rather than on the event loop that also serves /health.
        outcome = await run_in_threadpool(_handle_telegram, settings, update)
    except NotAllowedError:
        # Deliberately 200: a 4xx makes Telegram retry, and there is no
        # point retrying an unauthorised chat. Logged, then dropped.
        return {"status": "rejected"}

    return {"status": "ok", "detail": outcome}


def _handle_telegram(settings: Settings, update: dict[str, Any]) -> str:
    """Record what the owner tapped. Since M16 this needs no graph: the worker
    applies the decision."""
    with connect_autocommit(settings.database_url) as conn:
        handler = TelegramHandler(
            conn=conn,
            bot=TelegramClient(_require_token(settings)),
            allowed_chat_ids=frozenset(settings.allowed_chat_ids),
            on_queued=decision_recorded,
            dry_run=settings.dry_run,
        )
        return handler.handle(update)


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
    # Bytes, not str: Starlette decodes headers as latin-1, and compare_digest
    # raises on a str with a character outside ASCII -- a 500 and a traceback
    # for any caller who sends one.
    if provided is None or not compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="bad secret token")


def _require_token(settings: Settings) -> str:
    if settings.telegram_bot_token is None:
        raise HTTPException(status_code=503, detail="TELEGRAM_BOT_TOKEN is not configured")
    return settings.telegram_bot_token.get_secret_value()


def _record_refresh(settings: Settings, outcome: RefreshOutcome) -> None:
    """Keep a refresh as evidence: in memory for `/health`, in `job_runs` for
    the exit criterion and for the next boot."""
    from app.jobs.scheduler import record_tick

    TOKEN_EVIDENCE.record(outcome)
    error = None if outcome.ok else ("invalid_grant" if outcome.rejected else "refresh failed")
    record_tick(
        settings,
        "token_refresh",
        outcome.at,
        ok=outcome.ok,
        error=error,
        token_issued_at=outcome.issued_at,
    )


def _seed_token_evidence(settings: Settings) -> None:
    """Reload what earlier processes proved about the current token.

    Confirmation takes a week to earn and a redeploy to lose, if it lives only
    in memory. Never raises: an unreadable token is `/health`'s to report.
    """
    from app.store.db import connect
    from app.store.job_runs import JobRuns

    try:
        issued_at = token_store(settings).metadata().issued_at
        with connect(settings.database_url) as conn:
            last_ok_at, rejected = JobRuns(conn).refresh_evidence(issued_at)
    except Exception:
        log.exception("could not load token refresh evidence")
        return
    TOKEN_EVIDENCE.seed(issued_at, last_ok_at=last_ok_at, rejected=rejected)


def _settle_stranded_claims(settings: Settings, *, claimed_before: datetime) -> None:
    """Every `claimed` row made before `claimed_before` (M20, D4): a thread
    that parked is left to reconciliation, one the feed would offer again is
    released, and the rest are marked FAILED rather than left `claimed`,
    where nothing would ever look at them again.

    Never raises: boot must complete. A claim that cannot be settled is
    logged and left for the next pass, or the next boot.
    """
    from app.graph.checkpointer import postgres_checkpointer
    from app.graph.nodes import Deps
    from app.graph.runner import GraphSession
    from app.mail.feed import recover_stranded
    from app.store.db import connect

    try:
        with (
            connect(settings.database_url) as conn,
            postgres_checkpointer(settings.database_url) as saver,
        ):
            # Reading a thread's state builds the graph but runs none of its
            # nodes, so their dependencies are never needed here.
            threads = GraphSession(
                deps=cast(Deps, None), conn=conn, checkpointer=saver, trace=False
            )
            result = recover_stranded(
                conn,
                parked=lambda message_id: threads.thread(message_id).parked,
                forget=saver.delete_thread,
                claimed_before=claimed_before,
            )
    except Exception:
        log.exception("could not settle stranded claims; the next pass tries again")
        return
    if result.left or result.released or result.failed or result.errors:
        log.warning(
            "stranded claims: %d left for reconciliation, %d released, %d failed, %d unsettled",
            result.left,
            result.released,
            result.failed,
            result.errors,
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()

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
        from app.jobs.scheduler import activate, build_scheduler
        from app.mail.feed import STRANDED_AFTER

        # Nothing is in flight in this process yet, so a claim made before boot
        # was left by a predecessor that died mid-message -- unless a poller in
        # another process still holds it. The poller has no lock to ask, so a
        # claim younger than STRANDED_AFTER waits for a second pass, once it is
        # that old; neither pass touches a claim made after boot, by this
        # process's own poller.
        booted_at = datetime.now(UTC)
        _settle_stranded_claims(settings, claimed_before=booted_at - STRANDED_AFTER)

        _seed_token_evidence(settings)
        observe_refreshes(lambda outcome: _record_refresh(settings, outcome))

        scheduler = build_scheduler(settings)
        scheduler.add_job(
            _settle_stranded_claims,
            "date",
            run_date=booted_at + STRANDED_AFTER,
            args=[settings],
            kwargs={"claimed_before": booted_at},
            id="stranded_claims",
            # However late: by default a job over a second late is skipped.
            misfire_grace_time=None,
        )
        activate(scheduler)
        log.info("scheduler started: poll every %d min", settings.poll_interval_minutes)

    try:
        yield
    finally:
        if scheduler is not None:
            from app.jobs.poll import STOPPING

            # Stop taking new claims first, then wait for the message in hand.
            # Without both, a redeploy can leave a poll mid-flight holding a
            # claimed message that no longer has a process behind it.
            STOPPING.set()
            scheduler.shutdown(wait=True)


def create_app() -> FastAPI:
    # No /docs, /redoc or /openapi.json: they would describe every route to
    # anyone on the internet, and the owner has no use for them.
    app = FastAPI(
        title="mailagent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.include_router(router)
    app.include_router(web_router)
    return app


app = create_app()
