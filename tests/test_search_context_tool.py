"""The tool boundary: argument validation, the response payload, and the loop."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from app.rag.search import Hit
from app.tools.search_context import (
    SEARCH_CONTEXT_TOOL,
    TOOL_NAME,
    SearchContextInput,
    execute_search_context,
)


class RecordingSearcher:
    def __init__(self, results: list[Hit] | None = None) -> None:
        self.results = results if results is not None else []
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        query: str,
        *,
        participant: str | None = None,
        since: date | None = None,
        limit: int = 5,
    ) -> list[Hit]:
        self.calls.append(
            {"query": query, "participant": participant, "since": since, "limit": limit}
        )
        return self.results


def a_hit(subject: str = "Platform review") -> Hit:
    return Hit(
        chunk_id=1,
        thread_id="t1",
        message_id="m1",
        subject=subject,
        content="Ahmed Raza will join the platform review on Thursday.",
        participants=("ahmed.raza@northwind.example",),
        sent_at=datetime(2026, 8, 12, 9, 0, tzinfo=UTC),
        score=0.03,
    )


# --- arguments --------------------------------------------------------------


def test_a_blank_participant_means_no_filter() -> None:
    """Passed through it would filter for the empty address and return nothing,
    which is indistinguishable from "no such person"."""
    assert SearchContextInput(query="q", participant="  ").participant is None


def test_participants_are_lowercased_to_match_storage() -> None:
    assert SearchContextInput(query="q", participant="Ahmed@X.com").participant == "ahmed@x.com"


def test_unknown_arguments_are_rejected() -> None:
    with pytest.raises(ValidationError):
        SearchContextInput.model_validate({"query": "q", "k": 10})


def test_bad_arguments_come_back_as_an_answer_not_an_exception() -> None:
    """A speculative lookup must not be able to abort an extraction."""
    searcher = RecordingSearcher()
    result = execute_search_context(searcher, {"query": ""})

    assert "error" in result
    assert result["results"] == []
    assert searcher.calls == []


def test_a_hallucinated_date_does_not_raise() -> None:
    assert "error" in execute_search_context(RecordingSearcher(), {"query": "q", "since": "soon"})


# --- responses --------------------------------------------------------------


def test_no_matches_says_so_explicitly() -> None:
    """An empty list reads as a broken search; this reads as evidence."""
    result = execute_search_context(RecordingSearcher([]), {"query": "ahmed"})
    assert result["results"] == []
    assert "invent" in result["note"]


def test_a_result_carries_the_address_the_model_needs() -> None:
    result = execute_search_context(RecordingSearcher([a_hit()]), {"query": "ahmed"})
    assert result["results"][0]["participants"] == ["ahmed.raza@northwind.example"]
    assert result["results"][0]["sent_at"] == "2026-08-12"


def test_filters_reach_the_searcher() -> None:
    searcher = RecordingSearcher()
    execute_search_context(
        searcher, {"query": "offsite", "participant": "SARA@x.com", "since": "2026-01-01"}
    )
    assert searcher.calls[0]["participant"] == "sara@x.com"
    assert searcher.calls[0]["since"] == date(2026, 1, 1)


# --- the declaration --------------------------------------------------------


def test_the_declaration_avoids_what_gemini_rejects() -> None:
    """`additionalProperties` and `anyOf: [string, null]` are both refused."""
    rendered = repr(SEARCH_CONTEXT_TOOL)
    assert "additionalProperties" not in rendered
    assert "anyOf" not in rendered


def test_the_description_names_the_trigger_not_just_the_capability() -> None:
    """Models under-call tools described only by what they are."""
    description = SEARCH_CONTEXT_TOOL["description"].lower()
    assert "call this whenever" in description
    assert SEARCH_CONTEXT_TOOL["name"] == TOOL_NAME
