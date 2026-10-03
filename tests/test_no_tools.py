"""No model that reads mail holds a tool (M18, D4).

A structural test rather than a data-flow guess:
- the extraction pipeline takes no searcher;
- no call to `structured_call` from `app/extraction/` passes tools or a
  dispatcher, which are keyword-only there, so a positional argument cannot
  carry them;
- the graph's dependencies hold no reviewer.

M19's planner, which will hold tools, lives outside `app/extraction/`.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Iterator
from dataclasses import fields
from pathlib import Path

from app.extraction.llm import structured_call
from app.extraction.pipeline import ExtractionPipeline, build_pipeline
from app.graph.nodes import Deps

EXTRACTION = Path(__file__).resolve().parents[1] / "app" / "extraction"


def _structured_calls() -> Iterator[tuple[str, ast.Call]]:
    for path in sorted(EXTRACTION.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name == "structured_call":
                yield path.name, node


def test_the_extraction_pipeline_takes_no_searcher() -> None:
    assert "searcher" not in {field.name for field in fields(ExtractionPipeline)}
    assert "searcher" not in inspect.signature(build_pipeline).parameters


def test_tools_and_a_dispatcher_can_only_be_passed_by_name() -> None:
    parameters = inspect.signature(structured_call).parameters
    for name in ("tools", "dispatch"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY


def test_extraction_calls_structured_call() -> None:
    """Keeps the next test from passing because it found nothing to check."""
    assert len(list(_structured_calls())) >= 2


def test_no_call_from_extraction_passes_tools_or_a_dispatcher() -> None:
    for filename, call in _structured_calls():
        names = {keyword.arg for keyword in call.keywords}
        where = f"{filename}:{call.lineno}"
        assert not names & {"tools", "dispatch"}, where
        assert None not in names, f"{where} passes **kwargs, which could carry tools"


def test_the_graph_holds_no_reviewer() -> None:
    assert "reviewer" not in {field.name for field in fields(Deps)}
