"""No model holds a tool (M18, D4; M19, D13).

A structural test rather than a data-flow guess:
- `structured_call` takes no tools and no dispatcher at all, so no caller can
  hand a model a tool, and no tool's result can go back to a model;
- every call to it, from extraction and from the planner, passes no
  `**kwargs` that could carry them;
- the extraction pipeline takes no searcher;
- the graph's dependencies hold no reviewer.

M18 kept the tool loop "for M19"; M19's planner plans once and never sees a
result, so the loop went with it.
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

APP = Path(__file__).resolve().parents[1] / "app"
MODEL_CALLERS = (APP / "extraction", APP / "planner")
"""The packages whose code calls a model."""


def _structured_calls() -> Iterator[tuple[str, ast.Call]]:
    for package in MODEL_CALLERS:
        for path in sorted(package.glob("*.py")):
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
                    yield f"{package.name}/{path.name}", node


def test_structured_call_takes_no_tools() -> None:
    parameters = inspect.signature(structured_call).parameters
    assert not {"tools", "dispatch", "max_tool_turns"} & set(parameters)


def test_the_search_tool_is_gone() -> None:
    assert not (APP / "tools" / "search_context.py").exists()


def test_the_extraction_pipeline_takes_no_searcher() -> None:
    assert "searcher" not in {field.name for field in fields(ExtractionPipeline)}
    assert "searcher" not in inspect.signature(build_pipeline).parameters


def test_model_callers_call_structured_call() -> None:
    """Keeps the next test from passing because it found nothing to check.
    The planner and the reader that reads mail for it are among them (M19, D6)."""
    callers = {where for where, _ in _structured_calls()}
    assert {"planner/plan.py", "planner/reader.py"} <= callers
    assert len(list(_structured_calls())) >= 4


def test_no_call_passes_keyword_arguments_it_could_hide_tools_in() -> None:
    for where, call in _structured_calls():
        names = {keyword.arg for keyword in call.keywords}
        assert None not in names, f"{where}:{call.lineno} passes **kwargs"


def test_the_graph_holds_no_reviewer() -> None:
    assert "reviewer" not in {field.name for field in fields(Deps)}
