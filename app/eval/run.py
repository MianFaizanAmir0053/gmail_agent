"""Run the eval suite.

    python -m app.eval.run --extractor always_no
    python -m app.eval.run --extractor always_no --no-save

M03 registers the real extractor here; until then only the baselines exist.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable

from app.contracts import ExtractionResult
from app.eval import baselines, report
from app.eval.dataset import Extractor, load_fixtures
from app.eval.scorer import score


def _gemini() -> Extractor:
    # Imported lazily: constructing the pipeline needs an API key, and the
    # baselines must stay runnable without one.
    from app.extraction.pipeline import build_pipeline

    return build_pipeline(owner_email="me@example.com")


EXTRACTORS: dict[str, Callable[[], Extractor]] = {
    "always_no": lambda: baselines.always_no,
    "always_yes": lambda: baselines.always_yes,
    "gemini": _gemini,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Score an extractor against the golden set.")
    parser.add_argument("--extractor", default="always_no", choices=sorted(EXTRACTORS))
    parser.add_argument("--no-save", action="store_true", help="print only; write no results file")
    parser.add_argument("--tag", help="score only fixtures carrying this tag")
    args = parser.parse_args()

    fixtures = load_fixtures()
    if args.tag:
        fixtures = [f for f in fixtures if args.tag in f.tags]
    if not fixtures:
        raise SystemExit("No fixtures matched.")

    extractor = EXTRACTORS[args.extractor]()

    predictions: list[ExtractionResult] = []
    errors: list[str] = []
    for fixture in fixtures:
        try:
            predictions.append(
                extractor(
                    fixture.email, now_utc=fixture.now_utc, user_timezone=fixture.user_timezone
                )
            )
        except Exception as exc:  # one bad fixture must not void the whole run
            errors.append(f"{fixture.id}: {type(exc).__name__}: {exc}")
            predictions.append(
                ExtractionResult(is_meeting=False, confidence=0.0, reasoning=f"ERROR: {exc}")
            )

    result = score(fixtures, predictions)
    print(report.render(result, extractor=args.extractor))

    if errors:
        # Loud on purpose. Errors are scored as "not a meeting", which is
        # accidentally *correct* on every non-meeting fixture -- so a quiet
        # failure would inflate the headline instead of depressing it.
        print(f"\n!! {len(errors)} fixture(s) errored; the score above is not trustworthy")
        for line in errors:
            print(f"   {line}")

    if not args.no_save:
        path = report.save(result, extractor=args.extractor)
        print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
