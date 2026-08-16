"""PII redaction for stored traces.

Traces exist to debug extraction failures, which means they contain email
content -- other people's addresses, phone numbers, and correspondence. That is
worth keeping *shaped* and not worth keeping *verbatim*: enough structure to see
why a date was misread, not a warehouse of other people's mail.

Deliberately conservative. Over-redacting costs a little debuggability;
under-redacting builds a database that should not exist.
"""

from __future__ import annotations

import re
from typing import Any

MAX_TEXT = 2000
"""Bodies are truncated: a trace is for diagnosis, not archival."""

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\w)(\+?\d[\d\s().-]{7,}\d)(?!\w)")
_LONG_DIGITS = re.compile(r"(?<!\w)\d{9,}(?!\w)")
_URL = re.compile(r"https?://\S+")


def redact_text(value: str) -> str:
    value = _EMAIL.sub("<email>", value)
    value = _URL.sub("<url>", value)
    value = _PHONE.sub("<phone>", value)
    value = _LONG_DIGITS.sub("<number>", value)
    if len(value) > MAX_TEXT:
        value = f"{value[:MAX_TEXT]}... [{len(value) - MAX_TEXT} more chars]"
    return value


def redact(value: Any) -> Any:
    """Recursively redact a JSON-ish structure.

    Dict *keys* are left alone -- they are field names we chose, not content.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value
