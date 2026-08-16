# Gmail → Calendar Agent: Corrected & Modularized Build Plan

## Context

You have a 5-phase roadmap for a portfolio/learning project: an LLM agent that reads Gmail, extracts meeting details, and creates Calendar events — growing into a multi-agent RAG system with observability. The goal is a deployed artifact plus interview-ready evidence of production engineering judgment.

The roadmap is directionally good. The stack choice is sound and should not be re-litigated. But it has **sequencing bugs that make the headline claim unprovable**, a **deployment landmine that kills the bot after 7 days**, several **data-correctness problems misfiled as model problems**, and one piece of résumé-driven advice that should be cut.

This document fixes those issues and decomposes the work into **14 independently shippable modules** with explicit interfaces, exit criteria, and a dependency graph — so each can be built, tested, and finished without holding the whole system in your head.

**Confirmed constraints:** No voice/audio scope (the `voice_agent` directory name is vestigial — rename to `mailagent` or similar). Personal `@gmail.com` account. No deadline; optimizing for depth of learning over ship speed.

---

## Part 1 — What's wrong with the current plan

### Critical (will bite you)

**1. The eval set arrives too late, which makes your best interview line unprovable.**
Step 12 puts the eval set in Phase 3, but Phase 1 step 5 already says "run it on 20 real emails, log what it gets wrong" — with nothing to compare against. Every prompt change in Phases 1–2 is unmeasured, and "extraction accuracy went from 71% to 94%" requires a *frozen baseline measured before the improvements*. If you build evals in Phase 3, you cannot honestly produce that number retroactively.
→ **The eval harness becomes M2, before the extractor exists.** Its first run scores a stub at 0%. The first real extractor run produces the frozen baseline.

**2. Personal-Gmail OAuth refresh tokens expire after 7 days.**
Gmail read scopes are *restricted*; Calendar write is *sensitive*. An unverified app stays in "Testing" publishing status, where refresh tokens are invalidated after 7 days. Your deployed bot will work all week and then silently stop. Full verification requires a CASA security assessment (~$500+, months) — not worth it for a portfolio project.
→ **Design for it from day one:** encrypted token storage, a `python -m app.auth.reauth` command, a token-health check that alerts via Telegram *before* expiry, and a README section explaining the tradeoff. This is a genuinely good interview story about OAuth verification tiers. Note in the README that a Google Workspace account ($6/mo) lets you set the app to "Internal" and removes the expiry entirely — a one-line config change if you ever want it.

**3. Phase 1 step 4 will write garbage to your real calendar.**
"Define `create_calendar_event` as a tool the model calls, not a function you call after" — combined with "run it on 20 real emails" — means 20 LLM-authored events on your actual calendar before any human-in-the-loop guardrail exists (that arrives in step 7).
→ **A dedicated test calendar + a `DRY_RUN` flag from the first line of code.** Nothing touches the primary calendar until M6.

**4. Idempotency is missing, and it's misfiled as a model problem.**
Step 16 lists "duplicate bookings" as a reviewer-*agent* responsibility. It isn't. A scheduled poller with no processed-message ledger will re-process the same email on every restart and every overlapping run. No LLM can fix that.
→ **M4 owns a `processed_messages` table keyed on Gmail message ID plus a `historyId` cursor.** This is a database constraint, not a prompt.

**5. Timezone handling is deferred to Phase 5 but determines half your accuracy in Phase 1.**
Calendar events need RFC3339 timestamps with correct IANA zones. "Tuesday at 3" is ambiguous without the sender's zone, your zone, and DST awareness. Discovering this in Phase 5 means every earlier eval number is measuring the wrong thing.
→ **Timezone is a first-class field in the M3 extraction schema.** Store UTC + originating IANA zone; score exact-to-the-minute in UTC.

**6. LangGraph human-in-the-loop needs a durable checkpointer, or the Telegram approval can't resume.**
Step 7's Confirm/Edit/Cancel may be answered hours later, after a deploy or restart. Without `langgraph-checkpoint-postgres` and `interrupt()`/`Command(resume=...)`, the graph state is gone and the callback has nothing to resume into. This is the single biggest technical trap in the plan.
→ **M5 exit criterion is explicitly: kill the process mid-approval, restart, click Confirm, event still gets created.**

### Should fix

**7. Real emails can't be committed to a public repo.** Your 30-email eval set contains other people's names, addresses, and business details. → M2 includes an anonymization step producing synthetic-but-realistic fixtures. Real emails stay in a gitignored local directory; committed fixtures are scrubbed.

**8. The Telegram bot is an unauthenticated door to your email.** Anyone who finds the bot username can talk to it. → Chat-ID allowlist in M6. Small change, real vulnerability, and a legitimate "guardrails" talking point.

**9. Deployment is under-specified and the free tiers named no longer exist.** Railway's free tier is now trial credit; Fly.io's allowances changed. Telegram long-polling plus a cron poller plus FastAPI is three processes if you're careless. → **One FastAPI process:** Telegram *webhook* (not long-polling) + APScheduler for the Gmail poll. Budget ~$5/month hosting; Neon or Supabase free tier for Postgres is genuinely fine at this scale.

**10. "Airflow in Docker Compose if you want the resume line" is bad advice.** Airflow needs a scheduler, a webserver, and its own metadata DB to orchestrate a pipeline processing a few dozen emails a day. The operational overhead will eat a week and teach you Airflow-the-installation, not pipelines. → **APScheduler**, or **Prefect 3** if you specifically want an orchestrator on the résumé. Never adopt infrastructure for the résumé line alone; that instinct is visible in interviews and reads badly.

**11. Re-ranking has a hidden cost.** A local cross-encoder (bge-reranker) needs RAM you won't have on a $5 box. → Use a hosted rerank API, or skip reranking and do Reciprocal Rank Fusion — then *measure* whether it helped. "I tried reranking and it didn't beat RRF on my eval set" is a stronger answer than a feature checkbox.

**12. The reviewer agent is weak as specified.** A second LLM call in a graph is closer to LLM-as-judge than multi-agent. → Give it **its own tools** (calendar free/busy lookup, contact history via `search_context`), a structured verdict schema, and a bounded revision loop. And measure it — it might *reduce* accuracy. Reporting that honestly is worth more than claiming it helped.

**13. "Ship publicly after Phase 2" needs an asterisk.** With unverified restricted scopes, only accounts on your OAuth test-user list can authenticate. "Public" means *deployed and demoable by you*, not *strangers can sign up*. → Plan a 90-second demo video (M14). It's the only way most reviewers will ever see it run.

**14. CI is mentioned but never scheduled.** → It goes in M0, where it's nearly free, and gains an eval-regression job at M2.

**15. The timeline is optimistic.** OAuth, deploy, dashboard, and evals each absorb more time than expected. 5–6 weeks part-time is closer to **10–14 weeks**. You said there's no deadline — this is stated so you don't feel behind against a wrong number.

---

## Part 2 — Stack decisions to lock

Unchanged from your plan: Python + FastAPI, LangGraph, Postgres (+`pgvector` later), Next.js dashboard, Telegram, Docker, GitHub Actions.

**Model choices** (these are the pieces your plan left generic):

| Role | Model | Rate (per MTok in/out) | Why |
|---|---|---|---|
| Extraction | `claude-opus-5` | $5 / $25 | Structured outputs + strict tool use; adaptive thinking on by default. The quality floor for the number you'll quote. |
| Classify (is this a meeting?) | `claude-opus-5` at `effort: "low"` | $5 / $25 | Start here so one model covers both paths and your prompt cache stays warm. |
| Reviewer agent (M12) | `claude-opus-5` | $5 / $25 | Needs to catch what the extractor missed; a weaker reviewer is theatre. |

**Cost lever, your call, and it's a portfolio asset either way:** swapping the classify step to `claude-haiku-4-5` ($1/$5) is the obvious economy, since binary classification is easy. Don't do it blind — run both against the M2 eval set and put the accuracy/cost delta in the README. "I measured Haiku vs Opus on the classify step; it cost 5× less for 1.5 points of F1, so I kept it" is exactly the kind of decision interviewers probe for. Measuring it is the deliverable; which one you pick afterwards doesn't matter much.

**API details that will save you a day each:**
- Use `client.messages.parse()` with a Pydantic model — it validates the response against your schema automatically. The canonical parameter is `output_config: {format: {...}}`; the older top-level `output_format` is deprecated.
- **Assistant-turn prefills return a 400** on Opus 5. If you know the "prefill `{` to force JSON" trick, it no longer applies — structured outputs replace it entirely.
- Mark the calendar tool `strict: true` with `additionalProperties: false` and a full `required` list, so tool inputs are schema-validated.
- **Check `stop_reason` before reading `response.content`.** A refusal returns HTTP 200 with `stop_reason: "refusal"` and possibly empty content; code that indexes `content[0]` unconditionally crashes.
- **Prompt caching:** minimum cacheable prefix on Opus 5 is 512 tokens. Order the prompt stable→volatile — system prompt, few-shot examples, and tool definitions first with the cache breakpoint after them; today's date and the email body *last*. A `datetime.now()` interpolated into the system prompt invalidates the cache on every single request. Verify with `usage.cache_read_input_tokens` — if it's zero across repeated calls, something upstream is changing.
- Count tokens with `client.messages.count_tokens()`, never `tiktoken` (it's OpenAI's tokenizer and undercounts Claude by 15–20%).

---

## Part 3 — The modules

Each module: one goal, one deliverable, a binary exit criterion. Ship in order; each leaves the system working.

### Stage A — Foundations

**M0 · Repo & runtime skeleton** — ~0.5 day
Repo, `uv` or Poetry, `ruff` + `mypy`, `pytest`, Dockerfile, `pydantic-settings` config with a typed `.env` schema, GitHub Actions running lint + tests on push.
*Exit:* `docker build` succeeds; CI green on an empty test suite.

**M1 · Google auth & API clients** — ~1 day (budget a full evening for OAuth alone)
GCP project, Gmail + Calendar APIs enabled, OAuth desktop-app flow, refresh token obtained. `GmailClient.list_unread()` / `.get_message(id)` and `CalendarClient.create_event()` / `.freebusy()`. Tokens encrypted at rest (Fernet key from env), **never** in a committed `.env`. Dedicated test calendar; global `DRY_RUN` flag. A `reauth` CLI command and a `token_expires_at` health check.
*Exit:* list 10 real emails and create-then-delete an event on the test calendar, from one script. `DRY_RUN=true` performs zero writes.
*Documented:* the 7-day expiry, why it exists, and how you handle it.

**M2 · Eval harness & golden dataset** — ~1 day · **moved before the extractor**
30–40 emails with hand-labeled expected outputs. Anonymization script: real names/addresses/companies → consistent synthetic replacements. Committed fixtures are scrubbed; raw emails gitignored. Field-level scorer: `is_meeting` (precision/recall/F1), `start`/`end` (exact-to-minute in UTC), `attendees` (set F1), `title` (fuzzy). `make eval` prints a table and writes a timestamped JSON result.
*Exit:* harness runs against a stub extractor returning nulls, and correctly reports ~0%.
*Coverage to include deliberately:* relative dates ("next Tuesday"), timezone-crossing invites, all-day events, cancellations, forwarded threads, and **non-meeting emails** (newsletters, receipts) — false positives are the failure mode users actually notice.

### Stage B — The agent core

**M3 · Extraction & classification** — ~2–3 days
Pydantic schema: `is_meeting`, `title`, `start_utc`, `end_utc`, `timezone` (IANA), `attendees[]`, `location`, `confidence`, `reasoning`. Date grounding: inject current UTC time, the user's IANA zone, and today's weekday name into the prompt — models get "next Tuesday" wrong without it. Two-step: cheap classify → full extract only when `is_meeting` is true. Prompt structured for caching (stable prefix, volatile suffix).
*Exit:* **baseline eval number recorded, committed, and frozen.** This is the "71%" in your future sentence.

**M4 · Action layer & idempotency** — ~1 day
`create_calendar_event` as a model-callable strict tool. `processed_messages` table (Gmail message ID PK, status, event ID, timestamps). Gmail `historyId` cursor for incremental fetch. Free/busy conflict check before proposing. Everything still behind `DRY_RUN`.
*Exit:* run the poller twice over the same inbox → **zero duplicate events, zero duplicate LLM calls.**

**M5 · LangGraph orchestration & durable state** — ~3 days
Graph: `fetch → classify → extract → (review) → await_approval → act → log`. `langgraph-checkpoint-postgres` as the checkpointer. `interrupt()` at the approval node. `thread_id` = Gmail message ID. Retry-with-backoff on Gmail/LLM calls; explicit error node and dead-letter state.
*Exit:* **kill the process mid-approval, restart it, resume the thread — the event is still created correctly.**

**M6 · Telegram interface & human-in-the-loop** — ~2 days
Telegram *webhook* on the FastAPI app. Chat-ID allowlist. Inline keyboard: Confirm / Edit / Cancel. Callback resumes the graph via `Command(resume=...)`. Edit flow: free-text correction → re-extract with the correction as context. Approval messages render the proposed event in the user's local timezone.
*Exit:* real email → Telegram card → Confirm → event on the **real** calendar. `DRY_RUN` comes off here.

**M7 · Deploy** — ~1 day
Docker image, Railway or Fly, managed Postgres (Neon/Supabase), secrets via platform env, APScheduler for the Gmail poll, `/health` endpoint, Telegram webhook URL + Google OAuth redirect URI pointed at the deployed host.
*Exit:* runs unattended for 48 hours, processes real mail, survives a redeploy mid-approval. Token-expiry warning fires on schedule.

> **Ship it here.** This is the demoable artifact. Record the demo video (M14) now, while the system is small enough to explain in 90 seconds.

### Stage C — The differentiator

**M8 · Observability** — ~2–3 days · *highest value per hour in the whole plan*
`runs` and `spans` tables: `trace_id`, `node`, `model`, `input`/`output` (PII-redacted), `latency_ms`, `tokens_in`/`out`/`cached`, `cost_usd`, `status`, `retry_count`. A single pricing config keyed by model ID, with token counts stored raw so cost can be recomputed when rates change. Capture via LangGraph callbacks rather than manual instrumentation at each call site.
*Exit:* every run visible in Postgres with an accurate cost; daily spend reconciles against the Anthropic console within a few percent.
*Optional depth:* stand up Langfuse or LangSmith alongside for a week and write up why you kept (or dropped) the hand-rolled version.

**M9 · Next.js dashboard** — ~3 days
Runs list with status filter; trace drill-down (per-node timing, tokens, cost); cost-per-day chart; eval-score-over-time chart; failed-runs view with error detail. Auth: single shared token or basic auth — **do not build a user system**, it teaches nothing here and costs days.
*Exit:* deployed, and you can diagnose a real failure end-to-end from the dashboard without opening a SQL client.

### Stage D — Retrieval

**M10 · RAG ingestion pipeline** — ~3 days
Thread fetch → clean (strip quoted replies, signatures, HTML) → chunk with metadata (thread ID, participants, date) → embed → upsert into `pgvector`. Content-hash dedupe so re-runs are idempotent. Every run writes stats to the M8 tables.
*Exit:* re-running ingestion over the same mailbox inserts zero new rows.

**M11 · Hybrid retrieval & the `search_context` tool** — ~2 days
`pgvector` HNSW index + Postgres `tsvector` BM25, fused with Reciprocal Rank Fusion. Exposed to the agent as a `search_context` tool ("who is Ahmed and what did we agree last time?"). Optional hosted reranker behind a flag.
*Exit:* the extractor can answer an attendee-identity question it demonstrably could not before.

**M12 · Retrieval evaluation** — ~1 day
20–30 labeled queries with known-relevant chunks. Recall@k and MRR. Measure vector-only vs. BM25-only vs. RRF vs. RRF+rerank.
*Exit:* a committed results table. **Report it honestly even if RRF beats the reranker** — that's the interesting result.

### Stage E — Multi-agent & polish

**M13 · Reviewer agent** — ~2–3 days
A genuinely separate agent with its own system prompt and its own tools (`freebusy_check`, `search_context`). Structured verdict: `{approve | revise | reject, issues[], corrected_fields{}}`. Bounded revision loop (max 2 iterations, hard-capped in the graph). Catches timezone errors, double-bookings, ambiguous dates, wrong attendees.
*Exit:* eval delta measured and published — **including if it's negative or neutral.**

**M14 · Scheduled pipeline & portfolio packaging** — ~2 days
Move ingestion to APScheduler (or Prefect 3 if you want the orchestrator experience — **not Airflow**), with backfill vs. incremental modes and run stats. Then: README with an architecture diagram (Mermaid — renders natively on GitHub), the eval progression table, a "what broke and how I fixed it" section, cost per 100 emails, and a 90-second demo video.
*Exit:* someone who has never seen the repo understands what it does and what you learned, in under three minutes.

---

## Part 4 — Dependency graph

```
M0 ─┬─► M1 ─┬─► M3 ─► M4 ─► M5 ─► M6 ─► M7 ──► M8 ─┬─► M9
    │       │                                       │
    └─► M2 ─┘                                       └─► M10 ─► M11 ─► M12 ─► M13 ─► M14
```

- **M1 and M2 are parallel.** When OAuth consent screens make you want to quit, go label emails instead.
- **M8 can start any time after M5** but is most valuable right after deploy, when real traffic exists to observe.
- **M13 depends on M11**, because a reviewer without `search_context` is just a second opinion from the same information.

---

## Part 5 — Interfaces (the seams that make this modular)

Define these early; they're what let a module be replaced without touching its neighbours.

```python
# app/contracts.py — stable across all modules
class EmailMessage(BaseModel):
    id: str; thread_id: str; subject: str; body_text: str
    sender: str; recipients: list[str]; received_at: datetime

class ExtractionResult(BaseModel):
    is_meeting: bool
    title: str | None; start_utc: datetime | None; end_utc: datetime | None
    timezone: str | None            # IANA, e.g. "Asia/Karachi"
    attendees: list[str]; location: str | None
    confidence: float; reasoning: str

class ActionResult(BaseModel):
    status: Literal["created", "skipped_duplicate", "rejected", "failed"]
    event_id: str | None; error: str | None
```

The eval harness scores `ExtractionResult`, the graph passes it between nodes, the dashboard renders it, and the reviewer agent amends it. Get this file right and the module boundaries hold.

---

## Part 6 — Risks & cut list

| Risk | Mitigation |
|---|---|
| OAuth consent screen eats two evenings | Timeboxed in M1; M2 is parallel work when you stall |
| 7-day token expiry surprises you in production | Health check + Telegram alert + documented `reauth` (M1, M7) |
| Real emails leak into a public repo | Anonymization is part of M2's definition of done, not an afterthought |
| LLM costs run away during eval iteration | Prompt caching from M3; cost tracking from M8; `count_tokens` before large batches |
| Dashboard scope creep | Five views, shared-token auth, hard stop |
| Ingestion pipeline over-engineered | APScheduler until it visibly fails. Airflow is not on the table |

**If you need to cut:** M12 → M13 → M9 (in that order). Never cut M2 or M8 — they *are* the differentiator. Most candidates ship a working demo; almost none can show you the eval progression and the cost-per-run chart.

---

## Part 7 — Verification

Per-module exit criteria are listed above and are all binary. System-level checks:

- **`make eval`** — runs the golden set, prints per-field scores, writes a timestamped JSON. Runs in CI on every push touching `app/extraction/**`, and fails the build on a >3-point regression.
- **Idempotency check** — `python -m app.jobs.poll --once` twice in a row; assert zero new rows in `calendar_events`.
- **Durability check** — start an approval, `docker compose restart app`, click Confirm; assert the event is created.
- **Cost reconciliation** — `SELECT date, SUM(cost_usd) FROM runs GROUP BY 1` against the Anthropic console for the same window.
- **Cold-start check** — deploy from a clean database and clean token store; document every manual step required. If it isn't reproducible, the README is wrong.

---

## Note on execution

This document is the master plan. On implementation I'd split Parts 3–5 into per-module files under `docs/plans/M00-skeleton.md` … `M14-packaging.md`, each carrying its own goal, interface, exit criterion, and running notes — so a module can be picked up cold without re-reading the whole roadmap. This file stays as the index and dependency graph.

First action: rename `D:\voice_agent` to something matching the actual project (`mailagent`), then M0.
