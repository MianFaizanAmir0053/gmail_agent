# M15 · Go live and measure

**Est.** 5 days of work, then 8 days of unattended running · **Depends on** M14, the price-table fix · **Blocks** M16, M17, M20

First module of v2. Plan and decisions: [`ASSISTANT-PLAN.md`](../../ASSISTANT-PLAN.md).

## Goal

Run the existing pipeline unattended in production — on the paid Gemini tier,
with the weekly OAuth expiry either proven gone or explicitly handled — and
measure the real volume, loose ends and cost that every later module is
budgeted against.

## Why this comes first

- **Nothing has run in production.** M06 and M07 are still open. Three adversarial reviews of this spec found latent v1 bugs that surface only once the system runs unattended:
  - the image ignores the lockfile;
  - retries are invisible;
  - health reports `ok` while polls fail;
  - a redeploy strands messages;
  - after an *Edit*, a *Cancel* or *Confirm* loops back to extraction instead of finishing.
- **The budget is unmeasured.** The price table used placeholder rates (being fixed in a separate session), and Gemini Flash prices double on 1 Jan 2027.
- **Real mail is going through the free tier.** Free-tier prompts may be used to improve Google's products, including by human reviewers.
- **The seven-day refresh-token expiry blocks unattended operation.** It reportedly comes from "Testing" status. M15 proves or disproves that without betting the eight-day window on it.
- **v2's first job is loose ends.** That it costs the owner real time needs a number before M21 is worth building.

## Scope

**Part A — harden and deploy:** a reproducible single-instance deploy; liveness
that cannot pass while the pipeline is dead; visible retries; OAuth evidence
with a standby token; data retention; the v1 fixes above; observe mode.

**Part B — measure and decide:** mail volume and loose ends, with the proxy's
precision and miss rate measured; cost at 2026 and 2027 rates, reconciled
against the bill; go/no-go against thresholds committed before measuring.

**Out:**
- any channel (M16);
- binding approvals to their context and every new action (M17);
- stripping one-time codes and re-enabling ingestion (M18);
- how the poller reads mail (M20);
- extracting obligations (M21);
- the README cost section and `app/obs/pricing.py`, both owned by the price-table session.

Build Part A, then Part B. Each part has its own tasks and checkpoints.

---

## Part A — Harden and deploy

### A1. Reproducible image, single instance

- **Lockfile.** The Dockerfile copies `uv.lock` and runs `uv sync --locked --no-dev`, which also fails if the lock does not match `pyproject.toml`. The uv image is pinned to a version.
- **Version bounds.** `langgraph`, `langgraph-checkpoint` and `langgraph-checkpoint-postgres` get floors at the locked versions and caps below the next major version. The checkpoint format must not change under parked proposals.
- **Writable paths.** The Dockerfile creates `/app/secrets`, owned by `appuser`, so boot-time secret writing (`app/bootstrap.py`) cannot fail on permissions. Every path variable is absolute.
- **One machine.** Deploy with `fly deploy --ha=false` and confirm `fly scale count 1`.
- **Clean shutdown.** In `fly.toml`:
  - `kill_timeout` is raised above the longest tick;
  - the scheduler stops claiming messages once shutdown begins;
  - at boot, `claimed` ledger rows older than one hour are marked `FAILED` ("stranded by shutdown"). Otherwise they would never be polled, listed or purged again.
- **Database connection.** Only direct or session-mode connection strings are allowed. If only a transaction-mode pooler exists, one shared connect helper sets `prepare_threshold=None`, which disables prepared statements in psycopg (0 would prepare everything). The helper covers the checkpointer, the ledger, migrations and `job_runs`.

### A2. Observe mode

| Variable | Value | Why |
|---|---|---|
| `DRY_RUN` | `true` | No Calendar writes; it stays `true` until M17 binds approvals to their context |
| `RUN_SCHEDULER` | `true` | Polling on schedule is what is being proven |
| `TEST_CALENDAR_ID` | the throwaway calendar | `graph_session` refuses to start without it |
| `INGEST_ENABLED` | `false` | Keeps one-time codes and reset links out of the corpus until M18 |
| `SEARCH_CONTEXT_ENABLED` | `false` | The production corpus is empty |
| `REVIEWER_ENABLED` | `false` | Unchanged |
| `TELEGRAM_*` | unset | The channel arrives in M16 |
| `ALLOWED_CHAT_IDS` | `[]` | Unchanged |

**Logging.** The web process configures logging at startup; today only the
ingest job does. In production, `poll` prints message ids and statuses only,
never extracted titles.

### A3. Liveness

- **`job_runs` table** (migration `005_job_runs.sql`). One row per scheduled tick, with job, start, finish, success, counts and error. A poll tick succeeds only if it raised nothing **and** marked no message `FAILED`.
- **`/health` reads in-memory state.** It uses the last tick's result and time, and the token state. It makes no database round-trip per request: every tick already connects to the database, so tick success is the database check.
  - It returns **503** when no poll has succeeded within three intervals (with a one-interval grace after boot), or when every Google token is expired.
  - It never echoes exception text.
- **An external uptime monitor** on a free tier calls `/health` every 5 minutes and emails the owner on failure. It is an external service, so ask first.
- **The database is budgeted as always on.** Ten-minute polls keep it awake, so a scale-to-zero free allowance is not assumed.

### A4. Retries made visible

`call_with_retry` absorbs 429s silently. Retries are counted in `SpanUsage` and
written to `spans.retry_count`, a column that exists but has never been
written. Exhausted retries already produce error spans.

### A5. OAuth: evidence, with a standby token

**Owner steps.**

1. In Cloud console, open *OAuth consent screen → Audience* and press **Publish app**. Keep the user type External and do not submit for verification (personal use, fewer than 100 users).
2. Generate a **production-only** `FERNET_KEY`. The M07 checklist's "not the dev one" wins over DEPLOY.md's "identical".
3. Run `.\tasks.ps1 reauth --minted-under production` locally, with the production key and a separate token path. Continue past the unverified-app warning via *Advanced*. `tasks.ps1` forwards arguments to `reauth`, which it does not today, and the flag is required.
4. Upload that file and key as base64 secrets (DEPLOY.md §3 is corrected; it currently uploads the dev token).
5. **Around day 4**, mint a second production token the same way and upload it as `GOOGLE_TOKEN_STANDBY_B64`.

**Code.**

- The token metadata stores `minted_under`, next to `issued_at`.
- `TokenStore.save()` keeps every metadata field on routine refreshes.
- Every refresh outcome is written to `job_runs` (`job = 'token_refresh'`) and keyed to the token's `issued_at`. An old token's failure therefore never marks a new token expired.
- If the primary token fails with `invalid_grant`, that failure is recorded — it is the evidence — and the store fails over to the standby. Liveness continues, and the window does not restart.
- Token health, per token:

| State | Condition | Behaviour |
|---|---|---|
| `testing` | `minted_under = testing` | Today's countdown and two-day warning |
| `production-unconfirmed` | Minted under production, and no successful refresh recorded after `issued_at` + 7 days | Countdown kept, labelled as expected to lapse |
| `production-confirmed` | A successful refresh recorded after `issued_at` + 7 days | No countdown |
| `expired` | Last refresh failed with `invalid_grant` | Failover. `/health` returns 503 only when no token is usable |

**If the primary lapses.** That proves "In production" does not remove the
seven-day expiry. The fallback is Testing status with a weekly `reauth`, or a
Workspace mailbox, decided with the owner. Because the standby carries the
window, the rest of M15 still completes.

### A6. Retention and data location

Every polled message's full body, one-time-code mail included, is stored in its
graph checkpoint, and nothing deletes checkpoints today.

- **Hourly purge.** Delete checkpoints for threads whose ledger status is terminal (`SKIPPED`, `REJECTED`, `CREATED`), using the saver's thread deletion. `FAILED` threads keep theirs for 7 days. Parked threads are never touched.
- **Model text in the ledger.** `skip` stores the model's reasoning, which quotes the email, in `processed_messages.error`. The purge clears that text on terminal rows older than 7 days.
- **Location.** Mail bodies at rest live in the **database provider's region**. The app region in `fly.toml` (`fra`) decides only where they pass through. Both are chosen by the owner.

### A7. Parked proposals

- **The v1 routing bug is fixed first.** `await_approval` never clears `correction`, so after an *Edit*, a later *Cancel* or *Confirm* is routed back to `extract` (`app/graph/build.py`, `_decision`). Non-edit decisions clear it. New tests: *Edit* then *Cancel* ends rejected, and *Edit* then *Confirm* acts exactly once.
- **Day 1.** The owner sends a meeting email from another account, so a proposal parks. That sender is listed in the local `MEASURE_EXCLUDE_SENDERS` so it cannot distort Part B.
- **Mid-window redeploy.** The proposal is still listed afterwards: the resume-after-redeploy check M07 never ran.
- **Dry-run state recorded.** The proposal payload stores the `DRY_RUN` value in force when it parked. M17 makes `act` refuse a mismatch, and `DRY_RUN` may not be turned off before that exists.
- **End of M15.** Every parked proposal is cancelled with a new `sweep` decision. The ledger reason reads "swept: observe mode ended", so M24 never counts sweeps as the owner's rejections.

### A8. Paid tier

**Owner steps, before the first deploy.** A message that fails on day one stays
`FAILED`, because the ledger never offers it again.

1. Enable billing on the Google Cloud project behind the Gemini API key, and confirm a paid tier in AI Studio.
2. Set a Cloud Billing budget alert at **$50 minus the hosting and database list prices**. The alert sees Google Cloud spend only.

---

## Part B — Measure and decide

### B1. Two jobs, two places

| Job | Runs | Reads | Writes |
|---|---|---|---|
| `measure mail` | Locally, with the owner's token | Gmail metadata only, over **14 days ending at least 48 hours before the run** | Counts to `results/` |
| `measure mail --label` | Locally, **by the owner only** | The same flagged set, shown on screen | Only the resulting counts |
| `measure cost` | On the instance, through `fly ssh console` | Spans in the unattended window | Counts to stdout |

**Guards:**
- `measure mail` refuses to run while `USER_TIMEZONE` is the default `UTC`, unless `--timezone` is given.
- `--label` refuses to run without an interactive terminal. It is never run by an agent: its screen output would land in a transcript on disk.
- `measure cost` refuses a localhost `DATABASE_URL`. Evals run outside the unattended window, so they cannot pollute it.
- Every output prints its windows, time zone, models, `PRICING_CHECKED_ON`, the random seed, sample sizes and the git commit.

### B2. Volume and loose ends

**What it reads.** Metadata only:
- `format=metadata` on messages and threads;
- the headers From, To, Cc, Subject, List-Unsubscribe, Auto-Submitted and Precedence;
- label ids and `internalDate`.

It makes no model calls, needs nothing beyond `gmail.readonly`, and paginates
fully. Subjects are used for filtering and labelling only, and never written.

**Definitions.**
- **Owner-authored:** has the `SENT` label. `DRAFT` messages are ignored.
- **Addressed to the owner:** To or Cc matches the profile address (`users.getProfile`) or an `OWNER_ALIASES` entry, after normalising Gmail addresses (dots, `+tags`, `googlemail.com`).
- **Automated:** any of:
  - List-Unsubscribe present;
  - Auto-Submitted present with a value other than `no`;
  - Precedence `bulk` or `list`;
  - a no-reply sender;
  - a calendar notification;
  - an excluded sender.
- **Flagged thread:** a thread whose first qualifying inbound message has no owner-authored reply in that thread within 48 hours. A qualifying message is in the Primary category, not automated, and addressed to the owner. **The unit is the thread**; a thread counts once.

**Outputs (counts only):**
- inbound and sent messages per day, with the Primary, other-category, automated and no-category split (Primary depends on category labels);
- flagged threads over the 14 days, and the weekly rate (total ÷ 2);
- how many flagged threads were answered later;
- an informational snapshot of open flagged threads with a declared 30-day lookback. The snapshot is not used in the decision.

**Bias, measured in both directions.** `--label` draws random samples with a
printed seed:

- **Precision.** 40 flagged threads, or all if fewer. The owner answers two questions per thread: *did it need a reply?* and *was it still unanswered by any channel at 48 hours?* Precision is the share with two yeses.
- **Misses.** 20 unflagged, human, inbound threads. The owner answers: *was this an unanswered ask?* That gives the miss rate.

Both are reported with Wilson 95% intervals. The first complete run is the
decision run; later runs are reported but never replace it.

### B3. Cost

`measure cost` is **blocked until the price-table fix merges**.

- It passes raw span columns (input, cached, output and thinking tokens) to the merged pricing API at two reference dates, 15 Dec 2026 and 15 Jan 2027.
- It never sums the stored `cost_usd`, which was fixed at write time.
- It aborts on any unpriced model.
- It never patches pricing itself. If cached tokens are still billed twice after the merge, that goes back to the price-table session.

| Line | Source | Label |
|---|---|---|
| Triage | Classify spans ÷ messages classified | Measured |
| Meeting rate | Classified messages that went on to extraction | Measured |
| Extraction | Extract spans ÷ extractions, if there are at least 20 | Measured. Otherwise *unmeasured*, plus an upper bound from measured email size, the extraction prompt size and the 4,096-token output cap |
| Embeddings | Local corpus chunks per message and tokens per chunk × window mail matching ingestion's query (`DEFAULT_QUERY`) × the configured embedding model's published rate | *Estimate* |
| Retries | `spans.retry_count` total | Measured |
| Hosting and database | Plan list prices, database always on | List price |
| **Projection** | Inbound per day × (triage + meeting rate × extraction) + embeddings, × 30, at both dates | *Observe-mode v1 projection*. Estimate lines are added for what M18 turns back on (`search_context`, ingestion). v2's planner is unknown until M19 |

**Reconciliation.** The window's spans are priced at 2026 rates and compared
with Cloud Billing's Gemini charges for the same days. A difference above 15%
is explained in the running notes.

### B4. Go/no-go thresholds

These are committed to git **before day 0**, and the results cite that commit.

- **Loose ends go:** the lower bound of the corrected weekly rate is at least **5 threads per week**. The corrected rate is the weekly flagged rate × the lower bound of the precision interval. Otherwise M21 is re-planned before M16's ledger view is built. The miss rate is reported alongside.
- **Budget holds:** the observe-mode v1 projection, plus its M18 estimate lines, is at most **$15/month at 2027 rates**, and hosting plus database list prices are at most **$10/month**. Otherwise the model mix is re-planned before M19.

---

## Deliverables

**Part A:**
- **Image and deploy:** the Dockerfile (`--locked`, pinned uv, `/app/secrets`); version bounds; `fly.toml` (`kill_timeout`); the shutdown claim guard and the boot-time stranded-row recovery; the shared connect helper.
- **Liveness:** `005_job_runs.sql`, tick recording and the in-memory `/health`.
- **Retries:** counted into `spans.retry_count`.
- **Tokens:** `reauth --minted-under` (with `tasks.ps1` forwarding arguments), metadata-preserving `save()`, refresh outcomes keyed to `issued_at`, standby failover, and the per-token states.
- **Retention:** the hourly purge, including clearing reasoning text.
- **v1 fixes:** the routing fix with tests; `dry_run` recorded in proposals; the `sweep` decision.
- **Logging:** configured in the web process; production `poll` output limited to message ids and statuses.
- **`docs/DEPLOY.md`:**
  - billing first;
  - `--ha=false`;
  - direct or session connection strings;
  - absolute paths;
  - the production key, primary token and standby token;
  - the observe-mode variables;
  - the uptime monitor;
  - `fly ssh console` usage.

**Part B:**
- `app/jobs/measure.py` with its `mail` and `cost` subcommands and guards.
- The `OWNER_ALIASES` and `MEASURE_EXCLUDE_SENDERS` settings, and a `measure` entry in `tasks.ps1`.
- `results/volume-YYYY-MM-DD.md`, reviewed for personal data before committing.

**Optional, unblocked by the paid tier:** the M13 reviewer eval delta, run outside the unattended window.

## Commands

These are the agent-runnable ones. `measure mail --label` is for the owner only
and is deliberately absent.

```powershell
.\tasks.ps1 check                                   # lint, format, mypy --strict, tests
.\tasks.ps1 migrate                                 # applies 005_job_runs.sql locally
.\tasks.ps1 measure mail --since <UTC> --until <UTC> --timezone <IANA>
```

**Owner only**, in their own terminal:

```powershell
.\tasks.ps1 reauth --minted-under production        # day 0 primary, day ~4 standby
.\tasks.ps1 measure mail --since <UTC> --until <UTC> --timezone <IANA> --label
```

```bash
fly deploy --ha=false
fly scale count 1
curl -i https://<app>.fly.dev/health
fly ssh console -C "sh -c 'cd /app && python -m app.jobs.approve --list'"
fly ssh console -C "sh -c 'cd /app && python -m app.jobs.measure cost --since <UTC> --until <UTC>'"
```

## Testing

- **Deploy.** The image builds with `--locked`. A shutdown during a tick leaves no `claimed` row that boot does not recover.
- **Liveness.**
  - A tick that marks any message `FAILED` records `ok = false`.
  - `/health` returns 503 after three intervals without success, but not within the boot grace, and never makes a database call.
  - The gap query catches gaps at the start, middle and end of a window.
- **Retries.** A call that succeeds after two 429s writes `retry_count = 2`.
- **Tokens.**
  - Metadata survives a routine save.
  - Confirmation needs a recorded successful refresh after `issued_at` + 7 days.
  - An old token's failure never marks a newer token expired.
  - `invalid_grant` on the primary fails over to the standby and records the primary as expired.
  - With both expired, `/health` returns 503.
- **Purge.** Terminal threads lose their checkpoints. Parked threads and recent `FAILED` ones keep theirs. Reasoning text is cleared after 7 days.
- **Routing.** *Edit* then *Cancel* ends rejected; *Edit* then *Confirm* acts once; `sweep` records the sweep reason.
- **Measurement:**
  - owner detection by `SENT`;
  - drafts ignored;
  - address normalisation;
  - every automated rule, including Auto-Submitted `no` treated as human;
  - the thread as the unit;
  - the 48-hour rule against a fixed clock;
  - answered-later counting;
  - the no-category count;
  - full pagination;
  - excluded senders;
  - the time-zone guard;
  - the TTY guard on `--label`;
  - the localhost guard on `cost`;
  - no header values in any written output, asserted against seeded fixtures.
- **Cost.**
  - Raw columns are priced at both dates.
  - An unpriced model aborts.
  - Fewer than 20 extraction spans gives *unmeasured* plus the bound.
  - The projection uses the meeting rate.
- **Runtime:**
  - the day-0 `approve --list` over ssh;
  - the day-1 test proposal;
  - the mid-window redeploy;
  - the uptime monitor's history;
  - the day-8 token states;
  - the bill reconciliation.

## Boundaries

- **Always:**
  - keep `DRY_RUN=true`;
  - enable billing before the first deploy;
  - commit the thresholds before day 0;
  - commit counts only, reviewed before committing;
  - run production jobs through `fly ssh console`.
- **Ask first:**
  - the host, the database provider, the plan, and **both regions**;
  - the uptime monitor;
  - a purge schedule other than the one above;
  - re-enabling ingestion or `search_context` before M18;
  - any cost figure outside `results/`.
- **Never:**
  - send real mail through a free-tier key;
  - point the local `.env` at the production database;
  - commit addresses, subjects, ids or bodies;
  - run `--label` from an agent;
  - change OAuth scopes;
  - edit `app/obs/pricing.py` or the README cost section;
  - turn `DRY_RUN` off in M15.

## Exit criterion

All five must hold.

1. **Unattended.** Over eight consecutive days:
   - `job_runs`, sentinels included, shows no gap between successful polls longer than three intervals;
   - there are no `ok = false` ticks and no `FAILED` rows, unless each is explained in the running notes;
   - the uptime monitor reports no outage longer than 30 minutes;
   - no manual intervention was needed beyond the planned standby upload;
   - the primary token reached `production-confirmed`. Only this token clause may instead be satisfied by the recorded, explicitly chosen fallback.
2. **Paid.** Cloud Billing shows paid Gemini usage. There are no quota-error failures, the retry total is reported, and the reconciliation is within 15% or explained.
3. **Priced.** The price-table fix is merged and its rates match the official pricing page on the day of measurement. Cached tokens are billed once, and the embedding rate used matches the page.
4. **Measured.** `results/volume-*.md` is committed with every field B1 requires:
   - volume and the flagged weekly rate;
   - precision and miss rate with their intervals;
   - the labelled cost table;
   - the projection at both dates;
   - the reconciliation.
5. **Decided.** Both go/no-go results, computed against the committed thresholds, are in the running notes, and every parked proposal has been swept.

## Open questions

- Host: Fly, which the existing docs cover, or Railway, which has a config in the repo?
- Postgres provider and plan: it must offer a direct or session-mode connection and stay always on within budget.
- Regions for the app and the database?
- Uptime monitor service?

## Running notes

### 2026-09-30 · Task 1: hosting comparison

All prices were fetched on 2026-09-30 from official pages. Monthly figures are
our own arithmetic from list prices.

| | Fly.io (app) | Railway Hobby (app + database) | Supabase (database) | Neon (database) |
|---|---|---|---|---|
| Cost for this workload | Pay-as-you-go only; no free allowance for new organisations ([plans](https://docs.fly.io/about/discontinued-plans)). shared-cpu-1x in `sin`: 512 MB ≈ $4.05, 1 GB ≈ $7.23 per 30 days ([pricing](https://docs.fly.io/about/pricing)) | $5/month including $5 of usage; RAM $10/GB-month ([plans](https://docs.railway.com/reference/pricing/plans)). App plus Postgres ≈ $6–8.5 | Free $0; Pro $25, which is over budget ([pricing](https://supabase.com/pricing)) | Free cannot stay always on; paid always-on at 0.25 CU ≈ $19 ([pricing](https://neon.com/pricing)) |
| Nearest region | Singapore; no Mumbai ([regions](https://docs.fly.io/reference/regions)) | Singapore; no India ([regions](https://docs.railway.com/reference/deployment-regions)) | Mumbai or Singapore ([regions](https://supabase.com/docs/guides/platform/regions)) | Singapore ([regions](https://neon.com/docs/introduction/regions)) |
| Connection | Outbound IPv6 | Direct, over the private network | Direct over IPv6 only; the session pooler on `:5432` works over IPv4 ([docs](https://supabase.com/docs/guides/database/connecting-to-postgres)) | Direct host, or a transaction-mode pooler |
| Gotchas | The first deploy creates 2 machines; `kill_timeout` defaults to 5 s; `fly launch` enables auto-stop | Postgres is self-managed on a volume (`pgvector/pgvector:pg16`); volume backups exist ([backups](https://docs.railway.com/reference/backups)); a hard usage limit takes every service offline ([limits](https://docs.railway.com/reference/usage-limits)) | Free has no automatic backups, turns read-only past 500 MB, and pauses after a week of inactivity | Free scales to zero after 5 minutes and caps at 100 CU-hours per month |

**Uptime monitors.** Better Stack Free raises an incident on any non-2XX
response and adds heartbeats ([docs](https://betterstack.com/docs/uptime/monitor-types)).
Whether UptimeRobot Free treats a 503 as down is unverified.

**Recommendation:** Railway Hobby in Singapore, running the app and
`pgvector/pgvector:pg16` on a private network (about $6–8.5 a month), with:
- a Neon Free project in Singapore as the integration-test database;
- Better Stack Free for the HTTP check and a poller heartbeat.

**Owner's decision:** the cheaper **Fly.io in `sin` + Supabase Free in
Singapore**, about $4–7 a month.

- **Database connection:** Supabase's direct connection. It is IPv6-only on the free plan, and Fly machines reach it over outbound IPv6. The session-mode pooler on `:5432` is the fallback. Transaction mode on `:6543` is never used.
- **Uptime:** Better Stack Free.
- **Integration tests:** a Neon Free project in Singapore, through `TEST_DATABASE_URL`.

**Risks accepted with Supabase Free:**
- no automatic backups, so the M15 evidence tables could be lost;
- read-only past 500 MB, which the purge and ingestion-off keep well clear of;
- pausing after a week of inactivity. Whether ten-minute polls count as activity is unverified; the uptime monitor would catch a pause.

### 2026-09-30 · Code complete; waiting on day 0

Tasks 2-13 and 15-18 are built test-first on `v2-plan`. The deploy, the
unattended window and the owner's labelling remain.

**v1 bugs fixed along the way:**
- An *Edit* followed by *Cancel* or *Confirm* looped back to extraction.
- The image ignored `uv.lock`.
- A redeploy stranded `claimed` rows.
- `/health` could not fail.
- Retries were invisible.
- The hourly token refresh erased token metadata.
- Mail bodies were never deleted.
- `record_tick` connected with no timeout, so an unreachable database held the scheduler for minutes.

**Verified:**
- 575 unit tests pass in about 10 seconds.
- On the Neon test database, 60 integration tests pass. The one failure is the known `test_vector_mode_cannot_reach_a_keyword_only_match`, fixed on an unmerged branch in another session.
- A stalled poller turned `/health` into a 503 after three intervals, recording one `ok = false` tick per interval.
- CI built the Docker image.

**Merged `origin/main`**, which brings the verified price table and cached-token fix (PR #1) and the gateway triage (PR #2).

**Found, still open:**
- `gemini-embedding-001` is not on Google's pricing page as of today. Embedding cost is estimated at Gemini Embedding 2's $0.20 per million tokens, and M18 must check the model's status before re-enabling ingestion.
- The owner's local `.env` sets `EXTRACTION_MODEL=gemini-2.5-pro`, which the price table leaves out because it has tiered pricing. Production keeps the priced default (`gemini-3.6-flash`), or `measure cost` refuses to run.
- The Neon test database's password was pasted into the session chat. The owner resets it.
