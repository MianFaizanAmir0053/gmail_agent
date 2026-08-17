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

**Status: Part A done, Part B done except the video and screenshots.** Those
need a person at a screen recorder, so M14 stays open rather than being marked
complete with the highest-value artifact missing.

### Part A — the pipeline

Incremental and backfill are one pipeline with two queries, not two pipelines
with a watermark between them. That is only possible because M10's content-hash
dedupe makes a re-run over an overlapping window genuinely free — one `SELECT`,
zero embeddings. So the incremental window (2 days) is deliberately **wider than
the interval** (24 hours): the overlap costs nothing and it means a single
missed run cannot leave a permanent hole in the corpus. A stored cursor would
have to be updated transactionally with the inserts to be correct, and would buy
nothing here.

Verified live: `python -m app.jobs.ingest_job` over the last two days found 8
messages, produced 9 chunks, inserted **0**, and cost **$0**.

Ingestion is the only scheduled job that spends money without anyone having
asked for anything — a poll that finds no mail is free; an ingest that finds new
mail always embeds it. So `INGEST_ENABLED` defaults to false, unlike the poller.

Airflow was not adopted, and the reasoning is in `app/jobs/ingest_job.py` rather
than only here: a scheduler, a webserver and a metadata database to orchestrate
one daily job over a few dozen emails. "I looked at Airflow and it was wrong for
this scale" is a better sentence than the CV line.

### Part B — the table the plan asked for does not exist, honestly

The plan sketched a progression: baseline, `+ date grounding`, `+ timezone in
schema`, `+ few-shot examples`. That shape assumes those arrived as successive
fixes to a weak first attempt.

They did not. **Because the eval set was built before the extractor**, the
failure modes were known before a line of extraction code was written — so date
grounding and an IANA-zone field were in the *first* version. The frozen 92.9%
already includes them. There is no lower "before" number, and manufacturing one
to fill a nicer table would be precisely the dishonesty the harness exists to
prevent.

This is worth saying out loud in the README, because the missing table is
evidence *for* the sequencing decision rather than against it.

### The cost number needed a caveat, not a headline

`$0.025 per 100 emails` is real and comes from the `spans` table — but every
traced production run so far has been a non-meeting, so it stopped after the
cheap classify call. It is a **triage-only** figure. Quoting it as the cost per
email would be wrong by however much extraction adds, which is not yet known.
Ingestion cost carries the separate caveat that the embeddings API reports no
usage at all, so it is estimated rather than measured.

### Mermaid, not an image

Renders natively on GitHub with no image hosting, no build step, and it stays
diffable. `graph` had to be renamed to `flow` as a subgraph id — it collides
with the `graph` keyword.

### Still outstanding

- **90-second demo video.** Email arrives → Telegram card → Confirm → event on
  the calendar → the dashboard trace for that run. Not optional: unverified
  restricted scopes mean nobody but the owner can authenticate against a
  deployed instance, so this is the only way a reviewer sees it run. It also
  depends on M06/M07, since the Telegram half needs a reachable network.
- **Screenshot of the approval card**, for the top of the README.

### Verified

```
ruff / mypy --strict   clean
pytest                 459 passed
ingest_job (live)      8 messages, 9 chunks, 0 inserted, $0
```
