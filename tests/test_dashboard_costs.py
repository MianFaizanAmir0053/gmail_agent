"""The Costs page's totals, run on Postgres from the web app's own query
(`dashboard/src/lib/costs.ts`): the web app's tests run without a database,
so what the SQL counts is checked here."""

from __future__ import annotations

import re
import uuid
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

COSTS = Path(__file__).resolve().parents[1] / "dashboard" / "src" / "lib" / "costs.ts"


def _totals_sql() -> str:
    source = COSTS.read_text("utf-8")
    mail_run = re.search(r'export const MAIL_RUN = "(.*)";', source)
    template = re.search(r"export const TOTALS_SQL = `(.*?)`;", source, re.DOTALL)
    assert mail_run is not None and template is not None, "costs.ts no longer has its two exports"
    return template.group(1).replace("${MAIL_RUN}", mail_run.group(1))


def _run(conn: psycopg.Connection, message_id: str, cost: str) -> None:
    conn.execute(
        "INSERT INTO runs (trace_id, gmail_message_id, status, total_cost_usd)"
        " VALUES (%s, %s, 'success', %s)",
        (uuid.uuid4(), message_id, Decimal(cost)),
    )


@pytest.mark.integration
def test_the_cost_per_email_leaves_chat_out_and_counts_each_email_once(
    conn: psycopg.Connection,
) -> None:
    conn.execute("DELETE FROM runs")  # rolled back with the test
    _run(conn, "msg-a", "0.10")
    _run(conn, "msg-a", "0.30")  # one email, run again: a resume or a redrive
    _run(conn, "msg-b", "0.20")
    _run(conn, "chat-7", "0.05")  # a chat turn: a run, not an email

    row = conn.execute(_totals_sql()).fetchone()
    assert row is not None
    cost, runs, mail_cost, mail_emails = row

    assert (cost, runs) == (Decimal("0.65"), 4)  # the spend counts chat
    assert (mail_cost, mail_emails) == (Decimal("0.60"), 2)
