"""Rendering and persistence of eval results."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.eval.scorer import EvalReport

RESULTS_DIR = Path("results")


def _pct(value: float) -> str:
    return f"{value * 100:5.1f}%"


def render(report: EvalReport, *, extractor: str) -> str:
    lines: list[str] = []
    clf = report.is_meeting

    lines.append(f"extractor: {extractor}    fixtures: {report.total}")
    lines.append("")
    lines.append("is_meeting")
    lines.append(
        f"  accuracy {_pct(clf.accuracy)}   precision {_pct(clf.precision)}"
        f"   recall {_pct(clf.recall)}   f1 {_pct(clf.f1)}"
    )
    lines.append(f"  tp={clf.tp} fp={clf.fp} fn={clf.fn} tn={clf.tn}")
    lines.append("")

    applicable = report.fields[0].total if report.fields else 0
    lines.append(f"event fields (over {applicable} meeting fixtures)")
    for field in report.fields:
        lines.append(f"  {field.name:<12} {_pct(field.accuracy)}  ({field.correct}/{field.total})")

    att = report.attendees
    lines.append(f"  attendees micro-f1 {_pct(att.f1)}  (tp={att.tp} fp={att.fp} fn={att.fn})")
    lines.append("")
    lines.append(f"HEADLINE exact match: {_pct(report.exact_match)}")

    if report.failures:
        lines.append("")
        lines.append("failures")
        for outcome in report.failures:
            tags = ",".join(outcome.tags)
            lines.append(
                f"  {outcome.fixture_id:<10} [{tags}] wrong: {', '.join(outcome.wrong_fields)}"
            )

    return "\n".join(lines)


def to_dict(
    report: EvalReport, *, extractor: str, errors: list[str] | None = None
) -> dict[str, Any]:
    """Serialise a run, including whether it can be believed.

    `errors` is not decoration. A fixture that raised is scored as "not a
    meeting", and the runner says so loudly on the terminal -- but that warning
    used to live only in the terminal. The saved file looked exactly like a
    legitimate run, so a quota failure could be published into the eval-history
    chart as a genuine accuracy regression. Found by a run that scored 33.3%
    because six of nine fixtures had hit a daily request limit.
    """
    clf = report.is_meeting
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "extractor": extractor,
        "fixtures": report.total,
        "headline_exact_match": report.exact_match,
        "trustworthy": not errors,
        "errors": errors or [],
        "is_meeting": {
            "accuracy": clf.accuracy,
            "precision": clf.precision,
            "recall": clf.recall,
            "f1": clf.f1,
            "tp": clf.tp,
            "fp": clf.fp,
            "fn": clf.fn,
            "tn": clf.tn,
        },
        "fields": {
            f.name: {"accuracy": f.accuracy, "correct": f.correct, "total": f.total}
            for f in report.fields
        },
        "attendees_micro_f1": report.attendees.f1,
        "failures": [
            {"id": o.fixture_id, "tags": o.tags, "wrong_fields": o.wrong_fields}
            for o in report.failures
        ],
    }


def save(
    report: EvalReport,
    *,
    extractor: str,
    directory: Path = RESULTS_DIR,
    errors: list[str] | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    # An untrustworthy run is still saved, and named so the reason is visible
    # from a directory listing. Discarding it would hide that the run happened.
    prefix = "eval" if not errors else "eval-INVALID"
    path = directory / f"{prefix}-{extractor}-{stamp}.json"
    path.write_text(
        json.dumps(to_dict(report, extractor=extractor, errors=errors), indent=2) + "\n",
        encoding="utf-8",
    )
    return path
