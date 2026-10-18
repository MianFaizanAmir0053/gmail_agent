"""The one judge of an outage (`app.outage`): a failure every message would
meet, as against one that is the message's own."""

from __future__ import annotations

import http.client
from typing import Any

import httpx
import pytest
from fake_gmail import http_error
from google.genai import errors as genai_errors

from app.extraction.evaluation import GatewayError
from app.extraction.llm import UnavailableError
from app.google.calendar import CalendarUnavailableError
from app.outage import is_outage


def _gemini(code: int, status: str, message: str, **detail: Any) -> genai_errors.APIError:
    """Gemini's error as google-genai raises it, from the JSON the API answers."""
    error: dict[str, Any] = {"code": code, "status": status, "message": message}
    if detail:
        error["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", **detail}]
    kind = genai_errors.ClientError if code < 500 else genai_errors.ServerError
    return kind(code, {"error": error})


EVERYONES = [
    UnavailableError("gemini-3.6-flash unavailable after 4 attempts: 503 UNAVAILABLE"),
    httpx.ConnectError("[Errno 11001] getaddrinfo failed"),
    httpx.ReadTimeout("timed out"),
    _gemini(401, "UNAUTHENTICATED", "Request had invalid authentication credentials."),
    _gemini(403, "PERMISSION_DENIED", "Billing is not enabled for this project."),
    _gemini(404, "NOT_FOUND", "models/gemini-3.6-flash is not found."),
    _gemini(429, "RESOURCE_EXHAUSTED", "Quota exceeded: GenerateRequestsPerDayPerProjectPerModel"),
    _gemini(400, "INVALID_ARGUMENT", "API key not valid.", reason="API_KEY_INVALID"),
    _gemini(400, "FAILED_PRECONDITION", "User location is not supported for the API use."),
    GatewayError(401, "invalid API key"),
    GatewayError(403, "forbidden"),
    CalendarUnavailableError("the calendar's free/busy cannot be read"),
    http.client.IncompleteRead(b"partial"),
    http.client.BadStatusLine(""),
    http_error(503),
    http_error(401),
    FileNotFoundError("token.enc"),
]

ITS_OWN = [
    _gemini(400, "INVALID_ARGUMENT", "The input token count exceeds the maximum."),
    GatewayError(400, "bad request"),
    http_error(400),
    ValueError("the model's JSON did not parse"),
]


@pytest.mark.parametrize("exc", EVERYONES, ids=lambda exc: type(exc).__name__)
def test_a_failure_every_message_would_meet_is_an_outage(exc: BaseException) -> None:
    assert is_outage(exc)


@pytest.mark.parametrize("exc", ITS_OWN, ids=lambda exc: type(exc).__name__)
def test_a_failure_of_the_message_itself_is_not(exc: BaseException) -> None:
    assert not is_outage(exc)


def test_only_the_model_apis_own_errors_are_read_for_a_status() -> None:
    """A `code` of 403 on anything else says nothing about the model."""

    class UnrelatedError(Exception):
        code = 403

    assert not is_outage(UnrelatedError("not from a model"))


def test_an_invalid_key_is_named_where_google_genai_prints_it() -> None:
    """The marker sits in the error's details, which its text carries: if a
    google-genai release stopped printing them, an invalid key would quietly
    become each message's own failure again."""
    error = _gemini(400, "INVALID_ARGUMENT", "API key not valid.", reason="API_KEY_INVALID")

    assert "API_KEY_INVALID" in str(error)
