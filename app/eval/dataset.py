"""Golden fixtures.

Each fixture is a single self-contained JSON file holding the email *and* its
expected extraction. The plan originally split these (`fixtures/*.json` plus a
separate `labels.json`); one file per case removes the ID join, and with it the
chance of an email and its label silently drifting apart.

`now_utc` is the load-bearing field. Relative dates ("tomorrow", "this
Thursday") only have a correct answer relative to some instant, so the instant
is recorded per fixture and handed to the extractor. Without it the suite would
quietly change meaning every day it runs.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from app.contracts import EmailMessage, ExtractionResult

FIXTURE_DIR = Path("data/fixtures")


class Fixture(BaseModel):
    id: str
    tags: list[str] = Field(default_factory=list)
    """Slice labels: `meeting`, `non-meeting`, `relative-date`, `tz-crossing`,
    `all-day`, `reschedule`, `cancellation`, `forwarded`, `ambiguous`."""

    note: str = ""
    """Why this case exists. Read by humans, not by the scorer."""

    now_utc: datetime
    """The instant the extractor is told it is. Grounds relative dates."""

    user_timezone: str
    """The recipient's IANA zone -- what "3pm" means to them."""

    email: EmailMessage
    expected: ExtractionResult

    survives: list[str] = Field(default_factory=list)
    """Text that must come through the production preparation (M18, D9): the
    meeting's time, its link as the scrubber keeps it, a dial-in. Checked
    without a model (`tests/test_fixtures.py`)."""


def prepared(email: EmailMessage) -> EmailMessage:
    """The email as production hands it to a model (M18, D9): scrubbed, or
    set aside as credential mail, by the same preparation the fetch runs."""
    from app.policy.scrub import prepare

    subject, body, credential = prepare(email.subject, email.body_text, flagged=email.credential)
    return email.model_copy(
        update={"subject": subject, "body_text": body, "credential": credential}
    )


class Extractor(Protocol):
    """Anything that can be scored.

    M03's real extractor and the baselines in `app.eval.baselines` both satisfy
    this, so the harness never needs to know which it is running.
    """

    def __call__(
        self, email: EmailMessage, *, now_utc: datetime, user_timezone: str
    ) -> ExtractionResult: ...


def load_fixtures(directory: Path = FIXTURE_DIR) -> list[Fixture]:
    fixtures = [
        Fixture.model_validate_json(p.read_text(encoding="utf-8")) for p in _paths(directory)
    ]
    _assert_unique_ids(fixtures)
    return sorted(fixtures, key=lambda f: f.id)


def _paths(directory: Path) -> Iterator[Path]:
    if not directory.exists():
        raise FileNotFoundError(f"No fixture directory at {directory.resolve()}")
    yield from sorted(directory.glob("*.json"))


def _assert_unique_ids(fixtures: list[Fixture]) -> None:
    seen: set[str] = set()
    for fixture in fixtures:
        if fixture.id in seen:
            raise ValueError(f"Duplicate fixture id: {fixture.id}")
        seen.add(fixture.id)


def write_fixture(fixture: Fixture, directory: Path = FIXTURE_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{fixture.id}.json"
    path.write_text(
        json.dumps(fixture.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path
