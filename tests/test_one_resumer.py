"""D1's rule, enforced rather than remembered: only the worker resumes a thread.

The queue is safe because nothing else resumes or re-drives a thread, so no
recovery path can race a resume that is still running. A new caller of
`resume` or `redrive` anywhere else in `app/` fails this test.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"

ALLOWED = {
    APP / "channel" / "worker.py",  # the one resumer
    APP / "graph" / "runner.py",  # where the methods are defined
}


def _callers(name: str) -> list[str]:
    found: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        if path in ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == name
            ):
                found.append(f"{path.relative_to(APP.parent)}:{node.lineno}")
    return found


def test_only_the_worker_resumes_a_thread() -> None:
    assert _callers("resume") == []


def test_only_the_worker_re_drives_a_thread() -> None:
    assert _callers("redrive") == []
