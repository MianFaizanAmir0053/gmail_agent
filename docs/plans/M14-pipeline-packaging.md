# M14 · Scheduled pipeline & portfolio packaging

**Est.** 2 days · **Depends on** M13 · **Blocks** nothing

## Goal

Ingestion runs on a schedule, and the repo explains itself to someone who has never seen it.

## Part A — Scheduled pipeline

- Move M10 ingestion to a scheduled job with backfill and incremental modes
- Run stats (docs processed, chunks created, embedding cost, duration) into the M08 tables
- Failure alerting via the existing Telegram channel

**Use APScheduler.** If you specifically want an orchestrator on the résumé, use **Prefect 3** — it's a decorator and a `.serve()` call.

**Do not use Airflow.** It needs a scheduler, a webserver, and its own metadata database to orchestrate a pipeline that processes a few dozen emails a day. It will eat a week and teach you Airflow-the-installation rather than pipelines. Adopting infrastructure for the résumé line alone is visible in interviews and reads badly — the ability to say "I evaluated Airflow and it was wrong for this scale" is worth more than the line itself.

## Part B — Packaging (this is the part that gets you interviews)

### README structure

1. **What it does** — two sentences and a screenshot of the Telegram approval card
2. **Demo video** — 90 seconds, embedded at the top
3. **Architecture diagram** — Mermaid, renders natively on GitHub, no image hosting
4. **Eval results** — the progression table, with what changed at each step
5. **Cost** — dollars per 100 emails, from real M08 data
6. **What broke and how I fixed it** — the most-read section
7. **Known limitations** — the 7-day token expiry, unverified-scope constraints, single-user design
8. **Running it yourself** — clean-machine setup steps

### The eval table

| Change | Accuracy | Notes |
|---|---|---|
| Baseline (M03) | __% | frozen, no date grounding |
| + date grounding | __% | |
| + timezone in schema | __% | |
| + few-shot examples | __% | |
| + reviewer agent (M13) | __% | |

This table is the single highest-value artifact in the repo. One sentence from it — "extraction accuracy went from X% to Y% after date grounding and a reviewer agent" — outweighs the entire feature list.

### The demo video is not optional

Unverified restricted scopes mean only accounts on your OAuth test-user list can authenticate. **Nobody else can log into your deployed app.** The video is how reviewers will see it work. 90 seconds: email arrives → Telegram card → Confirm → event on the calendar → the dashboard trace for that run.

### "What broke" section

Write it honestly. Strong candidates:

- The 7-day OAuth token expiry and what it taught you about verification tiers
- Why the eval set had to come before the extractor
- Timezone handling as an accuracy problem, not a formatting problem
- Whatever the reviewer-agent measurement actually showed
- Prompt-cache misses and what was invalidating the prefix

This section is what separates a tutorial project from evidence of judgment.

## Exit criterion

Someone who has never seen the repo understands what it does and what you learned, in under three minutes.

## Running notes

_(record what surprised you here)_
