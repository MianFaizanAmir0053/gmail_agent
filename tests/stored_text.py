"""What a stored error must never carry (M18, D7): a code, a link, and a
sentence of email text, as an exception from a library or a model can carry
them. Every test of stored text seeds the same one.

The code and the link are put together at runtime, so no source line, and no
tool that prints one, carries a whole code-shaped string.
"""

from __future__ import annotations

_CODE = "".join(str(digit) for digit in (4, 8, 2, 9, 1, 3))
_LINK = "tracker" + ".example" + "/x"

LEAKY = (
    f"upstream said your code is {_CODE}, see https://{_LINK}?id=1\n"
    "Failing row contains (Hi Sara, the offsite moved to Thursday.)"
)
"""A first line with a code and a link, and a later line quoting an email, as
Postgres's failing-row detail and pydantic's `input_value` do."""

LEAKED = (_CODE, _LINK, "offsite")
"""What must not survive from `LEAKY`."""


def assert_clean(text: str | None) -> None:
    """`text` was stored, and carries nothing `LEAKY` seeded."""
    assert text is not None
    for leaked in LEAKED:
        assert leaked not in text, leaked
