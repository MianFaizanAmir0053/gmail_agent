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
    POST   /api/decisions/withdraw   ask for a queued decision to be withdrawn
                                     (202 requested, 409 settled, 404 no such
                                     decision)
    POST   /api/pause                pause the agent (204)
    POST   /api/resume               resume it (204)
    POST   /api/contacts             allow a guest outside the thread (204, 422
                                     not an address)
    POST   /api/push-subscriptions   store or refresh a browser's subscription
    DELETE /api/push-subscriptions   remove one
    POST   /api/pairing/codes        a pairing code for the iPhone fallback (201)
    POST   /api/pairing/redeem       redeem one (204, or 403 whatever the reason)

The pairing routes answer 404 unless `PAIRING_ENABLED` is set, and only after
the secret is checked, so a caller without it cannot tell whether they exist.
"""

from __future__ import annotations

from hmac import compare_digest
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.channel.decide import (
    MAX_CORRECTION_CHARS,
    DecisionResult,
    WithdrawStatus,
    decide,
    request_withdraw,
)
from app.channel.pairing import IssuedCode, issue_code, redeem
from app.config import Settings, get_settings
from app.jobs.scheduler import decision_recorded, wake_decisions
from app.policy import contacts, control
from app.policy.hashing import args_key
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

    token: str | None = Field(default=None, max_length=64)
    """What the owner's card showed: hash prefix, mode and generation (M17,
    D2). A Confirm without a valid one is refused as stale."""


class WithdrawRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: int = Field(ge=1)
    """The queued decision the owner's card showed, not its proposal: a
    request from an old card cannot reach a decision made since."""


class ContactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str = Field(min_length=3, max_length=contacts.MAX_ADDRESS_CHARS)
    message_id: str | None = Field(default=None, max_length=128)
    """The proposal it was allowed from, for the record."""


class SubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=512)
    auth: str = Field(min_length=1, max_length=512)


PUSH_SERVICES = (
    "fcm.googleapis.com",  # Chrome, Android
    "push.apple.com",  # Safari and installed web apps on iPhone
    "push.services.mozilla.com",  # Firefox
    "notify.windows.com",  # Edge
)
"""Hosts Fly may send pushes to. Fly POSTs to whatever endpoint is stored, so
only a real push service is accepted, rather than any https URL."""

MAX_SUBSCRIPTIONS = 10
"""Every push round sends to every row; this bounds the fan-out. Two phones
and a laptop need three."""


def _is_push_service(host: str) -> bool:
    return any(host == known or host.endswith("." + known) for known in PUSH_SERVICES)


class Subscription(BaseModel):
    """A browser's `PushSubscription`, as `JSON.stringify` writes it."""

    endpoint: str = Field(max_length=MAX_ENDPOINT_CHARS)
    keys: SubscriptionKeys

    @field_validator("endpoint")
    @classmethod
    def _a_known_push_service(cls, value: str) -> str:
        try:
            parts = urlsplit(value)
            port = parts.port
        except ValueError:
            raise ValueError("a push endpoint is an https URL") from None
        host = (parts.hostname or "").lower()
        if (
            parts.scheme != "https"
            or any(ch.isspace() for ch in value)
            or parts.username is not None
            or port not in (None, 443)
            or not _is_push_service(host)
        ):
            raise ValueError("a push endpoint is an https URL at a known push service")
        return value


class Unsubscribe(BaseModel):
    endpoint: str = Field(min_length=1, max_length=MAX_ENDPOINT_CHARS)


class PairingCodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issued_to: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:@-]+$")
    """Who asked, as a short label such as `web`. It is logged, so it is kept
    to characters that cannot forge a log line."""


class PairingRedeemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(max_length=64)
    """Any short string. One that is not six digits is refused like a wrong
    code, with a 403, so the answer never says which rule a guess broke."""


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
        case "not_ready":
            return JSONResponse({"status": "not_ready", "detail": result.detail}, status_code=409)
        case "outside":
            return JSONResponse({"status": "outside", "detail": result.detail}, status_code=422)
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
            token=body.token,
            dry_run=settings.dry_run,
        )


@router.post("/decisions/withdraw")
async def post_withdraw(
    request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    """Ask for a queued decision to be withdrawn (M17, D6). The worker
    carries the request out, even while paused, or declines it."""
    settings = get_settings()
    _verify(settings, authorization)
    body = await _body(request, WithdrawRequest)

    status = await run_in_threadpool(_request_withdraw, settings, body.decision_id)

    match status:
        case "requested":
            wake_decisions()
            return JSONResponse({"status": "requested"}, status_code=202)
        case "settled":
            return JSONResponse({"status": "settled"}, status_code=409)
        case _:
            return JSONResponse({"status": "not_found"}, status_code=404)


def _request_withdraw(settings: Settings, decision_id: int) -> WithdrawStatus:
    with connect_autocommit(settings.database_url) as conn:
        return request_withdraw(conn, decision_id)


@router.post("/pause", status_code=204)
async def post_pause(authorization: str | None = Header(default=None)) -> Response:
    """Pause the agent (M17, D6). Pausing a paused agent changes nothing."""
    settings = get_settings()
    _verify(settings, authorization)
    await run_in_threadpool(_switch, settings, True)
    return Response(status_code=204)


@router.post("/resume", status_code=204)
async def post_resume(authorization: str | None = Header(default=None)) -> Response:
    """Resume it. The worker is woken, so a held decision moves on at once."""
    settings = get_settings()
    _verify(settings, authorization)
    await run_in_threadpool(_switch, settings, False)
    wake_decisions()
    return Response(status_code=204)


def _switch(settings: Settings, paused: bool) -> None:
    with connect_autocommit(settings.database_url) as conn:
        control.switch(conn, paused=paused, via="web")


@router.post("/contacts", status_code=204)
async def post_contact(
    request: Request, authorization: str | None = Header(default=None)
) -> Response:
    """Allow a guest outside the thread (M17, D4). Allowing one already
    allowed changes nothing."""
    settings = get_settings()
    _verify(settings, authorization)
    body = await _body(request, ContactRequest)
    if settings.fernet_key is None:
        raise HTTPException(status_code=503, detail="FERNET_KEY is not configured")
    try:
        await run_in_threadpool(_allow_contact, settings, body)
    except ValueError:
        return JSONResponse(
            {"status": "invalid", "detail": "not an email address"}, status_code=422
        )
    return Response(status_code=204)


def _allow_contact(settings: Settings, body: ContactRequest) -> None:
    assert settings.fernet_key is not None
    key = args_key(settings.fernet_key.get_secret_value())
    with connect_autocommit(settings.database_url) as conn:
        contacts.allow(conn, body.address, via="web", key=key, message_id=body.message_id)


@router.post("/push-subscriptions", status_code=204)
async def post_subscription(
    request: Request, authorization: str | None = Header(default=None)
) -> Response:
    settings = get_settings()
    _verify(settings, authorization)
    subscription = await _body(request, Subscription)
    if not await run_in_threadpool(_store_subscription, settings, subscription):
        return JSONResponse(
            {"status": "too_many", "detail": f"at most {MAX_SUBSCRIPTIONS} subscriptions"},
            status_code=409,
        )
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


def _store_subscription(settings: Settings, subscription: Subscription) -> bool:
    """Store once per endpoint. The app re-posts on every open, so a repeat
    refreshes the keys and `last_seen_at` rather than adding a row. A new
    endpoint beyond `MAX_SUBSCRIPTIONS` is refused; a known one never is."""
    with connect_autocommit(settings.database_url) as conn, conn.transaction():
        known = conn.execute(
            "SELECT 1 FROM push_subscriptions WHERE endpoint = %s", (subscription.endpoint,)
        ).fetchone()
        if known is None:
            row = conn.execute("SELECT count(*) FROM push_subscriptions").fetchone()
            if row is not None and row[0] >= MAX_SUBSCRIPTIONS:
                return False
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
    return True


def _remove_subscription(settings: Settings, unsubscribe: Unsubscribe) -> None:
    with connect_autocommit(settings.database_url) as conn:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = %s", (unsubscribe.endpoint,))


def _require_pairing(settings: Settings) -> None:
    """Switched off, the fallback answers as an unknown route does."""
    if not settings.pairing_enabled:
        raise HTTPException(status_code=404)


@router.post("/pairing/codes", status_code=201)
async def post_pairing_code(
    request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    settings = get_settings()
    _verify(settings, authorization)
    _require_pairing(settings)
    body = await _body(request, PairingCodeRequest)

    issued = await run_in_threadpool(_issue_pairing_code, settings, body.issued_to)

    return JSONResponse(
        {"code": issued.code, "expires_at": issued.expires_at.isoformat(timespec="seconds")},
        status_code=201,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/pairing/redeem", status_code=204)
async def post_pairing_redeem(
    request: Request, authorization: str | None = Header(default=None)
) -> Response:
    settings = get_settings()
    _verify(settings, authorization)
    _require_pairing(settings)
    body = await _body(request, PairingRedeemRequest)

    if not await run_in_threadpool(_redeem_pairing_code, settings, body.code):
        return JSONResponse({"status": "refused"}, status_code=403)
    return Response(status_code=204)


def _issue_pairing_code(settings: Settings, issued_to: str) -> IssuedCode:
    with connect_autocommit(settings.database_url) as conn, conn.transaction():
        return issue_code(conn, issued_to=issued_to)


def _redeem_pairing_code(settings: Settings, code: str) -> bool:
    """The attempt is committed whatever the answer: a wrong guess that rolled
    back would cost the guesser nothing."""
    with connect_autocommit(settings.database_url) as conn, conn.transaction():
        return redeem(conn, code)
