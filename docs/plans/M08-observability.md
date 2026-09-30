# M08 · Observability

**Est.** 2–3 days · **Depends on** M07 · **Blocks** M09
**Highest value per hour in the entire plan.**

## Goal

Every run leaves a trace with timing, tokens, and cost. This is the module most candidates skip, and it's the clearest "I've run this in production" signal you can send.

## Deliverables

- `migrations/003_observability.sql` — `runs` and `spans`
- `app/obs/callback.py` — a LangGraph callback handler that records automatically
- `app/obs/pricing.py` — a single pricing config keyed by model ID
- PII redaction on stored inputs/outputs
- Daily cost rollup query

## Schema

```sql
CREATE TABLE runs (
    trace_id     UUID PRIMARY KEY,
    gmail_message_id TEXT REFERENCES processed_messages(gmail_message_id),
    status       TEXT NOT NULL,        -- success|failed|rejected
    started_at   TIMESTAMPTZ NOT NULL,
    ended_at     TIMESTAMPTZ,
    total_cost_usd NUMERIC(12,6),
    error        TEXT
);

CREATE TABLE spans (
    id           BIGSERIAL PRIMARY KEY,
    trace_id     UUID NOT NULL REFERENCES runs(trace_id),
    node         TEXT NOT NULL,
    model        TEXT,
    input_redacted  JSONB,
    output_redacted JSONB,
    latency_ms   INT NOT NULL,
    tokens_in    INT,
    tokens_out   INT,
    tokens_cached_read   INT,
    tokens_cached_write  INT,
    cost_usd     NUMERIC(12,6),
    status       TEXT NOT NULL,
    retry_count  INT NOT NULL DEFAULT 0,
    started_at   TIMESTAMPTZ NOT NULL
);
CREATE INDEX ON spans (trace_id);
CREATE INDEX ON spans (started_at DESC);
```

## Cost accounting

**Store raw token counts, not just a computed cost.** Rates change; if you've only kept dollars you can never recompute history.

```python
PRICING = {  # USD per million tokens
    "claude-opus-5":   {"in": 5.00, "out": 25.00},
    "claude-haiku-4-5": {"in": 1.00, "out":  5.00},
}
```

Cache reads bill at roughly 0.1× input; cache writes at roughly 1.25× (5-minute TTL). Track `cache_read_input_tokens` and `cache_creation_input_tokens` separately or your cost model will be wrong — and you'll lose the ability to show that prompt caching actually paid off.

## Capture via callback, not by hand

Instrumenting each call site by hand guarantees you'll miss one and silently under-report. A LangGraph callback handler fires on every node and every model call, so coverage is automatic.

## PII redaction

Traces store email content. Redact before insert: email addresses → `<email>`, phone numbers → `<phone>`, long digit runs → `<number>`. Keep enough structure to debug an extraction failure without warehousing other people's correspondence.

## Exit criterion

Every run visible in Postgres with an accurate cost, and `SELECT date, SUM(cost_usd) FROM runs GROUP BY 1` reconciles against the Anthropic console for the same window within a few percent.

## Optional depth

Stand up Langfuse or LangSmith alongside for a week, then write up why you kept — or dropped — the hand-rolled version. "I built it, compared it to the managed option, and here's the tradeoff" is a stronger answer than either choice alone.

## Running notes

**An unpriced model records `cost_usd = NULL`, never `0`.** Zero is a positive claim that something was free. A model missing from the rate table would then quietly under-report the total, and the first sign of trouble would be a bill that does not match the dashboard. `unpriced_models()` surfaces the gap and the report prints it loudly, because the failure mode of cost tracking is looking complete while being wrong.

**Token counts are the source of truth; cost is derived and stored alongside.** Rates change. Keeping only dollars means history can never be recomputed and every past figure silently starts meaning something different from the day the rate moved.

**`PRICING_CHECKED_ON` is printed next to every total.** The rates are hand-entered from a pricing page, not reported by the API, so a stale table is inevitable — the date makes it visible instead of assumed current.

**Cached input falls back to the full input rate when unknown.** Overestimating is the safer direction for a cost figure to be wrong in.

**Thinking tokens bill as output but are counted separately.** Folding them in would be arithmetically correct and would destroy the ability to see how much spend is reasoning — the first thing anyone tunes.

**Capture is by wrapping nodes, not by instrumenting call sites.** `build_graph` wraps every node through one helper, so a node added later is traced by construction. Hand-instrumentation guarantees one gets missed, and a missing span looks exactly like a node that was fast and free.

**Usage travels by `ContextVar`, not by parameter.** `structured_call` reports tokens to whatever span encloses it. Threading a recorder through the pipeline, the graph, and every node would put observability plumbing in the signature of code that has nothing to do with observability — and it is a no-op outside a trace, so the eval harness and unit tests need no wiring at all.

**A parked run records `awaiting_approval`, not `success`.** Recording it as success would have the dashboard claim work completed that is still waiting on a human.

**Span writes are wrapped in try/except.** Observability must never be the reason a run fails.

**`GraphSession` builds its graph per call.** A single long-lived graph would be pinned to one stale `trace_id`. Construction is pure — no I/O — so this is cheap.

### Verified

```
ruff / format / mypy --strict   pass
pytest                          322 tests
```

Live run against the real inbox, three messages, traces written and reported:

```
Runs (last 1d)
  success               3   avg   2101ms   $0.0008

Nodes
  node                n     avg     p95       in     out   think  cached      cost  err
  classify            3   1308ms   1800ms     6943     141       0       0   $0.0008    0
  fetch               3    408ms    474ms        0       0       0       0   $0.0000    0
  skip                3     11ms     12ms        0       0       0       0   $0.0000    0
```

Three job alerts, correctly triaged, **$0.0008 total** — roughly **$0.027 per 100 emails** at this shape. `extract` never ran, which is the two-stage design paying for itself: the expensive node is only reached by mail that is actually a meeting.

Reconciliation against the provider's billing page is still outstanding, and is the thing that would confirm the rate table rather than merely make it self-consistent.

### Correction, 2026-09-30 — placeholder rates, and cached tokens billed twice

The rates behind the figures above were placeholders, and the table said so in a comment. Checked against the [pricing page](https://ai.google.dev/gemini-api/docs/pricing) (paid, Standard tier, text; USD per million tokens, input / output / cached input):

| Model | Was | Published |
|---|---|---|
| `gemini-3.5-flash-lite` | 0.10 / 0.40 / 0.025 | 0.30 / 2.50 / 0.03 |
| `gemini-3.5-flash` | 0.30 / 2.50 / 0.075 | 1.50 / 9.00 / 0.15 |
| `gemini-3.6-flash`, `gemini-3.7-flash` | 0.30 / 2.50 / 0.075 | 0.75 / 3.75 / 0.075 through 2026-12-31, then 1.50 / 7.50 / 0.15 |
| `gemini-2.5-flash` | 0.30 / 2.50 / 0.075 | 0.30 / 2.50 / 0.03 |
| `gemini-2.5-flash-lite` | 0.10 / 0.40 / 0.025 | 0.10 / 0.40 / 0.01 |

`gemini-3.8-flash` (same schedule as 3.6/3.7) and `gemini-3.1-flash-lite` (0.25 / 1.50 / 0.025) were added.

**The three classify runs above were `gemini-3.5-flash-lite`.** The report has no model column, so this is inferred, but nothing else fits: it was the configured triage model, `gemini-2.5-flash-lite` 404s for this key (M03), and the stored $0.000751 is exactly 6,943 × $0.10 + 141 × $0.40 per million — a rate the old table gave only those two. At the published rate the same tokens cost $0.0024354, which is **~$0.081 per 100 emails**, not $0.025. The database was not reachable when this was corrected, so that figure is recomputed from the counts recorded here rather than re-queried from `spans`.

**Cached tokens were billed twice.** Gemini's `prompt_token_count` already includes `cached_content_token_count`, and `cost_usd` added the cached count on top, so every cached token was charged the full input rate *and* the cached rate. At 3.5 Flash-Lite's published rates, a 1,200-token prompt with 900 of it cached came out at $0.000712 — more than the $0.000685 it costs with nothing cached. It is $0.000442. The cached share is now carved out of the input rather than added to it. The runs above had no cache hits, so their figure was not affected.

**Rates are dated.** A call is priced at the rate in force on the UTC day it ran; the tracer passes the span's start, so repricing a row from its stored `started_at` reproduces its stored cost. UTC, not `started_at.date()`, because psycopg returns TIMESTAMPTZ in the session's zone. The pricing page does not say which timezone its change dates follow; if it means US Pacific, UTC prices the first eight hours of 1 January 2027 at the new rate early — high, which is the safe direction for a rise.

**Still outstanding:** spans already in the database keep the `cost_usd` they were written with, so the dashboard's totals before this date are the understated ones until `.\tasks.ps1 reprice` is run against that database to recompute them from their tokens. `gemini-2.5-pro` stays unpriced (it bills by prompt size, which a flat rate cannot express), and reconciliation against an actual invoice is still the only thing that would confirm any of this.
