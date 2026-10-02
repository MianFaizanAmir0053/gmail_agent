"""Connections (`app/store/db.py`)."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from app.store import db


def test_an_autocommit_connection_gives_up_on_an_unreachable_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pause, Resume and Withdraw connect this way. Without a timeout, a
    database that never answers would hold a request thread for minutes, and
    the command line would hang in an emergency with no output."""
    seen: dict[str, Any] = {}

    def connect(url: str, **kwargs: Any) -> object:
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(psycopg, "connect", connect)

    db.connect_autocommit("postgresql://x/y")

    assert seen == {"autocommit": True, "connect_timeout": db.CONNECT_TIMEOUT}
    assert 0 < db.CONNECT_TIMEOUT <= 10
