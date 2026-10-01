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
from app.graph.versioning import pipeline_version
from app.obs.trace import Tracer
from app.policy.hashing import Binding, args_key
from app.policy.registry import Registry
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
            # Each step's checkpoint is written before the next step runs.
            # LangGraph's default writes them in the background, and a crash
            # inside `act` could then lose the step that recorded the approval:
            # the thread would look parked, and be resumed a second time.
            result: dict[str, Any] = self._graph(tracer).invoke(
                payload, self.config(message_id), durability="sync"
            )
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

    def redrive(self, message_id: str) -> dict[str, Any]:
        """Run a thread on from where it stopped, with no new input.

        For a thread that consumed a decision and then failed mid-graph: its
        checkpoint still names the node that failed, and this runs it again.
        A thread whose next node is `act` is re-driven only with an approval
        in its state: the registry then finishes the write from what it
        stored, rather than writing again (M17, D3).
        """
        return self._run(message_id, None)

    def binding(self) -> Binding:
        """What this session binds proposals to (M17, D2): the calendar its
        actions write to, and the key of the hash."""
        return Binding(calendar_id=self.deps.calendar.calendar_id, key=self.deps.args_key)

    def pending(self, message_id: str) -> dict[str, Any] | None:
        """The interrupt payload for a parked thread, or None if not parked."""
        return self.thread(message_id).payload

    def revision(self, message_id: str) -> int:
        """Which version of the proposal the thread is on: 1 until an edit."""
        return self.thread(message_id).revision

    def thread(self, message_id: str) -> ThreadView:
        """What the checkpoint says about a thread, read once."""
        snapshot = self._graph().get_state(self.config(message_id))
        payload: dict[str, Any] | None = next(
            (interrupt_.value for task in snapshot.tasks for interrupt_ in task.interrupts),
            None,
        )
        return ThreadView(
            payload=payload,
            # The graph's own edit counter, not the interrupt payload: proposals
            # parked before the payload carried anything extra get the same answer.
            revision=int(snapshot.values.get("revisions", 0)) + 1,
            next=tuple(snapshot.next),
        )


@dataclass(frozen=True, slots=True)
class ThreadView:
    payload: dict[str, Any] | None
    """The interrupt payload while parked at `await_approval`; otherwise None."""

    revision: int
    """1 until an edit, then one more per edit."""

    next: tuple[str, ...]
    """The nodes the checkpoint would run next. A node that failed stays here,
    which is how a thread stopped mid-graph is told apart from a finished one."""

    @property
    def parked(self) -> bool:
        return self.payload is not None


@contextmanager
def graph_session(settings: Settings) -> Iterator[GraphSession]:
    """Open everything the graph needs, and close it again."""
    if settings.test_calendar_id is None:
        raise RuntimeError("TEST_CALENDAR_ID must be set before the graph can act.")
    if settings.fernet_key is None:
        raise RuntimeError("FERNET_KEY must be set: it keys the hash every approval binds (M17).")

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
        key = args_key(settings.fernet_key.get_secret_value())

        deps = Deps(
            gmail=GmailClient(build_service("gmail", "v1", credentials)),
            pipeline=build_pipeline(
                owner_email=settings.owner_email,
                searcher=searcher,
                owner_aliases=tuple(settings.owner_aliases),
            ),
            calendar=calendar,
            ledger=MessageLedger(conn),
            user_timezone=settings.user_timezone,
            registry=Registry(conn, calendar, key=key),
            # The reviewer gets the calendar even when DRY_RUN is set: freebusy
            # is a read, and a reviewer that cannot see the calendar loses the
            # one check the extractor genuinely could not make.
            reviewer=(
                build_reviewer(searcher=searcher, calendar=calendar)
                if settings.reviewer_enabled
                else None
            ),
            pipeline_version=pipeline_version(settings),
            args_key=key,
        )
        yield GraphSession(deps=deps, conn=conn, checkpointer=checkpointer)
