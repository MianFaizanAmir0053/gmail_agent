"""Pairing, the iPhone sign-in fallback (M16, D4).

Used only if Google sign-in fails inside the installed iPhone app, and off
unless `PAIRING_ENABLED` is set. The owner, signed in on another device, asks
for a 6-digit code and types it into the installed app's sign-in page. The
web app redeems it here, and on success gives that app a session of its own.

- A code lasts five minutes, and only its SHA-256 is stored.
- At most one code is live: issuing one ends every other.
- A code allows five attempts, right or wrong. After that it is dead, even
  for the right digits.
- A redeemed code never works again.

The hash keeps the code out of the table, not out of reach of someone who can
read it: there are only a million codes to try. What protects a code is its
five minutes and five attempts, and that `web_reader` cannot see the table.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hmac import compare_digest

import psycopg

from app.channel.park import require_transaction

log = logging.getLogger(__name__)

CODE_TTL = timedelta(minutes=5)
MAX_ATTEMPTS = 5

_CODE = re.compile(r"[0-9]{6}")
"""ASCII digits only. `\\d` and `str.isdigit()` accept other scripts' digits."""

_DRAWS = 10
"""A new code that matches an ended one still in the table is drawn again.
With a handful of rows among a million codes, ten misses in a row do not happen."""

_ISSUING = int.from_bytes(b"pairing", "big")
"""The advisory lock issuers take in turn. Without it, two requests at once
each find no live code to end, and both codes stay live. Only issuers take
it: a table lock would deadlock against a redeem holding a code's row."""


@dataclass(frozen=True, slots=True)
class IssuedCode:
    code: str = field(repr=False)
    """Shown to the owner once, and never stored. Kept out of the repr, which
    reaches logs and tracebacks."""

    expires_at: datetime


def issue_code(conn: psycopg.Connection, *, issued_to: str) -> IssuedCode:
    """A new code, live for `CODE_TTL`. Must run inside a transaction.

    Every other live code ends in the same transaction, so at most one is
    live. Ended codes keep their row, attempts and all, until the purge
    deletes them (D8).
    """
    require_transaction(conn)
    # Held until this transaction ends, so a second issuer sees this code.
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_ISSUING,))
    conn.execute("UPDATE pairing_codes SET expires_at = now() WHERE expires_at > now()")
    for _ in range(_DRAWS):
        code = f"{secrets.randbelow(10**6):06d}"
        row = conn.execute(
            """
            INSERT INTO pairing_codes (code_sha256, issued_to, expires_at)
            VALUES (%s, %s, now() + %s)
            ON CONFLICT (code_sha256) DO NOTHING
            RETURNING expires_at
            """,
            (_sha256(code), issued_to, CODE_TTL),
        ).fetchone()
        if row is not None:
            log.info("pairing code issued to %s", issued_to)
            return IssuedCode(code=code, expires_at=row[0])
    raise RuntimeError("could not draw an unused pairing code")


def redeem(conn: psycopg.Connection, code: str) -> bool:
    """Whether `code` is the live code, using it up if so. Must run inside a
    transaction.

    Every attempt on the live code counts, right or wrong. The answer never
    says why a code failed: wrong, expired, spent and absent look the same.
    """
    if not _well_formed(code):
        return False
    require_transaction(conn)
    live = conn.execute(
        """
        SELECT id, code_sha256 FROM pairing_codes
         WHERE redeemed_at IS NULL AND expires_at > now() AND attempts < %s
         ORDER BY created_at DESC, id DESC
         LIMIT 1
           FOR UPDATE
        """,
        (MAX_ATTEMPTS,),
    ).fetchone()
    if live is None:
        log.warning("pairing code refused")
        return False

    code_id, stored = live
    matched = compare_digest(_sha256(code).encode(), str(stored).encode())
    conn.execute(
        """
        UPDATE pairing_codes
           SET attempts = attempts + 1,
               redeemed_at = CASE WHEN %s THEN now() ELSE redeemed_at END
         WHERE id = %s
        """,
        (matched, code_id),
    )
    if matched:
        log.info("pairing code redeemed")
    else:
        log.warning("pairing code refused")
    return matched


def _well_formed(code: object) -> bool:
    return isinstance(code, str) and _CODE.fullmatch(code) is not None


def _sha256(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()
