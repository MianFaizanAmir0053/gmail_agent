"""Find out which models this key can actually call.

    python -m app.extraction.models            # list what the API advertises
    python -m app.extraction.models --probe    # actually try each one

`--probe` exists because listing is not enough. `models.list()` advertises
models the key cannot use: `gemini-2.5-flash-lite` appears in the listing and
then returns `404 ... no longer available to new users` on the first real call.
Model choice on the free tier is also quota-bound rather than capability-bound
-- some of the newest models allow as few as 20 requests per day, which a single
eval run exhausts -- so probing costs a few tokens and saves a wasted quota day.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from typing import Any

CANDIDATES = (
    "gemini-2.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
)


def _client() -> Any:
    """Metered like every other client (M17, D5): the probe's calls count."""
    from app.config import get_settings
    from app.policy import models

    settings = get_settings()
    return models.client(settings, models.local_gate(settings))


def _summarise(exc: Exception) -> str:
    text = str(exc)
    if "no longer available" in text:
        return "RETIRED for new keys"
    if "RESOURCE_EXHAUSTED" in text:
        return "QUOTA exhausted (per-day limit hit)"
    if "UNAVAILABLE" in text:
        return "OVERLOADED (transient)"
    if "NOT_FOUND" in text:
        return "NOT FOUND"
    return text[:90]


def list_models(advertised: Iterable[Any]) -> None:
    rows: list[tuple[str, str]] = []
    for model in advertised:
        name = str(getattr(model, "name", "")).removeprefix("models/")
        actions: Any = getattr(model, "supported_actions", None) or []
        if "generateContent" not in actions:
            continue
        rows.append((name, str(getattr(model, "display_name", ""))))

    for name, display in sorted(rows):
        print(f"  {name:<44} {display}")
    print(f"\n{len(rows)} model(s) advertise generateContent.")
    print("Advertised is not the same as callable -- run with --probe.")


def probe(client: Any) -> None:
    from google.genai import types

    config = types.GenerateContentConfig(max_output_tokens=8)

    for name in CANDIDATES:
        try:
            client.models.generate_content(model=name, contents="Reply with OK.", config=config)
        except Exception as exc:  # the failure text is the result we want
            print(f"  {name:<26} {_summarise(exc)}")
        else:
            print(f"  {name:<26} OK")


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect available Gemini models.")
    parser.add_argument("--probe", action="store_true", help="Make one tiny call per candidate.")
    args = parser.parse_args()

    if args.probe:
        probe(_client())
    else:
        from app.config import get_settings
        from app.policy import models

        list_models(models.advertised(get_settings()))


if __name__ == "__main__":
    main()
