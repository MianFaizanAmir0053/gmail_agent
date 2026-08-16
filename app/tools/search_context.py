"""`search_context` as a model-callable tool.

Unlike `create_calendar_event`, this tool has no side effects, so the harness
lets the model call it without an approval gate. What it *can* do is cost money
and time, which is why the loop that runs it is bounded rather than trusted.

## The description does most of the work

Models are conservative about reaching for tools. A description that says what a
tool *is* gets called rarely; one that names the **trigger condition** gets
called when it should be. So this one spells out the situations -- a name with
no address, a reference to something previously agreed -- rather than describing
a search index.

The schema is hand-written in Gemini's OpenAPI subset for the same reason as
`calendar_tool`: Pydantic emits `additionalProperties` and
`anyOf: [string, null]`, both of which the subset rejects. `SearchContextInput`
re-validates whatever arrives, because a declared schema is advisory.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.rag.search import DEFAULT_LIMIT, Hit

TOOL_NAME = "search_context"


class SearchContextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    participant: str | None = None
    since: date | None = None

    @field_validator("participant", mode="before")
    @classmethod
    def _normalise_participant(cls, value: object) -> object:
        """Addresses are stored lowercased, and the model will not be consistent.

        An empty string also has to become None: it means "no filter", and
        passing it through would filter for an address that is the empty string
        and return nothing, which looks exactly like "no such person".
        """
        if isinstance(value, str):
            cleaned = value.strip().lower()
            return cleaned or None
        return value


SEARCH_CONTEXT_TOOL: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Search past email threads for context about people, projects, or prior "
        "commitments. Call this whenever the email refers to a person, meeting, or "
        "decision you do not have details for -- for example to resolve an attendee's "
        "full email address from a first name, to check what was agreed previously, or "
        "to find where a recurring meeting is usually held. Prefer calling it over "
        "guessing an address or omitting an attendee."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "What to look for, in natural language. Include names and any "
                    "distinctive terms verbatim -- exact strings are matched separately "
                    "from meaning."
                ),
            },
            "participant": {
                "type": "string",
                "nullable": True,
                "description": (
                    "Optional filter: only threads involving this exact email address. "
                    "Leave null when you only have a first name -- put the name in the "
                    "query instead."
                ),
            },
            "since": {
                "type": "string",
                "description": "Optional filter: only threads on or after this date, YYYY-MM-DD.",
                "nullable": True,
            },
        },
        "required": ["query"],
    },
}


class Searcher(Protocol):
    def __call__(
        self,
        query: str,
        *,
        participant: str | None = ...,
        since: date | None = ...,
        limit: int = ...,
    ) -> list[Hit]: ...


def _render(hit: Hit) -> dict[str, Any]:
    return {
        "subject": hit.subject,
        "sent_at": hit.sent_at.date().isoformat(),
        "participants": list(hit.participants),
        "excerpt": hit.snippet(),
    }


def execute_search_context(
    searcher: Searcher, raw_args: dict[str, Any], *, limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    """Run a tool call and return the function-response payload.

    Never raises. Bad arguments come back as an `error` field the model can read
    and correct on the next turn -- raising would abort an extraction over a
    speculative lookup, which is a far worse trade than one wasted turn.
    """
    try:
        args = SearchContextInput.model_validate(raw_args)
    except ValidationError as exc:
        return {"error": f"Invalid arguments: {exc.errors()[0]['msg']}", "results": []}

    hits = searcher(args.query, participant=args.participant, since=args.since, limit=limit)

    if not hits:
        # Said explicitly. An empty list reads as "the search broke"; this reads
        # as evidence, and stops the model from retrying the same query.
        return {"results": [], "note": "No past threads matched. Do not invent details."}

    return {"results": [_render(hit) for hit in hits]}
