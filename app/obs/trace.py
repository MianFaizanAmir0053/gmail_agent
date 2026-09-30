"""Recording traces.

Capture happens by wrapping graph nodes rather than by instrumenting each call
site. Hand-instrumenting guarantees one gets missed, and a missing span looks
exactly like a node that was fast and free.

Token usage travels from `structured_call` to the enclosing span through a
`ContextVar`. The alternative -- threading a recorder argument through the
pipeline, the graph, and every node -- would put observability plumbing in the
signature of code that has nothing to do with observability.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg

from app.obs.pricing import cost_usd
from app.obs.redact import redact

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SpanUsage:
    """Token counts accumulated inside one node."""

    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    thinking_tokens: int = 0
    calls: int = 0

    def add(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
        thinking_tokens: int,
    ) -> None:
        # A node making two calls with different models keeps the last; the
        # graph gives classify and extract their own nodes precisely so this
        # stays a one-model-per-span story.
        self.model = model
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cached_tokens += cached_tokens
        self.thinking_tokens += thinking_tokens
        self.calls += 1

    def cost_at(self, at: datetime) -> Decimal | None:
        """Priced at the rate in force at `at`. The tracer passes the span's
        start, so repricing from the stored `started_at` reproduces the figure."""
        return cost_usd(
            self.model,
            at=at,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cached_tokens=self.cached_tokens,
            thinking_tokens=self.thinking_tokens,
        )


_current_usage: ContextVar[SpanUsage | None] = ContextVar("current_span_usage", default=None)


def record_llm_usage(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    thinking_tokens: int,
) -> None:
    """Called by `structured_call`. A no-op when nothing is tracing, so the
    eval harness and unit tests need no observability wiring."""
    usage = _current_usage.get()
    if usage is None:
        return
    usage.add(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        thinking_tokens=thinking_tokens,
    )


@dataclass(slots=True)
class Tracer:
    """Writes runs and spans. One instance per graph run."""

    conn: psycopg.Connection
    trace_id: uuid.UUID = field(default_factory=uuid.uuid4)

    def start_run(self, gmail_message_id: str) -> None:
        self.conn.execute(
            "INSERT INTO runs (trace_id, gmail_message_id, status) VALUES (%s, %s, 'running')"
            " ON CONFLICT (trace_id) DO NOTHING",
            (self.trace_id, gmail_message_id),
        )

    def finish_run(self, status: str, error: str | None = None) -> None:
        self.conn.execute(
            """
            UPDATE runs
               SET status = %s,
                   error = %s,
                   ended_at = now(),
                   duration_ms = EXTRACT(EPOCH FROM (now() - started_at)) * 1000,
                   total_cost_usd = (
                       SELECT SUM(cost_usd) FROM spans WHERE spans.trace_id = runs.trace_id
                   )
             WHERE trace_id = %s
            """,
            (status, error, self.trace_id),
        )

    @contextmanager
    def span(self, node: str, payload: Any = None) -> Iterator[SpanUsage]:
        usage = SpanUsage()
        token = _current_usage.set(usage)
        started = datetime.now(UTC)
        clock = time.perf_counter()
        status, error = "ok", None

        try:
            yield usage
        except Exception as exc:
            status, error = "error", f"{type(exc).__name__}: {exc}"
            raise
        finally:
            _current_usage.reset(token)
            try:
                self._write_span(
                    node=node,
                    payload=payload,
                    usage=usage,
                    status=status,
                    error=error,
                    started=started,
                    latency_ms=int((time.perf_counter() - clock) * 1000),
                )
            except Exception:
                # Observability must never be the reason a run fails.
                log.exception("failed to write span for node %s", node)

    def _write_span(
        self,
        *,
        node: str,
        payload: Any,
        usage: SpanUsage,
        status: str,
        error: str | None,
        started: datetime,
        latency_ms: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO spans (
                trace_id, node, model, status, started_at, latency_ms,
                input_redacted, input_tokens, output_tokens, cached_tokens,
                thinking_tokens, cost_usd, error
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                self.trace_id,
                node,
                usage.model,
                status,
                started,
                latency_ms,
                json.dumps(redact(payload)) if payload is not None else None,
                usage.input_tokens,
                usage.output_tokens,
                usage.cached_tokens,
                usage.thinking_tokens,
                usage.cost_at(started),
                error,
            ),
        )


def traced(tracer: Tracer | None, node: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a graph node so it records a span.

    `tracer=None` returns the function untouched, so the graph is still usable
    without a database -- which is what every graph unit test relies on.
    """
    if tracer is None:
        return fn

    def wrapped(state: Any) -> Any:
        with tracer.span(node, payload={"message_id": state.get("message_id")}):
            return fn(state)

    wrapped.__name__ = getattr(fn, "__name__", node)
    return wrapped
