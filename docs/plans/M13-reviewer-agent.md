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

**Status: code complete, delta not yet measured.** The reviewer is built, wired
into the graph, and covered by tests. The exit criterion — a published eval
delta — is *not* met, and the reason is in "What stopped the measurement" below.
Marking this done would be exactly the claim the module exists to avoid.

### The tools are what make it an agent

`freebusy_check` is the honest one: the extractor never looked at the calendar,
so a double-booking is something only the reviewer can see. `search_context`
comes from M11. Strip both and this collapses into LLM-as-judge — a second
opinion on the same evidence, which catches incoherence and very little else.

Tools are declared **only when they are connected**. A declared tool with
nothing behind it is worse than an absent one: the model spends a turn calling
it, gets an error, and the system prompt has promised a capability that does not
exist.

### Corrections are advisory, and that is a decision

A `revise` verdict carries suggested field values, and they are fed into a fresh
extraction rather than written onto the existing result. Patching fields
directly would let the reviewer set a start time that never passes through
`to_extraction_result` — no local-to-UTC conversion, no zone validation, no
ordering check. The reviewer is a better critic than a data-entry clerk.

### The cap is arithmetic, not instruction

`MAX_REVIEW_ROUNDS` is compared in the router in `build.py`, so no sequence of
verdicts can outlast it: three opinions, two re-extractions, then the graph
carries on with whatever it has and puts the reviewer's objections on the
approval card. A separate budget from `MAX_REVISIONS` — a human asking twice and
an agent asking twice should not exhaust each other's allowance.

The eval wrapper duplicates the cap rather than importing the graph, which is a
real cost. The alternative was dragging a checkpointer, a ledger and a Postgres
connection into a harness whose whole value is running anywhere.

### What stopped the measurement

Two attempts, both invalidated, neither by the reviewer:

1. **503 on `gemini-3.7-flash`** — model overloaded, 2 of 9 fixtures failed
   after four retries. Score printed 77.8%; meaningless.
2. **429 daily quota on `gemini-3.6-flash`** — the *extraction* model, 20
   requests per day, exhausted by the first arm plus the day's earlier work.
   6 of 9 fixtures failed. Score printed 33.3%; also meaningless.

What did come through is worth recording as partial, not as a result: in
attempt 1 the 7 fixtures that completed were **all still correct** — the reviewer
approved them and changed nothing. No harm on 7, no evidence either way on 2.

**The unreviewed arm scored 100% on the meeting slice** (9 fixtures, clean run,
`results/eval-gemini-20260817T073830Z.json`). That is a ceiling, and it means
this comparison can only ever detect *harm*. A reviewer cannot improve on a
perfect score. Worth knowing before spending three days on one.

To finish: one clean run of `--extractor gemini_reviewed --tag meeting` on a day
with quota, against that 100%.

### The bug this incident exposed

A run where fixtures error scores them as "not a meeting", and the runner has
always said so loudly on the terminal. **That warning lived only on the
terminal.** The saved JSON was indistinguishable from a legitimate run, so
`app.eval.publish` would have pushed a 33.3% quota outage into the dashboard's
accuracy chart as a genuine regression — and the eval-history view is the one
chart the whole project is built to make credible.

Now `to_dict` records `errors` and `trustworthy`, `save` names the file
`eval-INVALID-…` so it is visible from a directory listing, and `publish` refuses
it. The two invalid runs from today were deleted rather than kept, since they
predate the field and would be indistinguishable from real ones.

This is the second time an eval-integrity bug has surfaced from an infrastructure
failure rather than from the code being read. Both times the failure was loud and
the persistence was quiet.

### Verified

```
ruff / mypy --strict   clean
pytest                 454 passed (twice, to catch order dependence)
```
