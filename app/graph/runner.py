"""Wiring and invocation.

Separated from `build.py` so the graph can be constructed with fakes in tests
without any of this touching Google, Gemini, or Postgres.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import psycopg
from langgraph.types import Command

from app.config import Settings
from app.google.auth import build_service, load_credentials
from app.google.calendar import CalendarClient
from app.google.gmail import GmailClient
from app.graph.build import build_graph
from app.graph.checkpointer import postgres_checkpointer
from app.graph.nodes import Deps
from app.obs.trace import Tracer
from app.store.ledger import MessageLedger


@dataclass(slots=True)
class GraphSession:
    deps: Deps
    conn: psycopg.Connection
    checkpointer: Any
    trace: bool = True

    def config(self, message_id: str) -> dict[str, Any]:
        """Thread config for one message.

        `thread_id` is the Gmail message ID, so a checkpoint, a ledger row, and
        a Telegram callback all key off the same value.
        """
        return {"configurable": {"thread_id": message_id}}

    def _graph(self, tracer: Tracer | None = None) -> Any:
        """Built per call so each invocation gets its own trace_id.

        Construction is pure -- no I/O, no connections -- so this is cheap, and
        it avoids a single long-lived graph pinned to one stale tracer.
        """
        return build_graph(self.deps, self.checkpointer, tracer)

    def _run(self, message_id: str, payload: Any) -> dict[str, Any]:
        tracer = Tracer(self.conn) if self.trace else None
        if tracer is not None:
            tracer.start_run(message_id)

        try:
            result: dict[str, Any] = self._graph(tracer).invoke(payload, self.config(message_id))
        except Exception as exc:
            if tracer is not None:
                tracer.finish_run("failed", f"{type(exc).__name__}: {exc}")
            raise

        if tracer is not None:
            # A parked run is not finished. Recording it as success would make
            # the dashboard claim work completed that is still waiting on a human.
            parked = self.pending(message_id) is not None
            tracer.finish_run("awaiting_approval" if parked else "success")
        return result

    def start(self, message_id: str, thread_id: str) -> dict[str, Any]:
        return self._run(message_id, {"message_id": message_id, "thread_id": thread_id})

    def resume(self, message_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        """Continue a thread parked at `await_approval`.

        Nothing of the original run is passed in again -- the checkpoint holds
        it. That is the property the exit criterion tests.
        """
        return self._run(message_id, Command(resume=decision))

    def pending(self, message_id: str) -> dict[str, Any] | None:
        """The interrupt payload for a parked thread, or None if not parked."""
        snapshot = self._graph().get_state(self.config(message_id))
        for task in snapshot.tasks:
            for interrupt_ in task.interrupts:
                value: dict[str, Any] = interrupt_.value
                return value
        return None


@contextmanager
def graph_session(settings: Settings) -> Iterator[GraphSession]:
    """Open everything the graph needs, and close it again."""
    if settings.test_calendar_id is None:
        raise RuntimeError("TEST_CALENDAR_ID must be set before the graph can act.")

    from app.agents.reviewer import build_reviewer
    from app.extraction.pipeline import build_pipeline
    from app.rag.search import build_context_search

    credentials = load_credentials(settings)

    with (
        psycopg.connect(settings.database_url, autocommit=True) as conn,
        postgres_checkpointer(settings.database_url) as checkpointer,
    ):
        # Shares the graph's connection. Retrieval is read-only and runs inside
        # an extraction that is already holding it, so a second pool would buy
        # nothing but another thing to close.
        searcher = build_context_search(conn, settings) if settings.search_context_enabled else None

        calendar = CalendarClient(
            build_service("calendar", "v3", credentials),
            settings.test_calendar_id,
            dry_run=settings.dry_run,
        )

        deps = Deps(
            gmail=GmailClient(build_service("gmail", "v1", credentials)),
            pipeline=build_pipeline(owner_email=settings.owner_email, searcher=searcher),
            calendar=calendar,
            ledger=MessageLedger(conn),
            user_timezone=settings.user_timezone,
            # The reviewer gets the calendar even when DRY_RUN is set: freebusy
            # is a read, and a reviewer that cannot see the calendar loses the
            # one check the extractor genuinely could not make.
            reviewer=(
                build_reviewer(searcher=searcher, calendar=calendar)
                if settings.reviewer_enabled
                else None
            ),
        )
        yield GraphSession(deps=deps, conn=conn, checkpointer=checkpointer)
