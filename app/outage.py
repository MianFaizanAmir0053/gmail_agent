"""Whether a failure says anything about the one message it struck.

An outage is a failure every message would meet. The meeting pipeline sends
the message back to the feed for one, rather than dead-letter it
(`app.jobs.poll`; M20's running notes, 2026-10-18), and M21's reads charge
no strike for one. One judge, so the two cannot disagree.
"""

from __future__ import annotations

import http.client

import httpx
from google.genai import errors as genai_errors

from app.extraction.evaluation import GatewayError
from app.extraction.llm import UnavailableError
from app.google.calendar import CalendarUnavailableError
from app.google.gmail import is_outage as google_outage

MODEL_REFUSALS = frozenset({401, 403, 404, 429})
"""Statuses from a model's API that every message would meet: its key,
billing or permission refused (401, 403), the model gone (404), or a quota
the client gave up on at once, a day's included (429)."""

KEY_REFUSED = "API_KEY_INVALID"
"""How Gemini marks the 400 it answers an invalid key with."""

UNSUPPORTED = "FAILED_PRECONDITION"
"""The status of the 400 Gemini answers a project it cannot serve at all
with: a free tier or a location it does not support."""


def is_outage(exc: BaseException) -> bool:
    """A failure every message would meet:
    - a model gave up on its retries (`UnavailableError`), or its network
      failed: httpx's own errors, which the model clients raise as they are;
    - Gemini or AI Gateway refused the key, billing, permission, model or
      quota (`MODEL_REFUSALS`), or Gemini answered a 400 naming the key
      invalid or the project unsupported;
    - the calendar could not be read (`CalendarUnavailableError`), which
      Google can say inside a 200;
    - a response cut off mid-read: http.client's own errors, which httplib2
      lets through on a bad link;
    - Google or the network failed, as `app.google.gmail.is_outage` judges
      it: a 429 or 5xx, a rate-limit 403, a 401 or a refused token, or an
      `OSError`, a local one such as a missing token file included.
    """
    if isinstance(exc, genai_errors.APIError):
        if exc.code in MODEL_REFUSALS:
            return True
        return exc.code == 400 and (exc.status == UNSUPPORTED or KEY_REFUSED in str(exc))
    if isinstance(exc, GatewayError):
        return exc.status_code in MODEL_REFUSALS
    if isinstance(
        exc,
        UnavailableError
        | httpx.TransportError
        | CalendarUnavailableError
        | http.client.HTTPException,
    ):
        return True
    return google_outage(exc)
