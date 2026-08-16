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
from typing import Final

from cryptography.fernet import Fernet

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

    def save(self, credentials_json: str, *, issued_at: datetime | None = None) -> None:
        """Persist credentials.

        `issued_at` defaults to the existing value, so routine access-token
        refreshes do not silently extend the seven-day window. Pass an explicit
        value only after a real consent flow.
        """
        if issued_at is None:
            issued_at = self._read_issued_at() or datetime.now(UTC)

        payload = json.dumps({"credentials": credentials_json, "issued_at": issued_at.isoformat()})
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_bytes(self._fernet.encrypt(payload.encode()))

    def load(self) -> tuple[str, datetime]:
        """Return `(credentials_json, issued_at)`."""
        if not self._path.exists():
            raise TokenNotFoundError(f"No token at {self._path}. Run: python -m app.google.reauth")
        payload = json.loads(self._fernet.decrypt(self._path.read_bytes()).decode())
        return payload["credentials"], datetime.fromisoformat(payload["issued_at"])

    def health(self, *, now: datetime | None = None) -> TokenHealth:
        _, issued_at = self.load()
        now = now or datetime.now(UTC)
        expires_at = issued_at + TESTING_MODE_REFRESH_LIFETIME
        return TokenHealth(
            issued_at=issued_at,
            expires_at=expires_at,
            days_remaining=(expires_at - now).total_seconds() / 86400,
        )

    def _read_issued_at(self) -> datetime | None:
        if not self._path.exists():
            return None
        return self.load()[1]
