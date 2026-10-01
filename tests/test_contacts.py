"""Confirmed contacts (M17, D4): guests the owner allowed once, kept until removed."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from app.policy import contacts
from app.policy.hashing import args_key, subject_hash

KEY = args_key("test-key")


@pytest.mark.integration
def test_an_allowed_guest_is_confirmed_however_it_is_spelled(conn: psycopg.Connection) -> None:
    conn.execute("DELETE FROM confirmed_contacts")  # rolled back with the test

    contacts.allow(conn, "Sara.Khan+work@googlemail.com", via="web", key=KEY, message_id="m1")

    assert contacts.confirmed(conn, ["sarakhan@gmail.com", "ali@example.com"]) == {
        "sarakhan@gmail.com"
    }


@pytest.mark.integration
def test_allowing_twice_keeps_one_row_and_its_first_record(conn: psycopg.Connection) -> None:
    conn.execute("DELETE FROM confirmed_contacts")

    contacts.allow(conn, "ali@example.com", via="cli", key=KEY)
    contacts.allow(conn, "ALI@example.com", via="web", key=KEY, message_id="m2")

    rows = conn.execute("SELECT address, via FROM confirmed_contacts").fetchall()
    assert rows == [("ali@example.com", "cli")]


@pytest.mark.integration
def test_a_removed_contact_is_no_longer_confirmed(conn: psycopg.Connection) -> None:
    conn.execute("DELETE FROM confirmed_contacts")
    contacts.allow(conn, "ali@example.com", via="web", key=KEY)

    assert contacts.remove(conn, "Ali@Example.com", key=KEY) is True
    assert contacts.remove(conn, "ali@example.com", key=KEY) is False

    assert contacts.confirmed(conn, ["ali@example.com"]) == frozenset()


@pytest.mark.integration
def test_the_audit_log_links_a_contact_without_naming_it(conn: psycopg.Connection) -> None:
    """A keyed hash of the address, never the address (D7)."""
    conn.execute("DELETE FROM confirmed_contacts")

    contacts.allow(conn, "ali@example.com", via="web", key=KEY, message_id="m1")
    contacts.remove(conn, "ali@example.com", key=KEY)

    rows = conn.execute(
        "SELECT kind, subject_hash, message_id FROM audit_log"
        " WHERE kind IN ('contact_allowed', 'contact_removed') ORDER BY id DESC LIMIT 2"
    ).fetchall()
    digest = subject_hash(KEY, "ali@example.com")
    assert rows == [("contact_removed", digest, None), ("contact_allowed", digest, "m1")]
    assert "ali" not in digest


def test_an_address_that_is_not_one_is_refused() -> None:
    with pytest.raises(ValueError, match="address"):
        contacts.allow(None, "not an address", via="web", key=KEY)  # type: ignore[arg-type]


class _Gmail:
    """Message m1 is filed in thread `thread-1`; a message it does not know
    is gone, and `down` makes every call fail."""

    def __init__(self, thread: dict[str, object], *, down: bool = False) -> None:
        self.thread = thread
        self.down = down
        self.read: list[str] = []

    def message_metadata(self, message_id: str) -> Any:
        from types import SimpleNamespace

        from app.google.gmail import MessageGoneError

        if self.down:
            raise ConnectionError("Gmail unavailable")
        if message_id != "m1":
            raise MessageGoneError(message_id)
        return SimpleNamespace(thread_id="thread-1")

    def thread_headers(self, thread_id: str) -> dict[str, object]:
        self.read.append(thread_id)
        return self.thread


SARA_WAS_WRITTEN_TO: dict[str, object] = {
    "messages": [
        {
            "labelIds": ["SENT"],
            "payload": {"headers": [{"name": "To", "value": "sara@example.com"}]},
        }
    ]
}


@pytest.mark.integration
def test_outsiders_are_read_from_the_thread_and_the_contacts_now(
    conn: psycopg.Connection,
) -> None:
    """Neither in the message's own Gmail thread nor allowed: outside (D4).
    The thread is the one Gmail files the message in, not the ledger's
    `thread_id`, which holds the message id."""
    conn.execute("DELETE FROM confirmed_contacts")
    contacts.allow(conn, "ali@example.org", via="web", key=KEY)
    gmail = _Gmail(SARA_WAS_WRITTEN_TO)

    found = contacts.unconfirmed_outsiders(
        conn, gmail, "m1", ["Sara@example.com", "ali@example.org", "new@example.net"]
    )

    assert found == ["new@example.net"]
    assert gmail.read == ["thread-1"]


# --- the command line ----------------------------------------------------------------


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, object]]]:
    """Runs both commands against a recorder instead of the database."""
    from contextlib import contextmanager
    from types import SimpleNamespace

    from pydantic import SecretStr

    calls: list[tuple[str, str, dict[str, object]]] = []

    @contextmanager
    def no_connection(url: str):  # type: ignore[no-untyped-def]
        yield object()

    def allow(conn: object, address: str, **kwargs: object) -> None:
        if "@" not in address:
            raise ValueError("not an email address")
        calls.append(("allow", address, kwargs))

    def remove(conn: object, address: str, **kwargs: object) -> bool:
        calls.append(("remove", address, kwargs))
        return address != "unknown@example.com"

    settings = SimpleNamespace(
        fernet_key=SecretStr("a-fernet-key-for-tests"), database_url="postgresql://x/y"
    )
    for module in ("app.jobs.approve", "app.jobs.contacts"):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings)
        monkeypatch.setattr(f"{module}.connect_autocommit", no_connection)
        monkeypatch.setattr(f"{module}.contacts.allow", allow)
        monkeypatch.setattr(f"{module}.contacts.remove", remove)
    return calls


def test_approve_allow_records_the_contact_as_the_command_lines(
    cli: list[tuple[str, str, dict[str, object]]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.jobs import approve

    monkeypatch.setattr("sys.argv", ["approve", "--allow", "Sara.Khan@googlemail.com"])

    approve.main()

    [(kind, address, kwargs)] = cli
    assert (kind, address, kwargs["via"]) == ("allow", "Sara.Khan@googlemail.com", "cli")
    assert "Allowed sarakhan@gmail.com." in capsys.readouterr().out


def test_approve_allow_refuses_what_is_not_an_address(
    cli: list[tuple[str, str, dict[str, object]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.jobs import approve

    monkeypatch.setattr("sys.argv", ["approve", "--allow", "nobody"])

    with pytest.raises(SystemExit, match="not an email address"):
        approve.main()


@pytest.mark.parametrize(
    ("address", "said"),
    [
        ("ali@example.com", "Removed ali@example.com."),
        ("unknown@example.com", "unknown@example.com was not a confirmed contact."),
    ],
)
def test_removing_a_contact_says_what_happened(
    cli: list[tuple[str, str, dict[str, object]]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    address: str,
    said: str,
) -> None:
    from app.jobs import contacts as command

    monkeypatch.setattr("sys.argv", ["contacts", "--remove", address])

    command.main()

    assert cli[0][:2] == ("remove", address)
    assert said in capsys.readouterr().out


@pytest.mark.integration
def test_guests_all_allowed_need_no_read_of_gmail(conn: psycopg.Connection) -> None:
    """Gmail being down never holds a Confirm whose guests the owner allowed."""
    conn.execute("DELETE FROM confirmed_contacts")
    contacts.allow(conn, "sara@example.com", via="web", key=KEY)
    gmail = _Gmail(SARA_WAS_WRITTEN_TO, down=True)

    assert contacts.unconfirmed_outsiders(conn, gmail, "m1", ["Sara@Example.com"]) == []


@pytest.mark.integration
def test_a_message_gmail_no_longer_has_leaves_every_guest_not_allowed_outside(
    conn: psycopg.Connection,
) -> None:
    conn.execute("DELETE FROM confirmed_contacts")
    contacts.allow(conn, "ali@example.org", via="web", key=KEY)

    found = contacts.unconfirmed_outsiders(
        conn, _Gmail(SARA_WAS_WRITTEN_TO), "gone", ["sara@example.com", "ali@example.org"]
    )

    assert found == ["sara@example.com"]


@pytest.mark.parametrize("address", ["sára@example.com", "sara@exam ple.com", "\x85a@b.com"])
def test_only_printable_ascii_without_space_is_an_address(address: str) -> None:
    """The web app's `isAddress` refuses the same."""
    with pytest.raises(ValueError, match="address"):
        contacts.allow(None, address, via="web", key=KEY)  # type: ignore[arg-type]
