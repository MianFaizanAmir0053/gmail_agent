"""Gmail `users.messages.get` responses built from `data/injection/` cases.

The cases and their responses live in `app/eval/cases.py`, which the model run
reads too; the tests import them from here, as they always have.
"""

from __future__ import annotations

from app.eval.cases import FIXTURES_DIR, gmail_response, load_cases

__all__ = ["FIXTURES_DIR", "gmail_response", "load_cases"]
