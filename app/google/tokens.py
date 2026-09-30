"""Encrypted OAuth token storage, and the seven-day clock.

## Why `issued_at` is tracked separately

`Credentials.expiry` is the **access token** expiry -- roughly one hour, refreshed
transparently. It says nothing about the failure mode that actually kills this
app: while the OAuth consent screen is in "Testing" publishing status, Google
invalidates the **refresh token** after seven days, and there is no API that
reports that deadline.

So we record when the refresh token was minted and count forward ourselves. The
clock resets only on a fresh consent flow -- refreshing an access token reuses
the same refresh token and does *not* extend it. `TokenStore.save()` therefore
preserves the original `issued_at` unless a caller explicitly passes a new one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal, cast

from cryptography.fernet import Fernet

MintedUnder = Literal["testing", "production"]
"""The OAuth app's publishing status when a token was minted (M15).

Whether a refresh token lapses after seven days depends on the status at the
moment of consent, not on what the app is set to later. So it is recorded
with the token. A setting would describe the app, not the token, and would
be wrong for every token minted before a change.
"""


@dataclass(frozen=True, slots=True)
class TokenMetadata:
    issued_at: datetime
    minted_under: MintedUnder


TESTING_MODE_REFRESH_LIFETIME: Final = timedelta(days=7)
"""How long a refresh token survives while the app is unverified ("Testing").

Publishing the app as "Internal" (requires Google Workspace) removes this limit
entirely. Full public verification requires a CASA assessment.
"""

REAUTH_WARNING_THRESHOLD: Final = timedelta(days=2)


@dataclass(frozen=True, slots=True)
class TokenHealth:
    issued_at: datetime
    expires_at: datetime
    days_remaining: float

    @property
    def expired(self) -> bool:
        return self.days_remaining <= 0

    @property
    def needs_reauth_soon(self) -> bool:
        return self.days_remaining <= REAUTH_WARNING_THRESHOLD.days


class TokenNotFoundError(RuntimeError):
    """No stored token. Run the consent flow: `python -m app.google.reauth`."""


class TokenStore:
    """Fernet-encrypted credentials on disk.

    The plaintext contains a refresh token that grants read access to the
    user's entire mailbox, so it is never written unencrypted and never
    committed -- `secrets/` and `*.enc` are gitignored.
    """

    def __init__(self, path: Path, fernet_key: str) -> None:
        self._path = path
        self._fernet = Fernet(fernet_key.encode())

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.exists()

    def save(
        self,
        credentials_json: str,
        *,
        issued_at: datetime | None = None,
        minted_under: MintedUnder | None = None,
    ) -> None:
        """Persist credentials, keeping every metadata field already stored.

        `issued_at` and `minted_under` default to the existing values, so the
        hourly access-token refresh neither extends the seven-day window nor
        erases how the token was minted. Pass them only after a real consent
        flow. Fields this version does not know are carried over too, so an
        older process refreshing a token cannot strip a newer one's metadata.
        """
        payload = self._read_payload() or {}
        payload["credentials"] = credentials_json
        if issued_at is not None:
            payload["issued_at"] = issued_at.isoformat()
        payload.setdefault("issued_at", datetime.now(UTC).isoformat())
        if minted_under is not None:
            payload["minted_under"] = minted_under
        # Every token that predates this field was minted in Testing.
        payload.setdefault("minted_under", "testing")

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_bytes(self._fernet.encrypt(json.dumps(payload).encode()))

    def load(self) -> tuple[str, datetime]:
        """Return `(credentials_json, issued_at)`."""
        payload = self._require_payload()
        return payload["credentials"], datetime.fromisoformat(payload["issued_at"])

    def metadata(self) -> TokenMetadata:
        payload = self._require_payload()
        minted_under = payload.get("minted_under")
        return TokenMetadata(
            issued_at=datetime.fromisoformat(payload["issued_at"]),
            # Anything unrecognised is treated as Testing: the cautious reading.
            minted_under="production" if minted_under == "production" else "testing",
        )

    def health(self, *, now: datetime | None = None) -> TokenHealth:
        _, issued_at = self.load()
        now = now or datetime.now(UTC)
        expires_at = issued_at + TESTING_MODE_REFRESH_LIFETIME
        return TokenHealth(
            issued_at=issued_at,
            expires_at=expires_at,
            days_remaining=(expires_at - now).total_seconds() / 86400,
        )

    def _read_payload(self) -> dict[str, Any] | None:
        if not self._path.exists():
            return None
        return cast(dict[str, Any], json.loads(self._fernet.decrypt(self._path.read_bytes())))

    def _require_payload(self) -> dict[str, Any]:
        payload = self._read_payload()
        if payload is None:
            raise TokenNotFoundError(f"No token at {self._path}. Run: python -m app.google.reauth")
        return payload
