# M03 · Extraction & classification

**Est.** 2–3 days · **Depends on** M01, M02 · **Blocks** M04

## Goal

Turn an `EmailMessage` into an `ExtractionResult`, and **record the frozen baseline number**. This is the "71%" in your future sentence — commit it and never edit it.

## Deliverables

- `app/extraction/schema.py` — the Pydantic model (already in `contracts.py`; extend if needed)
- `app/extraction/prompts.py` — system prompt, few-shot examples, cache-friendly ordering
- `app/extraction/classify.py` — is this a meeting? (cheap path)
- `app/extraction/extract.py` — full extraction (only runs when classify says yes)
- `results/baseline.json` — **committed, frozen, never edited**
- A README line recording the baseline

## Two-step design

```
email ──► classify (cheap) ──► is_meeting? ──no──► done
                                   │
                                  yes
                                   ▼
                            extract (full)
```

Most email isn't a meeting. Running the expensive extraction on every newsletter is the single biggest avoidable cost in the system.

## Date grounding — do this first

Models get "next Tuesday" wrong without context. Inject into every prompt:

- Current UTC datetime, ISO format
- The user's IANA timezone
- Today's **weekday name** (models reason about "next Tuesday" better with the anchor spelled out)

Then require the model to output `start_utc` **plus** the originating `timezone`. Store both. Score in UTC.

Half your accuracy lives in this section. It was deferred to Phase 5 in the original plan, which would have meant every earlier eval number measured the wrong thing.

## Model & API notes

Default to `claude-opus-5` for both steps, with `effort: "low"` on classify. One model across both paths keeps the prompt cache warm and the code simple.

- Use `client.messages.parse()` with the Pydantic model — it validates the response against your schema automatically. Canonical param is `output_config={"format": ...}`; the old top-level `output_format` is deprecated.
- **Assistant-turn prefills return a 400** on Opus 5. The "prefill `{` to force JSON" trick no longer applies — structured outputs replace it.
- **Check `stop_reason` before reading `response.content`.** A refusal returns HTTP 200 with `stop_reason: "refusal"` and possibly empty content. Code that indexes `content[0]` unconditionally will crash.
- Count tokens with `client.messages.count_tokens()`, never `tiktoken` — it's OpenAI's tokenizer and undercounts Claude by 15–20%.

## Prompt caching

Minimum cacheable prefix on Opus 5 is 512 tokens. Order the prompt **stable → volatile**:

```
[ system prompt ] [ few-shot examples ] [ tool defs ]  ← cache breakpoint here
[ today's date ] [ the email body ]                     ← volatile, after the breakpoint
```

A `datetime.now()` interpolated into the *system prompt* invalidates the cache on every single request. Verify it's working: `usage.cache_read_input_tokens` should be non-zero on the second and later calls. If it's always zero, something upstream is changing between requests.

## Cost experiment (optional, high value)

Run the classify step on `claude-haiku-4-5` ($1/$5 per MTok vs Opus 5's $5/$25) against the M02 eval set. Record the accuracy delta and the cost delta in the README.

"I measured Haiku vs Opus on the classify step; it cost 5× less for 1.5 points of F1, so I kept it" is exactly the kind of decision interviewers probe. **Measuring it is the deliverable** — which one you pick afterwards matters much less.

## Exit criterion

`make eval` produces a real number against the golden set, and that number is committed to `results/baseline.json` and frozen.

## Running notes

**The model is never asked for UTC.** The obvious schema asks for `start_utc` directly; don't. Converting "4pm Karachi on 19 August" to UTC is arithmetic over a timezone database including DST rules that vary by year and jurisdiction, and models get it wrong *plausibly* — an hour out, silently, for half the year.

So the wire schema asks for `start_local` (naive wall-clock, exactly as a human reading the email would say it) plus the IANA `timezone` those times were expressed in, and `zoneinfo` does the arithmetic. The model does language; the standard library does calendars. `test_daylight_saving_is_handled_by_zoneinfo_not_the_model` pins it: 9am Los Angeles is 16:00Z in August but 17:00Z in December, and nothing in the prompt has to know that.

**`tzdata` is a runtime dependency, not a dev one.** Windows ships no IANA database and slim Linux images frequently drop it, so `ZoneInfo("Asia/Karachi")` raises `ZoneInfoNotFoundError` and *every* timezone conversion fails. Caught by the test suite; would otherwise have surfaced as "unknown IANA zone" against a perfectly valid zone in the container.

**Wire model is separate from `ExtractionResult`.** The wire schema is shaped by what a model emits reliably; the contract is shaped by what the rest of the system needs. Keeping them apart means the prompt schema can change without touching every consumer.

**All properties are marked required in the JSON schema**, even nullable ones. Without that the model may omit a field rather than emit `null`, and "no location given" becomes indistinguishable from "didn't look".

**Malformed extractions degrade rather than raise.** An unknown zone or an end-before-start returns `is_meeting=False` with the reason recorded, instead of propagating a half-built event that fails later and further from the cause.

**Prompt is split stable/volatile for caching.** System holds instructions, conventions, and worked examples with the cache breakpoint at the end; the grounding block (current time, weekday spelled out) and the email go in the user turn. `test_current_time_never_reaches_the_system_prompt` guards the own-goal of interpolating `now` into the cached prefix.

**Few-shot examples deliberately avoid the golden set**, and a test asserts no fixture title or subject appears in the system prompt. Teaching conventions is legitimate; teaching the answers would make the score meaningless.

### Provider changed: Anthropic → Gemini

Switched at the user's request, once it turned out the available key was a Gemini one. The swap stayed inside `llm.py` plus wiring, which is what `Usage`/`Completion` being provider-neutral bought. `payloads.py`, the eval harness, the ledger, and the Google clients were untouched.

Four things bit, all worth remembering:

1. **`extra="forbid"` makes Pydantic emit `additionalProperties`, and the endpoint 400s on it.** Keeping strict validation on our side while sending a scrubbed schema is the fix — `response_json_schema()` strips that one key and passes the rest.

2. **Stripping schema keys by name is dangerous.** A first attempt also dropped `title` as "documentation noise" and deleted `properties.title` — a real payload field — leaving it in `required` and nowhere else. The 400 blamed the top-level schema and named nothing useful. Schema keywords and field names share a namespace.

3. **`thinking_budget` is the 2.x knob and 3.x models reject it** with a bare `400 Request contains an invalid argument`. The 3.x equivalent is `thinking_level` (MINIMAL / LOW / MEDIUM / HIGH).

4. **`models.list()` advertises models the key cannot call.** `gemini-2.5-flash-lite` is listed and then 404s with "no longer available to new users". Added `--probe` to `app.extraction.models`, which makes one tiny call per candidate; guessing was costing quota.

**Free-tier quota is the real model-selection constraint, not capability.** `gemini-3.7-flash` allows **20 requests per day** — a single eval run exhausts it. Splitting the two stages across different models (`gemini-3.5-flash-lite` triage, `gemini-3.6-flash` extraction) also splits the per-model quota, which matters more here than any capability edge.

The retry logic had to learn the difference: a **per-day** quota is not worth retrying, a **per-minute** one clears in twenty seconds. An early heuristic matched on `FreeTier`, which appears in *both* quota IDs, and gave up immediately on a limit that would have cleared — the run reported 92.9% "untrustworthy" purely because of my own retry bug. Now it matches `PerDay` only and honours the server's `retryDelay` hint instead of guessing a backoff.

**The eval harness now tolerates per-fixture errors** and prints a loud warning, because one 503 was voiding all fourteen results. The warning matters: an errored fixture is scored as "not a meeting", which is accidentally *correct* on every non-meeting — so a quiet failure would inflate the headline rather than depress it.

### Baseline — frozen

`results/baseline.json`, 2026-08-16, `gemini-3.5-flash-lite` + `gemini-3.6-flash`:

```
is_meeting    accuracy 100.0%   precision 100.0%   recall 100.0%   f1 100.0%
title         100.0%    start_utc 100.0%    end_utc 100.0%    timezone 100.0%
attendees      88.9%    micro-f1 96.0%

HEADLINE exact match:  92.9%   (13/14)
```

Against the M02 floor of **35.7%** (`always_no`) and **0.0%** (`always_yes`).

Only failure is `fx-005` (all-day), on `attendees`.

**Do not "fix" that yet.** The fixture expects `ops@example.com` as an attendee, but `fx-013` establishes the opposite convention — distribution lists are not people — and `ops@` looks exactly like one. The label may be wrong and the model right. Adjusting a label after seeing the prediction is how a baseline stops meaning anything; investigate it as its own change, with the before/after recorded.

Every event field is at 100% on the first real run, which is a suspiciously good start on 14 synthetic fixtures written by the same author as the prompt. The number will drop when real anonymised mail enters the set — that is the point of M02's fetch/anonymise path, and the honest version of this story.
