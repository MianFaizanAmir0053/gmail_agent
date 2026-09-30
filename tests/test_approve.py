"""The operator's CLI over parked proposals.

The session is faked: what is under test is which proposals get which
decision, not LangGraph's resume, which `test_graph.py` covers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from app.graph.runner import GraphSession
from app.jobs.approve import list_pending, sweep_all


@dataclass
class FakeRows:
    rows: list[tuple[str]]

    def fetchall(self) -> list[tuple[str]]:
        return self.rows


@dataclass
class FakeConn:
    awaiting: list[str]

    def execute(self, sql: str, params: tuple[Any, ...]) -> FakeRows:
        return FakeRows([(message_id,) for message_id in self.awaiting])


@dataclass
class FakeSession:
    awaiting: list[str]
    parked: set[str]
    dry_run: bool = True
    resumed: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    @property
    def conn(self) -> FakeConn:
        return FakeConn(self.awaiting)

    def pending(self, message_id: str) -> dict[str, Any] | None:
        if message_id not in self.parked:
            return None
        return {"proposed": {"title": "Design review"}, "dry_run": self.dry_run}

    def resume(self, message_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        self.resumed.append((message_id, decision))
        return {}


def test_sweep_all_ends_every_parked_proposal_with_a_sweep() -> None:
    session = FakeSession(awaiting=["a", "b"], parked={"a", "b"})

    swept = sweep_all(cast(GraphSession, session))

    assert swept == 2
    assert session.resumed == [("a", {"action": "sweep"}), ("b", {"action": "sweep"})]


def test_list_shows_the_dry_run_each_proposal_was_made_under(
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = FakeSession(awaiting=["a"], parked={"a"}, dry_run=True)

    list_pending(cast(GraphSession, session))

    assert "dry_run when proposed: True" in capsys.readouterr().out


def test_sweep_all_leaves_ledger_rows_with_no_live_checkpoint_alone() -> None:
    """Resuming a thread that is not parked would start a fresh run of it."""
    session = FakeSession(awaiting=["a", "stale"], parked={"a"})

    swept = sweep_all(cast(GraphSession, session))

    assert swept == 1
    assert session.resumed == [("a", {"action": "sweep"})]


def test_reconcile_on_demand_reports_what_it_did(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.channel.reconcile import ReconcileResult
    from app.jobs import approve

    monkeypatch.setattr(
        approve,
        "reconcile",
        lambda session, **kwargs: ReconcileResult(recorded=2, closed=1, errors=0),
    )

    line = approve.reconcile_now(cast(GraphSession, FakeSession(awaiting=[], parked=set())))

    assert line == "Recorded 2 parked thread(s); closed 1 row(s); 0 error(s)."
