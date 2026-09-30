"""The Google tokens' state, as `/health` reports it and as alerts judge it.

One reading, shared, so an alert can never disagree with the health check
about whether a token is fine. The evidence is passed in rather than read
from a global, so each caller -- and each test -- decides which record of
refreshes it trusts.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.google.tokens import TokenHealth, TokenState, TokenStore, token_state
from app.obs.liveness import TokenEvidence

log = logging.getLogger(__name__)

UNUSABLE_STANDBY = frozenset({None, "expired", "unreadable"})


def token_report(
    store: TokenStore, evidence: TokenEvidence, *, now: datetime | None = None
) -> tuple[TokenState, TokenHealth]:
    metadata = store.metadata()
    seen = evidence.for_token(metadata.issued_at)
    state = token_state(
        metadata,
        now=now or datetime.now(UTC),
        last_ok_refresh_at=seen.last_ok_at,
        rejected=seen.rejected,
    )
    return state, store.health()


def standby_state(store: TokenStore | None, evidence: TokenEvidence) -> str | None:
    """The standby token's state; None when none is configured. Never raises."""
    if store is None:
        return None
    try:
        state, _ = token_report(store, evidence)
    except Exception:
        log.exception("standby token check failed")
        return "unreadable"
    return state
