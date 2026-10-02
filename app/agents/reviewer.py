"""A reviewer that can go and check, rather than merely disagree.

## What makes this an agent and not a second prompt

The weak version of this idea is LLM-as-judge: one more call, over the same
email, with the same information, asked whether the first answer looks right. It
can catch incoherence and almost nothing else, because it has no way to learn
anything the extractor did not already have.

This reviewer gets **its own evidence**:

- `freebusy_check` -- is that slot actually free? The extractor never looked.
- `search_context` -- who is this person, and what was agreed last time? The
  extractor may have had this too, but the reviewer can ask a different question
  of it, knowing what the answer is being checked against.

That is the whole reason M13 depends on M11. Strip the tools and this collapses
back into a second opinion from the same evidence.

## Corrections are advisory, deliberately

A `revise` verdict carries `corrections`, and they are fed back into a fresh
extraction rather than written onto the existing result. Patching fields directly
would let the reviewer set a start time that never passes through
`to_extraction_result` -- no local-to-UTC conversion, no zone validation, no
ordering check. The reviewer is a better critic than it is a data-entry clerk.

## Times stay local

Same convention as extraction: the reviewer reads and writes local wall-clock
plus an IANA zone, and never an offset. A reviewer doing its own timezone
arithmetic would be a second source of exactly the bug it is meant to catch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from app.contracts import EmailMessage, ExtractionResult
from app.extraction import prompts
from app.extraction.llm import GenaiLike, Usage, structured_call
from app.google.calendar import CalendarClient
from app.policy.budget import Gate
from app.policy.models import MAX_PROMPT_CHARS
from app.tools.calendar_tool import FREEBUSY_TOOL, execute_freebusy
from app.tools.search_context import SEARCH_CONTEXT_TOOL, Searcher, execute_search_context
from app.tools.search_context import TOOL_NAME as SEARCH_TOOL_NAME

FREEBUSY_TOOL_NAME = "freebusy_check"


class Corrections(BaseModel):
    """Fields the reviewer believes are wrong. Omit anything that is right."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, description="Corrected title, or null.")
    start_local: str | None = Field(
        default=None,
        description=(
            "Corrected local wall-clock start, ISO 8601 with no offset and no Z. "
            "Do NOT convert to UTC."
        ),
    )
    end_local: str | None = Field(default=None, description="Corrected local end, same format.")
    timezone: str | None = Field(
        default=None, description="Corrected IANA zone name. Never an offset like '+05:00'."
    )
    attendees: list[str] | None = Field(
        default=None, description="The full corrected attendee list, or null to leave it alone."
    )
    location: str | None = Field(default=None, description="Corrected location, or null.")

    def describe(self) -> str:
        parts = [f"{name}={value!r}" for name, value in self.model_dump(exclude_none=True).items()]
        return ", ".join(parts)


class ReviewVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "revise", "reject"] = Field(
        description=(
            "approve: the extraction is correct as it stands. "
            "revise: something is wrong and can be fixed. "
            "reject: this should not become a calendar event at all."
        )
    )
    issues: list[str] = Field(
        description="One short sentence per problem found. Empty when approving."
    )
    corrections: Corrections = Field(
        default_factory=Corrections,
        description="Suggested field values. Only meaningful when revising.",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(description="One sentence on what was checked and what it showed.")

    def feedback(self) -> str:
        """What gets handed back to the extractor on a revise."""
        lines = [f"- {issue}" for issue in self.issues]
        suggested = self.corrections.describe()
        if suggested:
            lines.append(f"- Suggested corrections: {suggested}")
        return "\n".join(lines)


REVIEWER_SYSTEM = f"""\
You review calendar events that another system extracted from email, before a
human is asked to approve them. You are the last check that costs nothing.

Your job is to find errors, not to agree. But do not manufacture them: an
extraction that is correct should be approved, and "approve" is the right answer
most of the time.

Check, in order:

1. **The date and time.** Does the email actually fix them, or did the extractor
   guess? "Next Friday" sent on a Friday is ambiguous. If the email is genuinely
   ambiguous, say so rather than picking one.
2. **The timezone.** If the sender stated a zone, that zone must be in the
   result. If the event lands at an implausible local hour -- 3am, or a weekend
   when the thread is clearly about work -- suspect a zone error.
3. **Availability.** Call `freebusy_check` for the proposed slot. A double
   booking is a real error the extractor cannot see.
4. **Attendees.** Call `search_context` if a person is named without an address,
   or if you suspect someone who is always on this thread has been dropped.
   Never invent an address.
5. **Whether this is a meeting at all.** Cancellations, newsletters advertising
   webinars, and "let's grab coffee sometime" are not events. Reject them.

Reject rather than revise when the problem is that no event should exist.
Revise when a field is wrong and you can say what it should be.

{prompts.CONVENTIONS}
Report times as local wall-clock with an IANA zone, exactly as the extractor is
required to. Never do timezone arithmetic yourself.
"""


@dataclass(slots=True)
class ReviewStats:
    reviews: int = 0
    approvals: int = 0
    revisions: int = 0
    rejections: int = 0
    tool_calls: int = 0
    usages: list[Usage] = field(default_factory=list)

    def record(self, verdict: ReviewVerdict, usage: Usage) -> None:
        self.reviews += 1
        self.usages.append(usage)
        if verdict.decision == "approve":
            self.approvals += 1
        elif verdict.decision == "revise":
            self.revisions += 1
        else:
            self.rejections += 1


@dataclass(slots=True)
class Reviewer:
    client: GenaiLike
    model: str
    searcher: Searcher | None = None
    calendar: CalendarClient | None = None
    stats: ReviewStats = field(default_factory=ReviewStats)

    @property
    def tools(self) -> list[dict[str, Any]]:
        """Only tools that are actually connected.

        Declaring a tool with no implementation behind it is worse than omitting
        it: the model spends a turn calling it and gets an error back, and the
        prompt has promised a capability the reviewer does not have.
        """
        available: list[dict[str, Any]] = []
        if self.calendar is not None:
            available.append(FREEBUSY_TOOL)
        if self.searcher is not None:
            available.append(SEARCH_CONTEXT_TOOL)
        return available

    def __call__(
        self,
        email: EmailMessage,
        extraction: ExtractionResult,
        *,
        now_utc: datetime,
        user_timezone: str,
    ) -> ReviewVerdict:
        tools = self.tools
        completion = structured_call(
            self.client,
            model=self.model,
            system=REVIEWER_SYSTEM,
            user=self._user(email, extraction, now_utc, user_timezone),
            schema=ReviewVerdict,
            max_output_tokens=2048,
            tools=tools or None,
            dispatch=self._dispatch if tools else None,
        )
        self.stats.record(completion.parsed, completion.usage)
        return completion.parsed

    def _dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.stats.tool_calls += 1
        if name == FREEBUSY_TOOL_NAME and self.calendar is not None:
            return execute_freebusy(self.calendar, args)
        if name == SEARCH_TOOL_NAME and self.searcher is not None:
            return execute_search_context(self.searcher, args)
        return {"error": f"No such tool: {name}"}

    def _user(
        self,
        email: EmailMessage,
        extraction: ExtractionResult,
        now_utc: datetime,
        user_timezone: str,
    ) -> str:
        proposed = extraction.model_dump(mode="json", exclude={"reasoning", "confidence"})
        # The proposal under review is never cut: on a long email the body
        # gives way, so there is always something to review (M17, D5).
        tail = (
            f"The extractor proposed:\n{proposed}\n\nIts stated reasoning: {extraction.reasoning}\n"
        )
        content = prompts.user_content(
            email,
            now_utc=now_utc,
            user_timezone=user_timezone,
            room=MAX_PROMPT_CHARS - 1 - len(tail),
        )
        return f"{content}\n{tail}"


def build_reviewer(
    searcher: Searcher | None = None,
    calendar: CalendarClient | None = None,
    *,
    gate: Gate | None = None,
) -> Reviewer:
    """Wire a reviewer from settings, metered like the pipeline (M17, D5)."""
    from app.config import get_settings
    from app.policy import models

    settings = get_settings()
    # Cast for the same reason `build_pipeline` does: the client is
    # structurally compatible with the Protocol but not nominally so.
    return Reviewer(
        client=cast(GenaiLike, models.client(settings, gate or models.local_gate(settings))),
        model=settings.reviewer_model,
        searcher=searcher,
        calendar=calendar,
    )
