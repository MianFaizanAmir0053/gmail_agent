# M13 · Reviewer agent

**Est.** 2–3 days · **Depends on** M11 · **Blocks** M14

## Goal

A second agent that checks the extractor's work before the human sees it — genuinely multi-agent, not a second prompt wearing a hat.

## Why the original spec was weak

"Add a reviewer agent that checks the extractor's output" describes LLM-as-judge: one more model call, same information, no tools, no authority. What makes it actually multi-agent is that the reviewer has **its own tools and its own information** — it can go look things up the extractor never saw.

## Deliverables

- `app/agents/reviewer.py` — separate system prompt, own tool set
- `ReviewVerdict` schema
- Bounded revision loop in the M05 graph (hard cap: 2 iterations)
- Eval delta measured and published

## The reviewer's tools

- `freebusy_check(start, end)` — is this slot actually free?
- `search_context(query)` — from M11; who is this person, have we met before?

This is why M13 depends on M11. A reviewer without `search_context` is a second opinion from the same evidence, which is worth much less.

## Verdict schema

```python
class ReviewVerdict(BaseModel):
    decision: Literal["approve", "revise", "reject"]
    issues: list[str]                    # human-readable, shown on the approval card
    corrected_fields: dict[str, Any]     # field -> corrected value
    confidence: float
```

## What it should catch

- Timezone errors (event lands at 3am local)
- Double-bookings (free/busy says the slot is taken)
- Ambiguous dates the extractor guessed at ("next Friday" sent on a Friday)
- Wrong or missing attendees (`search_context` knows Sara is always on these)
- Non-meetings that slipped past classification

## Loop safety

```
extract ──► review ──┬── approve ──► await_approval
                     ├── revise (max 2) ──► extract
                     └── reject ──► log + skip
```

**Cap the revisions in the graph, not in the prompt.** A prompt instruction to "only revise twice" is a suggestion; a counter in graph state is a guarantee.

## Measure it — including if it's bad

Run the M02 eval set with the reviewer on and off. Publish both numbers.

A reviewer can *reduce* accuracy: it may "correct" fields that were already right, or reject valid meetings it's unsure about. If that's what your data shows, that's the finding, and reporting it is worth more than a claim you can't support. Interviewers can tell the difference between someone who measured and someone who assumed.

## Exit criterion

Eval delta measured, committed to `results/`, and written up — **whatever direction it points**.

## Running notes

_(record what surprised you here)_
