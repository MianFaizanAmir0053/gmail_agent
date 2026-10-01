"""Pairing, the iPhone sign-in fallback (M16, D4).

Off unless `PAIRING_ENABLED` is set. A 6-digit code stands in for Google
sign-in on one device, so these are the rules that keep it from being a
guessable password: five minutes, five attempts, once, one live code at a
time, and only its SHA-256 stored.
"""

from __future__ import annotations

import hashlib
import re
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import psycopg
import pytest
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

from app.channel.pairing import CODE_TTL, MAX_ATTEMPTS, IssuedCode, issue_code, redeem

NO_DATABASE = cast(psycopg.Connection, object())
"""Any attribute access fails: proves a malformed code is refused before Postgres."""

MALFORMED = [
    "",
    "12345",
    "1234567",
    "12345a",
    " 123456",
    "123456 ",
    "123456\n",
    "12 456",
    "-12345",
    # Digits from other scripts, which `str.isdigit()` and `\d` accept:
    "".join(chr(0x0661 + n) for n in range(6)),  # Arabic-Indic 1 to 6
    "".join(chr(0xFF11 + n) for n in range(6)),  # fullwidth 1 to 6
]

LIVE = "redeemed_at IS NULL AND expires_at > now() AND attempts < %s"


def _outside_a_transaction() -> psycopg.Connection:
    idle = SimpleNamespace(info=SimpleNamespace(transaction_status=TransactionStatus.IDLE))
    return cast(psycopg.Connection, idle)


def _wrong(code: str) -> str:
    """Another well-formed code: a guess that misses."""
    return f"{(int(code) + 1) % 1_000_000:06d}"


def _attempts(conn: psycopg.Connection) -> list[int]:
    return [row[0] for row in conn.execute("SELECT attempts FROM pairing_codes ORDER BY id")]


def _live(conn: psycopg.Connection, issued_to: str | None = None) -> int:
    row = conn.execute(
        f"SELECT count(*) FROM pairing_codes WHERE {LIVE} AND (%s::text IS NULL OR issued_to = %s)",
        (MAX_ATTEMPTS, issued_to, issued_to),
    ).fetchone()
    assert row is not None
    return int(row[0])


# --- before anything moves -----------------------------------------------------


@pytest.mark.parametrize("code", MALFORMED)
def test_anything_but_six_ascii_digits_is_refused_before_postgres(code: str) -> None:
    assert redeem(NO_DATABASE, code) is False


def test_issuing_outside_a_transaction_is_refused() -> None:
    """Ending the old codes and storing the new one must stand or fall together."""
    with pytest.raises(RuntimeError, match="transaction"):
        issue_code(_outside_a_transaction(), issued_to="web")


def test_redeeming_outside_a_transaction_is_refused() -> None:
    """Outside one, the row lock ends before the attempt is counted, and two
    redeems of the right code could both succeed."""
    with pytest.raises(RuntimeError, match="transaction"):
        redeem(_outside_a_transaction(), "123456")


def test_the_code_stays_out_of_reprs() -> None:
    """Reprs reach logs and tracebacks; the code is a password for five minutes."""
    issued = IssuedCode(code="123456", expires_at=datetime(2026, 10, 1, 12, 5, tzinfo=UTC))
    assert "123456" not in repr(issued)


# --- issuing and redeeming (Postgres) -----------------------------------------


@pytest.fixture
def db(conn: psycopg.Connection) -> psycopg.Connection:
    """The test's connection, with no pairing codes but its own. The delete is
    rolled back with the test, like everything else on `conn`."""
    conn.execute("DELETE FROM pairing_codes")
    return conn


@pytest.mark.integration
def test_a_code_works_once(db: psycopg.Connection) -> None:
    issued = issue_code(db, issued_to="web")

    assert redeem(db, issued.code) is True
    assert redeem(db, issued.code) is False


@pytest.mark.integration
def test_a_code_is_six_digits_and_lasts_five_minutes(db: psycopg.Connection) -> None:
    issued = issue_code(db, issued_to="web")

    assert re.fullmatch(r"[0-9]{6}", issued.code)
    row = db.execute("SELECT expires_at - created_at, expires_at FROM pairing_codes").fetchone()
    assert row == (CODE_TTL, issued.expires_at)


@pytest.mark.integration
def test_a_code_keeps_its_leading_zeros(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.channel.pairing.secrets.randbelow", lambda bound: 42)

    issued = issue_code(db, issued_to="web")

    assert issued.code == "000042"
    assert redeem(db, "000042") is True


@pytest.mark.integration
def test_a_wrong_guess_spends_an_attempt(db: psycopg.Connection) -> None:
    issued = issue_code(db, issued_to="web")

    assert redeem(db, _wrong(issued.code)) is False
    assert _attempts(db) == [1]
    assert redeem(db, issued.code) is True
    assert _attempts(db) == [2]


@pytest.mark.integration
def test_the_fifth_attempt_still_counts(db: psycopg.Connection) -> None:
    issued = issue_code(db, issued_to="web")
    for _ in range(MAX_ATTEMPTS - 1):
        assert redeem(db, _wrong(issued.code)) is False

    assert redeem(db, issued.code) is True


@pytest.mark.integration
def test_after_five_wrong_guesses_even_the_right_code_fails(db: psycopg.Connection) -> None:
    issued = issue_code(db, issued_to="web")
    for _ in range(MAX_ATTEMPTS):
        assert redeem(db, _wrong(issued.code)) is False

    assert redeem(db, issued.code) is False
    assert _attempts(db) == [MAX_ATTEMPTS]  # a dead code is not even tried


@pytest.mark.integration
def test_an_expired_code_fails(db: psycopg.Connection) -> None:
    issued = issue_code(db, issued_to="web")
    db.execute("UPDATE pairing_codes SET expires_at = now() - interval '1 second'")

    assert redeem(db, issued.code) is False


@pytest.mark.integration
def test_a_new_code_ends_the_old_one(db: psycopg.Connection) -> None:
    first = issue_code(db, issued_to="web")
    second = issue_code(db, issued_to="web")

    assert _live(db) == 1
    assert redeem(db, first.code) is False
    assert redeem(db, second.code) is True


@pytest.mark.integration
def test_a_code_that_matches_a_dead_one_is_drawn_again(
    db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hash is unique, and an ended code stays until the purge deletes it."""
    draws = iter([111111, 111111, 222222])
    monkeypatch.setattr("app.channel.pairing.secrets.randbelow", lambda bound: next(draws))

    first = issue_code(db, issued_to="web")
    second = issue_code(db, issued_to="web")

    assert (first.code, second.code) == ("111111", "222222")
    assert redeem(db, "222222") is True


@pytest.mark.integration
def test_a_malformed_code_spends_no_attempt(db: psycopg.Connection) -> None:
    issued = issue_code(db, issued_to="web")

    assert redeem(db, issued.code + "0") is False
    assert _attempts(db) == [0]


@pytest.mark.integration
def test_with_no_live_code_nothing_is_redeemed(db: psycopg.Connection) -> None:
    assert redeem(db, "123456") is False


@pytest.mark.integration
def test_only_the_codes_sha256_is_stored(db: psycopg.Connection) -> None:
    issued = issue_code(db, issued_to="web")

    with db.cursor(row_factory=dict_row) as cur:
        stored: dict[str, Any] | None = cur.execute("SELECT * FROM pairing_codes").fetchone()
    assert stored is not None
    assert stored["code_sha256"] == hashlib.sha256(issued.code.encode()).hexdigest()
    assert stored["issued_to"] == "web"
    assert issued.code not in [value for value in stored.values() if isinstance(value, str)]


# --- at the same moment (real concurrency) ------------------------------------


@pytest.fixture
def committed_codes(migrated_database: str) -> Iterator[str]:
    """A label for pairing codes committed for real, so two connections can
    race on them. They are deleted afterwards."""
    label = f"race-{uuid.uuid4().hex[:12]}"
    yield label
    with psycopg.connect(migrated_database, autocommit=True) as cleanup:
        cleanup.execute("DELETE FROM pairing_codes WHERE issued_to = %s", (label,))


def _at_once(work: Callable[[], None]) -> list[Exception]:
    """Run `work` on two threads released together. Returns what they raised."""
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def run() -> None:
        try:
            barrier.wait()
            work()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return errors


@pytest.mark.integration
def test_two_redeems_of_the_right_code_at_once_one_wins(
    migrated_database: str, committed_codes: str
) -> None:
    with psycopg.connect(migrated_database, autocommit=True) as setup, setup.transaction():
        issued = issue_code(setup, issued_to=committed_codes)
    results: list[bool] = []

    def attempt() -> None:
        with psycopg.connect(migrated_database, autocommit=True) as own, own.transaction():
            results.append(redeem(own, issued.code))

    assert _at_once(attempt) == []
    assert sorted(results) == [False, True]


@pytest.mark.integration
def test_two_codes_asked_for_at_once_leave_one_live(
    migrated_database: str, committed_codes: str
) -> None:
    """A double tap on "Show a pairing code" must not leave two live codes."""

    def ask() -> None:
        with psycopg.connect(migrated_database, autocommit=True) as own, own.transaction():
            issue_code(own, issued_to=committed_codes)

    assert _at_once(ask) == []
    with psycopg.connect(migrated_database) as check:
        assert _live(check, committed_codes) == 1
