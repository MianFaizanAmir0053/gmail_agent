"""The API the web app calls (M16, D4).

The Next.js server is the only intended caller: it holds `WEB_API_SECRET` and
checks the owner's session before every call, and the browser never reaches
Fly. Fly's URL is still public, so everything here is authenticated first --
before the body is even read, so an unauthenticated caller gets nothing parsed
on its behalf. That is also why the handlers are `async` and hand their
blocking database work to the thread pool, rather than being plain `def`
handlers, which FastAPI would feed a parsed body before they could check a
header.

    POST   /api/decisions            record a decision (202 queued, 409 stale,
                                     404 no proposal, 422 invalid)
    POST   /api/push-subscriptions   store or refresh a browser's subscription
    DELETE /api/push-subscriptions   remove one
"""

from __future__ import annotations

from hmac import compare_digest
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.channel.decide import MAX_CORRECTION_CHARS, DecisionResult, decide
from app.config import Settings, get_settings
from app.jobs.scheduler import decision_recorded
from app.store.db import connect_autocommit

router = APIRouter(prefix="/api")

MAX_ENDPOINT_CHARS = 2048


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    action: Literal["confirm", "edit", "cancel"]
    """No `sweep`: that is an operator ending observe mode, never a tap."""

    correction: str = Field(default="", max_length=MAX_CORRECTION_CHARS)


class SubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=512)
    auth: str = Field(min_length=1, max_length=512)


class Subscription(BaseModel):
    """A browser's `PushSubscription`, as `JSON.stringify` writes it."""

    endpoint: str = Field(max_length=MAX_ENDPOINT_CHARS)
    keys: SubscriptionKeys

    @field_validator("endpoint")
    @classmethod
    def _https_only(cls, value: str) -> str:
        # Every push service is HTTPS; anything else is not a subscription.
        if not value.startswith("https://") or any(ch.isspace() for ch in value):
            raise ValueError("a push endpoint is an https URL")
        return value


class Unsubscribe(BaseModel):
    endpoint: str = Field(min_length=1, max_length=MAX_ENDPOINT_CHARS)


def _verify(settings: Settings, authorization: str | None) -> None:
    """`Authorization: Bearer <WEB_API_SECRET>`, compared in constant time.

    Unset means 503, never "no authentication": `Settings` turns a blank value
    into None, so `compare_digest("", "")` can never let anyone in.
    """
    if settings.web_api_secret is None:
        raise HTTPException(status_code=503, detail="WEB_API_SECRET is not configured")
    scheme, _, token = (authorization or "").partition(" ")
    expected = settings.web_api_secret.get_secret_value()
    if scheme != "Bearer" or not token or not compare_digest(token.encode(), expected.encode()):
        raise HTTPException(
            status_code=401, detail="bad bearer token", headers={"WWW-Authenticate": "Bearer"}
        )


async def _body[M: BaseModel](request: Request, model: type[M]) -> M:
    try:
        raw: Any = await request.json()
    except ValueError:
        raise HTTPException(status_code=422, detail="the body is not JSON") from None
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        # Locations and messages only: the input could be the owner's own text,
        # and a response body ends up in logs and proxies.
        detail = exc.errors(include_url=False, include_input=False, include_context=False)
        raise HTTPException(status_code=422, detail=detail) from None


@router.post("/decisions")
async def post_decision(
    request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    settings = get_settings()
    _verify(settings, authorization)
    body = await _body(request, DecisionRequest)

    result = await run_in_threadpool(_record_decision, settings, body)

    match result.status:
        case "queued":
            decision_recorded()
            return JSONResponse(
                {"status": "queued", "decision_id": result.decision_id}, status_code=202
            )
        case "stale":
            return JSONResponse(
                {
                    "status": "stale",
                    "current_revision": result.current_revision,
                    "detail": result.detail,
                },
                status_code=409,
            )
        case "not_found":
            return JSONResponse({"status": "not_found", "detail": result.detail}, status_code=404)
        case _:
            return JSONResponse({"status": "invalid", "detail": result.detail}, status_code=422)


def _record_decision(settings: Settings, body: DecisionRequest) -> DecisionResult:
    with connect_autocommit(settings.database_url) as conn:
        return decide(
            conn,
            body.message_id,
            action=body.action,
            revision=body.revision,
            correction=body.correction,
            via="web",
        )


@router.post("/push-subscriptions", status_code=204)
async def post_subscription(
    request: Request, authorization: str | None = Header(default=None)
) -> Response:
    settings = get_settings()
    _verify(settings, authorization)
    subscription = await _body(request, Subscription)
    await run_in_threadpool(_store_subscription, settings, subscription)
    return Response(status_code=204)


@router.delete("/push-subscriptions", status_code=204)
async def delete_subscription(
    request: Request, authorization: str | None = Header(default=None)
) -> Response:
    settings = get_settings()
    _verify(settings, authorization)
    unsubscribe = await _body(request, Unsubscribe)
    await run_in_threadpool(_remove_subscription, settings, unsubscribe)
    return Response(status_code=204)


def _store_subscription(settings: Settings, subscription: Subscription) -> None:
    """Store once per endpoint. The app re-posts on every open, so a repeat
    refreshes the keys and `last_seen_at` rather than adding a row."""
    with connect_autocommit(settings.database_url) as conn:
        conn.execute(
            """
            INSERT INTO push_subscriptions (endpoint, p256dh, auth)
            VALUES (%s, %s, %s)
            ON CONFLICT (endpoint) DO UPDATE
               SET p256dh = EXCLUDED.p256dh,
                   auth = EXCLUDED.auth,
                   last_seen_at = now()
            """,
            (subscription.endpoint, subscription.keys.p256dh, subscription.keys.auth),
        )


def _remove_subscription(settings: Settings, unsubscribe: Unsubscribe) -> None:
    with connect_autocommit(settings.database_url) as conn:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = %s", (unsubscribe.endpoint,))
