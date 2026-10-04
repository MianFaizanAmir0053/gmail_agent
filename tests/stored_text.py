"""What a stored error must never carry (M18, D7): a code, a link, and a
sentence of email text, as an exception from a library or a model can carry
them. Every test of stored text seeds the same one."""

from __future__ import annotations

LEAKY = (
    "upstream said your code is 482913, see https://tracker.example/x?id=1\n"
    "Failing row contains (Hi Sara, the offsite moved to Thursday.)"
)
"""A first line with a code and a link, and a later line quoting an email, as
Postgres's failing-row detail and pydantic's `input_value` do."""

LEAKED = ("482913", "tracker.example/x", "offsite")
"""What must not survive from `LEAKY`."""


def assert_clean(text: str | None) -> None:
    """`text` was stored, and carries nothing `LEAKY` seeded."""
    assert text is not None
    for leaked in LEAKED:
        assert leaked not in text, leaked
