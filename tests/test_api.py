"""Webhook authentication and health reporting.

The webhook is the one route reachable from the open internet, and it can cause
a calendar write. Its authentication is worth more tests than its happy path.
"""

from __future__ import annotations

import base64
import threading
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api import router
from app.bootstrap import SecretDecodeError, materialise_secrets
from app.config import Settings
from app.google.tokens import TokenMetadata
from app.obs.liveness import Liveness, MailSyncLiveness

SECRET = "s3cret-token"
OWNER = "web-api-secret"
"""`WEB_API_SECRET`: health details beyond the status need it."""


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "database_url": "postgresql://localhost/test",
        "gemini_api_key": "test-key",
        "telegram_webhook_secret": SecretStr(SECRET),
        "telegram_bot_token": SecretStr("123:abc"),
        "allowed_chat_ids": [4242],
    }
    return Settings(**(base | overrides))


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A bare app with just the router -- no lifespan, so no scheduler starts."""
    monkeypatch.setattr("app.api.get_settings", _settings)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _owner() -> dict[str, str]:
    return {"Authorization": f"Bearer {OWNER}"}


def _post(client: TestClient, secret: str | None, body: Any = None) -> Any:
    headers = {} if secret is None else {"X-Telegram-Bot-Api-Secret-Token": secret}
    return client.post("/telegram/webhook", json=body or {"update_id": 1}, headers=headers)


# --- webhook authentication ------------------------------------------------


def test_missing_secret_header_is_rejected(client: TestClient) -> None:
    assert _post(client, None).status_code == 403


def test_wrong_secret_is_rejected(client: TestClient) -> None:
    assert _post(client, "not-the-secret").status_code == 403


def test_a_secret_header_outside_ascii_is_a_403_not_a_crash(client: TestClient) -> None:
    """Starlette decodes headers as latin-1; comparing str with a non-ASCII
    character raised TypeError, a 500 and a logged traceback."""
    response = client.post(
        "/telegram/webhook",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "sécret".encode("latin-1")},
    )
    assert response.status_code == 403


def test_empty_secret_header_is_rejected(client: TestClient) -> None:
    """Regression: this returned 200 in a live run.

    A blank `TELEGRAM_WEBHOOK_SECRET=` in .env became `SecretStr("")`, which is
    not None -- so the route read it as configured and then matched an empty
    header, authenticating anyone who sent one.
    """
    assert _post(client, "").status_code == 403


def test_blank_configured_secret_is_treated_as_unset() -> None:
    """The fix, at the settings layer: whitespace is not a secret."""
    assert _settings(telegram_webhook_secret="   ").telegram_webhook_secret is None
    assert _settings(telegram_bot_token="").telegram_bot_token is None


def test_a_blank_secret_makes_the_route_refuse_not_allow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.api.get_settings", lambda: _settings(telegram_webhook_secret=""))
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    assert _post(client, "").status_code == 503
    assert _post(client, "anything").status_code == 503


def test_unconfigured_secret_refuses_rather_than_allowing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failing open here would leave the webhook unauthenticated."""
    monkeypatch.setattr("app.api.get_settings", lambda: _settings(telegram_webhook_secret=None))
    app = FastAPI()
    app.include_router(router)

    assert _post(TestClient(app), SECRET).status_code == 503


class _Connection:
    """Stands in for the webhook's database connection."""

    def __enter__(self) -> object:
        return object()

    def __exit__(self, *args: object) -> None:
        return None


def test_valid_secret_reaches_the_handler(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.connect_autocommit", lambda url: _Connection())
    monkeypatch.setattr("app.api.TelegramClient", lambda token: object())
    monkeypatch.setattr(
        "app.api.TelegramHandler",
        lambda **kwargs: type("H", (), {"handle": lambda self, u: "handled"})(),
    )

    response = _post(client, SECRET)

    assert response.status_code == 200
    assert response.json()["detail"] == "handled"


def test_the_webhook_needs_no_graph_and_does_its_database_work_off_the_event_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Since M16 it only records a decision. The worker applies it."""
    offloaded: list[str] = []

    async def _in_threadpool(fn: Any, *args: Any) -> Any:
        offloaded.append(fn.__name__)
        return fn(*args)

    monkeypatch.setattr("app.api.run_in_threadpool", _in_threadpool)
    monkeypatch.setattr("app.api.connect_autocommit", lambda url: _Connection())
    monkeypatch.setattr("app.api.TelegramClient", lambda token: object())
    monkeypatch.setattr(
        "app.api.TelegramHandler",
        lambda **kwargs: type("H", (), {"handle": lambda self, u: "handled"})(),
    )

    assert _post(client, SECRET).status_code == 200
    assert offloaded == ["_handle_telegram"]


def test_unauthorised_chat_gets_200_not_an_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 4xx makes Telegram retry, and retrying a rejected chat is pointless."""
    from app.telegram.handler import NotAllowedError

    def _raise(self: object, update: Any) -> str:
        raise NotAllowedError("nope")

    monkeypatch.setattr("app.api.connect_autocommit", lambda url: _Connection())
    monkeypatch.setattr("app.api.TelegramClient", lambda token: object())
    monkeypatch.setattr(
        "app.api.TelegramHandler", lambda **kwargs: type("H", (), {"handle": _raise})()
    )

    response = _post(client, SECRET)

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"


# --- health ----------------------------------------------------------------


def test_health_reports_dry_run_state(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["dry_run"] is True


def test_an_unreadable_token_is_a_503_that_says_why_without_details(
    client: TestClient,
) -> None:
    """A 500 tells a monitor nothing; a 503 with a reason tells it to page.
    The exception text stays in the server log: it can name paths and hosts."""
    response = client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["problems"] == ["google token unreadable"]
    assert "token_error" not in body


@dataclass(frozen=True)
class _Token:
    days_remaining: float = 5.0
    expired: bool = False
    needs_reauth_soon: bool = False


@dataclass(frozen=True)
class _Store:
    token: _Token

    def health(self) -> _Token:
        return self.token

    def metadata(self) -> TokenMetadata:
        return TokenMetadata(
            issued_at=datetime.now(UTC) - timedelta(days=2), minted_under="testing"
        )


def _scheduler_on(monkeypatch: pytest.MonkeyPatch, booted_ago: timedelta) -> Liveness:
    monkeypatch.setattr("app.api.token_store", lambda settings: _Store(_Token()))
    monkeypatch.setattr(
        "app.api.get_settings",
        lambda: _settings(
            run_scheduler=True, poll_interval_minutes=10, web_api_secret=SecretStr(OWNER)
        ),
    )
    booted_at = datetime.now(UTC) - booted_ago
    live = Liveness(booted_at=booted_at)
    monkeypatch.setattr("app.api.LIVENESS", live)
    # The mail sync caught up just now, unless a test says otherwise.
    mail = MailSyncLiveness(booted_at=booted_at)
    mail.reached_end(datetime.now(UTC))
    monkeypatch.setattr("app.api.MAIL_SYNC", mail)
    return live


def test_a_stalled_poller_is_a_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _scheduler_on(monkeypatch, booted_ago=timedelta(hours=2))

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["problems"] == ["no successful poll in three intervals"]


def test_a_fresh_process_is_healthy_inside_its_boot_grace(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scheduler_on(monkeypatch, booted_ago=timedelta(minutes=1))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_polling_is_not_judged_when_the_scheduler_is_off(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scheduler_on(monkeypatch, booted_ago=timedelta(hours=2))
    monkeypatch.setattr("app.api.get_settings", lambda: _settings(run_scheduler=False))

    assert client.get("/health").status_code == 200


def test_a_decision_open_for_over_an_hour_is_a_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker is stuck, or the queue is. Either way the owner's tap has
    gone nowhere for an hour."""
    live = _scheduler_on(monkeypatch, booted_ago=timedelta(minutes=1))
    live.decisions_checked(datetime.now(UTC) - timedelta(minutes=61))

    response = client.get("/health", headers=_owner())

    assert response.status_code == 503
    assert response.json()["problems"] == ["a decision has been open for over an hour"]
    assert response.json()["oldest_open_decision_seconds"] >= 3660


def test_health_shows_how_many_browsers_would_hear_a_push(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero is visible, not a failure: before the first phone subscribes there
    is simply nobody to push to."""
    live = _scheduler_on(monkeypatch, booted_ago=timedelta(minutes=1))
    live.subscriptions_counted(0)

    response = client.get("/health", headers=_owner())

    assert response.status_code == 200
    assert response.json()["push_subscriptions"] == 0


def test_a_spent_budget_is_not_an_outage(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap doing its job (M17, D5): 200, and with the bearer, the state
    and the month's spend."""
    live = _scheduler_on(monkeypatch, booted_ago=timedelta(minutes=1))
    live.budget_checked("exhausted", Decimal("39.95"))
    live.writes_checked(1)

    response = client.get("/health", headers=_owner())

    assert response.status_code == 200
    body = response.json()
    assert body["budget"] == {"state": "exhausted", "month_spend_usd": "39.95", "cap_usd": 40.0}
    assert (body["unconfirmed_writes"], body["unpriced_models"]) == (1, [])


def _with(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    monkeypatch.setattr(
        "app.api.get_settings",
        lambda: _settings(
            run_scheduler=True,
            poll_interval_minutes=10,
            web_api_secret=SecretStr(OWNER),
            **overrides,
        ),
    )


def test_a_model_in_use_with_no_price_is_a_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate refuses every call to it, so mail processing has stopped."""
    _scheduler_on(monkeypatch, booted_ago=timedelta(minutes=1))
    _with(monkeypatch, extraction_model="gemini-0-unpriced")

    response = client.get("/health", headers=_owner())

    assert response.status_code == 503
    assert response.json()["problems"] == ["a model in use has no price"]
    assert response.json()["unpriced_models"] == ["gemini-0-unpriced"]


def test_the_embedding_model_counts_only_with_search_or_ingestion_on(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scheduler_on(monkeypatch, booted_ago=timedelta(minutes=1))
    unpriced = {"embedding_model": "embedding-0-unpriced"}

    _with(monkeypatch, search_context_enabled=False, ingest_enabled=False, **unpriced)
    assert client.get("/health").status_code == 200

    _with(monkeypatch, search_context_enabled=False, ingest_enabled=True, **unpriced)
    assert client.get("/health").status_code == 503

    _with(monkeypatch, search_context_enabled=True, ingest_enabled=False, **unpriced)
    assert client.get("/health").status_code == 503


def test_a_stranger_sees_the_status_but_not_the_details(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whether a proposal is waiting, or whether any phone would hear a push,
    is the owner's business. The platform and the uptime monitor need only
    the status code."""
    live = _scheduler_on(monkeypatch, booted_ago=timedelta(minutes=1))
    live.subscriptions_counted(2)
    live.decisions_checked(datetime.now(UTC) - timedelta(seconds=40))

    for headers in (
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Bearer é".encode("latin-1")},
    ):
        body = client.get("/health", headers=headers).json()
        assert "push_subscriptions" not in body
        assert "oldest_open_decision_seconds" not in body
        assert "budget" not in body and "unconfirmed_writes" not in body


@dataclass
class _Scheduler:
    """Stands in for APScheduler: records what boot asks of it."""

    jobs: list[dict[str, Any]] = field(default_factory=list)

    def add_job(self, func: Any, trigger: str, **kwargs: Any) -> None:
        self.jobs.append({"func": func, "trigger": trigger, **kwargs})

    def shutdown(self, wait: bool = True) -> None:
        pass


def _boot(monkeypatch: pytest.MonkeyPatch, settle: Any) -> tuple[list[str], _Scheduler]:
    """Boot the app with the scheduler on and its parts stood in for. Returns
    what happened, in order, and the scheduler."""
    happened: list[str] = []
    scheduler = _Scheduler()
    monkeypatch.setattr(
        "app.api.get_settings", lambda: _settings(run_scheduler=True, migrate_on_boot=False)
    )
    # A real one would leave its handler on the `app` logger for later tests.
    monkeypatch.setattr("app.api.configure_logging", lambda: None)
    monkeypatch.setattr("app.api.materialise_secrets", list)
    monkeypatch.setattr("app.api._seed_token_evidence", lambda settings: None)
    monkeypatch.setattr("app.api.observe_refreshes", lambda record: None)
    monkeypatch.setattr("app.api._settle_stranded_claims", settle)
    monkeypatch.setattr("app.jobs.scheduler.build_scheduler", lambda settings: scheduler)
    monkeypatch.setattr(
        "app.jobs.scheduler.activate", lambda scheduler: happened.append("scheduler started")
    )
    monkeypatch.setattr("app.jobs.poll.STOPPING", threading.Event())
    return happened, scheduler


def test_boot_settles_old_claims_first_and_the_rest_once_the_guard_has_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claim younger than the guard might belong to a poller still running
    in another process, so boot leaves it to a second pass, which settles
    every claim made before boot -- never one this process made."""
    from app.api import create_app
    from app.mail.feed import STRANDED_AFTER

    passes: list[datetime] = []

    def settle(settings: Settings, *, claimed_before: datetime) -> None:
        passes.append(claimed_before)
        happened.append("claims settled")

    happened, scheduler = _boot(monkeypatch, settle)
    with TestClient(create_app()):
        pass

    assert happened == ["claims settled", "scheduler started"]
    (second,) = [job for job in scheduler.jobs if job["func"] is settle]
    booted_at = second["kwargs"]["claimed_before"]
    assert passes == [booted_at - STRANDED_AFTER]
    assert (second["trigger"], second["run_date"]) == ("date", booted_at + STRANDED_AFTER)
    # However late the scheduler gets to it: APScheduler skips a job over a
    # second late by default, which would leave the claims for the next boot.
    assert second["misfire_grace_time"] is None


def test_boot_completes_when_stranded_claims_cannot_be_settled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable database, or an unreadable checkpoint, used to abort
    boot -- on every restart."""
    from app.api import _settle_stranded_claims

    def unsettled(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the checkpoint will not deserialise")

    monkeypatch.setattr("app.store.db.connect", lambda url: nullcontext(None))
    monkeypatch.setattr(
        "app.graph.checkpointer.postgres_checkpointer", lambda url: nullcontext(None)
    )
    monkeypatch.setattr("app.mail.feed.recover_stranded", unsettled)

    _settle_stranded_claims(_settings(), claimed_before=datetime.now(UTC))  # must not raise


def test_the_api_documents_nothing_to_strangers() -> None:
    from app.api import create_app

    client = TestClient(create_app())

    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def _mail_sync(
    monkeypatch: pytest.MonkeyPatch, booted_ago: timedelta, caught_up_ago: timedelta | None
) -> MailSyncLiveness:
    mail = MailSyncLiveness(booted_at=datetime.now(UTC) - booted_ago)
    if caught_up_ago is not None:
        mail.reached_end(datetime.now(UTC) - caught_up_ago)
    monkeypatch.setattr("app.api.MAIL_SYNC", mail)
    return mail


def test_a_mail_sync_that_has_not_reached_the_end_for_three_intervals_is_a_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dead, or alive but behind a backlog: either way the agent sees nothing,
    and an empty feed would otherwise leave /health green (M20, D6)."""
    live = _scheduler_on(monkeypatch, booted_ago=timedelta(hours=2))
    live.poll_finished(ok=True, at=datetime.now(UTC))
    _mail_sync(monkeypatch, timedelta(hours=2), caught_up_ago=timedelta(minutes=7))

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["problems"] == [
        "no mail sync pass reached the end of history in three intervals"
    ]


def test_a_mail_sync_whose_every_fetch_fails_is_a_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each pass still reaches the end of history, so the check above stays
    green while no message is read."""
    _scheduler_on(monkeypatch, booted_ago=timedelta(minutes=1))
    mail = _mail_sync(monkeypatch, timedelta(minutes=1), caught_up_ago=timedelta(seconds=5))
    mail.fetches_tried(tried=3, failed=3, at=datetime.now(UTC) - timedelta(minutes=7))

    response = client.get("/health", headers=_owner())

    assert response.status_code == 503
    assert response.json()["problems"] == ["every mail sync fetch has failed for three intervals"]
    assert response.json()["mail_sync"]["fetches_failing_since"] is not None


def test_a_fresh_boot_is_not_blamed_for_the_mail_sync(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scheduler_on(monkeypatch, booted_ago=timedelta(minutes=3))
    _mail_sync(monkeypatch, timedelta(minutes=3), caught_up_ago=None)

    assert client.get("/health").status_code == 200


def test_the_owner_sees_the_mail_syncs_records_and_a_stranger_does_not(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scheduler_on(monkeypatch, booted_ago=timedelta(minutes=1))
    mail = _mail_sync(monkeypatch, timedelta(minutes=1), caught_up_ago=timedelta(seconds=5))
    mail.status = {"rows": 40, "queue": {"queued": 1, "unreadable": 0}}
    mail.recall = {"missed": 0}

    owner = client.get("/health", headers=_owner()).json()
    stranger = client.get("/health").json()

    assert owner["mail_sync"]["rows"] == 40
    assert owner["mail_sync"]["last_recall"] == {"missed": 0}
    assert 0 <= owner["mail_sync"]["cursor_age_seconds"] < 60
    assert "mail_sync" not in stranger


def test_the_owner_sees_a_recall_that_failed_last_time(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scheduler_on(monkeypatch, booted_ago=timedelta(minutes=1))
    mail = _mail_sync(monkeypatch, timedelta(minutes=1), caught_up_ago=timedelta(seconds=5))
    mail.recall_failed(at=datetime(2026, 10, 1, 5, 15, tzinfo=UTC), error="SyncBusyError")

    owner = client.get("/health", headers=_owner()).json()

    assert owner["mail_sync"]["last_recall_failure"] == {
        "at": "2026-10-01T05:15:00+00:00",
        "error": "SyncBusyError",
    }


def test_a_recent_open_decision_is_reported_but_healthy(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = _scheduler_on(monkeypatch, booted_ago=timedelta(minutes=1))
    live.decisions_checked(datetime.now(UTC) - timedelta(seconds=40))

    response = client.get("/health", headers=_owner())

    assert response.status_code == 200
    assert 40 <= response.json()["oldest_open_decision_seconds"] < 120


# --- secret materialisation ------------------------------------------------


def test_base64_secrets_are_written_to_disk(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "client_secret.json"
    written = materialise_secrets(
        {
            "GOOGLE_CLIENT_SECRETS_B64": base64.b64encode(b'{"installed":{}}').decode(),
            "GOOGLE_CLIENT_SECRETS_PATH": str(target),
        }
    )

    assert written == [str(target)]
    assert target.read_bytes() == b'{"installed":{}}'


def test_absent_variables_are_skipped_not_defaulted(tmp_path: Path) -> None:
    """Locally the real files already exist; overwriting them with nothing
    would be worse than doing nothing."""
    assert materialise_secrets({}) == []


def test_invalid_base64_fails_loudly() -> None:
    """Booting without credentials means a process that looks healthy and
    silently processes no mail."""
    with pytest.raises(SecretDecodeError, match="not valid base64"):
        materialise_secrets({"GOOGLE_TOKEN_B64": "!!!not base64!!!"})


# --- standby token (M15) ----------------------------------------------------


def _token_stores(
    monkeypatch: pytest.MonkeyPatch, *, primary_rejected: bool, standby: _Token | None
) -> None:
    """Primary minted under production; the evidence says whether Google killed it."""
    from app.google.tokens import RefreshOutcome
    from app.obs.liveness import TokenEvidence

    issued = datetime.now(UTC) - timedelta(days=8)
    evidence = TokenEvidence()
    if primary_rejected:
        evidence.record(
            RefreshOutcome(issued_at=issued, at=datetime.now(UTC), ok=False, rejected=True)
        )
    monkeypatch.setattr("app.api.TOKEN_EVIDENCE", evidence)
    monkeypatch.setattr(
        "app.api.token_store",
        lambda settings: _ProductionStore(_Token(days_remaining=-1.0), issued),
    )
    monkeypatch.setattr(
        "app.api.standby_token_store",
        lambda settings: (
            None
            if standby is None
            else _ProductionStore(standby, datetime.now(UTC) - timedelta(days=4))
        ),
    )


@dataclass(frozen=True)
class _ProductionStore:
    token: _Token
    issued_at: datetime

    def health(self) -> _Token:
        return self.token

    def metadata(self) -> TokenMetadata:
        return TokenMetadata(issued_at=self.issued_at, minted_under="production")


def test_a_dead_primary_with_a_live_standby_is_healthy(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _token_stores(monkeypatch, primary_rejected=True, standby=_Token(days_remaining=3.0))

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["token_state"] == "expired"
    assert body["token_in_use"] == "standby"


def test_a_dead_primary_without_a_standby_is_a_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _token_stores(monkeypatch, primary_rejected=True, standby=None)

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["problems"] == ["google token expired"]
