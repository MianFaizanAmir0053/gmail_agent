"""OAuth flow and credential loading."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from app.config import Settings
from app.google.scopes import SCOPES
from app.google.tokens import MintedUnder, RefreshOutcome, TokenStore

log = logging.getLogger(__name__)

_refresh_observer: Callable[[RefreshOutcome], None] | None = None


def observe_refreshes(observer: Callable[[RefreshOutcome], None] | None) -> None:
    """Report every access-token refresh to `observer` (M15).

    The web process registers one that records the outcome. The CLI tools,
    the eval harness and the tests register none, so they need no database.
    """
    global _refresh_observer
    _refresh_observer = observer


def _report(outcome: RefreshOutcome) -> None:
    if _refresh_observer is None:
        return
    try:
        _refresh_observer(outcome)
    except Exception:
        # Evidence is worth recording, never worth failing authentication for.
        log.exception("could not record a token refresh")


class GoogleAuthNotConfiguredError(RuntimeError):
    """Required Google settings are missing from the environment."""


def token_store(settings: Settings) -> TokenStore:
    if settings.google_token_path is None or settings.fernet_key is None:
        raise GoogleAuthNotConfiguredError(
            "GOOGLE_TOKEN_PATH and FERNET_KEY must be set. See .env.example."
        )
    return TokenStore(Path(settings.google_token_path), settings.fernet_key.get_secret_value())


def run_consent_flow(settings: Settings, *, minted_under: MintedUnder) -> Credentials:
    """Open a browser, complete consent, and store a fresh token.

    This is the only path that resets the seven-day refresh-token clock.
    `minted_under` is the OAuth app's publishing status right now, and is
    stored with the token as the evidence M15 checks.
    """
    if settings.google_client_secrets_path is None:
        raise GoogleAuthNotConfiguredError(
            "GOOGLE_CLIENT_SECRETS_PATH must be set. See .env.example."
        )

    flow = InstalledAppFlow.from_client_secrets_file(settings.google_client_secrets_path, SCOPES)
    # access_type=offline is what actually yields a refresh token; prompt=consent
    # forces a new one even when Google would otherwise reuse a prior grant.
    credentials = cast(
        Credentials,
        flow.run_local_server(port=0, access_type="offline", prompt="consent"),
    )

    token_store(settings).save(
        credentials.to_json(), issued_at=datetime.now(UTC), minted_under=minted_under
    )
    return credentials


def load_credentials(settings: Settings) -> Credentials:
    """Load stored credentials, refreshing the access token if stale.

    A successful refresh is written back, but `issued_at` is preserved -- the
    refresh token itself is unchanged and its seven-day deadline stands.
    """
    store = token_store(settings)
    credentials_json, issued_at = store.load()
    credentials: Credentials = Credentials.from_authorized_user_info(
        cast(dict[str, Any], json.loads(credentials_json)), SCOPES
    )

    if not credentials.valid and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except RefreshError as exc:
            _report(
                RefreshOutcome(
                    issued_at=issued_at,
                    at=datetime.now(UTC),
                    ok=False,
                    rejected="invalid_grant" in str(exc),
                )
            )
            raise
        store.save(credentials.to_json())
        _report(RefreshOutcome(issued_at=issued_at, at=datetime.now(UTC), ok=True, rejected=False))

    return credentials


def build_service(name: str, version: str, credentials: Credentials) -> Any:
    """Construct a discovery client.

    Returns `Any` deliberately: googleapiclient builds its surface at runtime,
    so there is nothing meaningful to annotate. Callers wrap it in a typed
    client class rather than passing this around.
    """
    return build(name, version, credentials=credentials, cache_discovery=False)
