# M15 · Go live and measure — tasks

Spec: [`docs/plans/M15-go-live.md`](../docs/plans/M15-go-live.md) · Plan: [`plan.md`](plan.md)

- **Every code task:** write the failing test first, then make it pass. `.\tasks.ps1 check` must be green before committing.
- **Owner steps** are marked **[owner]**.
- **Scope sizes:** S is 1–2 files, M is 3–5 files.
- **This machine has no working Postgres or Docker.** WSL and Hyper-V are disabled; see the session memory "no-local-postgres". So:
  - tests marked `integration` skip under a plain `check`. They run in one of three places:
    - a local `pgserver` scratch Postgres, following the recipe in that memory. It needs the owner's OK, because it is a download.
    - CI, using the pgvector service in `.github/workflows/ci.yml`.
    - the hosted dev database chosen in task 1, via `TEST_DATABASE_URL`.
  - CI's Test step has been red since August because of `test_rag_search.py::test_vector_mode_cannot_reach_a_keyword_only_match`, which only passes against a populated database. A separate session is fixing it. Check the failing test's name before blaming a change.
  - the image is built by CI's "Build image" step or by Fly's remote builder.

---

## Phase 1 · Decide

### Task 1: Choose host, database, regions and uptime monitor

**Description:** Fetch the current pricing and feature pages for Fly.io,
Railway, Supabase, Neon and two free uptime monitors, then propose one
combination. Requirements:
- the database has a direct or session-mode connection, pgvector, and stays always on;
- the app and database share a region near the owner;
- hosting plus database list prices come to at most $10/month;
- the monitor gives 5-minute checks with email alerts.

Also decide how integration tests run: a separate hosted **dev** database, or
CI only. CI only means pushing the branch, which the owner must approve.

The owner decides.

**Acceptance criteria:**
- [x] A comparison with sources and fetch dates is added to the M15 running notes.
- [x] The owner's choice is recorded:
  - host: Fly.io in `sin`;
  - database: Supabase Free in Singapore, over its direct IPv6 connection;
  - uptime monitor: Better Stack Free;
  - integration tests: a Neon Free project in Singapore.
- [x] Railway was not chosen, so the Fly steps stand.

**Verification:** the running notes carry the decision, with sources.

**Dependencies:** none · **Files:** `docs/plans/M15-go-live.md` · **Scope:** XS

---

## Phase 2 · Harden

### Task 2: Fix the Edit → Cancel/Confirm routing bug

**Description:** `await_approval` never clears `correction`. After an *Edit*,
any later decision is routed back to `extract`, and the proposal stays parked.
Non-edit decisions must clear it.

**Acceptance criteria:**
- [x] *Edit* then *Cancel* ends at `reject` with the ledger marked `REJECTED`.
- [x] *Edit* then *Confirm* reaches `act` exactly once.
- [x] Existing graph tests still pass.

**Verification:** `uv run pytest tests/test_graph.py`, then `.\tasks.ps1 check`.

**Dependencies:** none · **Files:** `app/graph/nodes.py`, `tests/test_graph.py` · **Scope:** S

### Task 3: Add the `sweep` decision and record `dry_run` in proposals

**Description:**
- The interrupt payload stores the `DRY_RUN` value in force when the proposal parked.
- A new `sweep` decision ends a proposal with the ledger reason "swept: observe mode ended".
- `approve.py` accepts `--action sweep`.

**Acceptance criteria:**
- [x] The pending payload contains `dry_run`.
- [x] `sweep` records the sweep reason, never "declined by user". `--sweep-all` ends every parked proposal.
- [x] `approve --list` shows `dry_run` for each proposal.

**Verification:** graph and approve tests, then `.\tasks.ps1 check`.

**Dependencies:** task 2 · **Files:** `app/graph/nodes.py`, `app/jobs/approve.py`, `tests/test_graph.py` · **Scope:** S

### Task 4: Make the image reproducible

**Description:**
- The Dockerfile copies `uv.lock`, runs `uv sync --locked --no-dev`, pins the uv image, and creates `/app/secrets` owned by `appuser`.
- `pyproject.toml` gets floors at the locked versions and caps below the next major version for the three LangGraph packages.
- `fly.toml` raises `kill_timeout` above the longest tick.

**Acceptance criteria:**
- [x] `uv lock --check` passes after the bounds change.
- [ ] The image builds, locally or on Fly's remote builder, and boot-time secret writing succeeds as `appuser`.
- [x] No dependency version changes in `uv.lock`: only the manifest constraint and the specifiers changed.
- [x] `fly.toml`:
  - `kill_timeout = 120`, up from the 5-second default;
  - `primary_region = "sin"`;
  - `swap_size_mb = 512`;
  - auto-stop already off.

*Status: done except the image build, which stays unverified until CI or the first remote build (task 14), because this machine has no Docker.*

**Verification:** `uv lock --check`; `docker build .` when Docker is running, otherwise at task 14's first deploy; `.\tasks.ps1 check`.

**Dependencies:** task 1 (host) · **Files:** `Dockerfile`, `pyproject.toml`, `uv.lock`, `fly.toml` · **Scope:** S

### Checkpoint: after tasks 1–4

- [x] `.\tasks.ps1 check` is green.
- [x] The routing fix is proven by tests.
- [x] Hosting is decided.
- [x] The owner reviewed on 2026-09-30 and said continue.

### Task 5 (reduced): Refuse transaction-mode pooler URLs

**Description:** Task 1 chose Supabase's direct connection, so no
prepared-statement workaround is needed. What remains is a guard.

Settings validation rejects a `DATABASE_URL` or `TEST_DATABASE_URL` that
points at a known transaction-mode pooler: Supabase's port `6543`, or a Neon
host containing `-pooler`. The error names the direct or session-mode
alternative. Otherwise a pasted pooler string would fail every checkpoint
write at runtime instead of at boot.

**Acceptance criteria:**
- [x] Both pooler forms are rejected at startup (`get_settings`), with an actionable message that never repeats the URL. `TEST_DATABASE_URL` gets the same check, and skip messages show only the host.
- [x] Direct, session-mode and localhost URLs are accepted.

**Verification:** `uv run pytest tests/test_config.py`; `.\tasks.ps1 check`.

**Dependencies:** task 1 · **Files:** `app/config.py`, `tests/test_config.py` · **Scope:** S

### Task 6: Shutdown-safe polling and recovery of stranded claims

**Description:**
- Once shutdown begins, the poller stops claiming new messages.
- At boot, ledger rows still `claimed` after more than one hour become `FAILED`, with the reason "stranded by shutdown".

**Acceptance criteria:**
- [x] A shutdown signal mid-tick claims no further messages, and a stopped pass leaves the sync cursor where it was.
- [ ] Boot recovery marks only stale `claimed` rows; fresh claims and other statuses are untouched. *The integration tests are written but have not run: they need `TEST_DATABASE_URL` (the Neon project).*

**Verification:** ledger and scheduler tests (integration marker for Postgres); `.\tasks.ps1 check`.

**Dependencies:** none · **Files:** `app/jobs/poll.py`, `app/api.py`, `app/store/ledger.py`, `tests/test_ledger.py`, `tests/test_scheduler.py` · **Scope:** M

### Task 7: The `job_runs` table and tick recording

**Description:**
- Migration `005_job_runs.sql`.
- Every scheduled poll and ingest tick writes a row: job, start, finish, success, counts and error.
- A poll tick succeeds only if it raised nothing and marked no message `FAILED`, so `poll_once` reports its failure count.
- A gap query, including window sentinels, returns the longest stretch without a successful poll.

**Acceptance criteria:**
- [x] A tick with one `FAILED` message records `ok = false`.
- [x] A raising tick records `ok = false` with the error type, and the scheduler keeps running. A database outage costs only the record.
- [ ] The gap query catches gaps at the start, middle and end of a window. *The integration tests are written but have not run: they need `TEST_DATABASE_URL`.*

**Verification:** `.\tasks.ps1 migrate`; tests with the integration marker; `.\tasks.ps1 check`.

**Dependencies:** task 6 · **Files:** `migrations/005_job_runs.sql`, `app/store/job_runs.py`, `app/jobs/scheduler.py`, `app/jobs/poll.py`, `tests/test_job_runs.py` · **Scope:** M

### Task 8: In-memory `/health`, logging and quiet production output

**Description:**
- `/health` reads the last tick's result and time and the token state from memory.
- It returns 503 after three intervals without a successful poll, with a one-interval grace after boot.
- It never echoes exception text and never calls the database.
- The web process configures logging at startup.
- In production, `poll` prints message ids and statuses, never titles.

**Acceptance criteria:**
- [x] `/health` returns 200 inside the boot grace and 503 after three failed intervals.
- [x] No database call happens per request; it reads the in-process `LIVENESS` record.
- [x] Scheduler log lines appear in the server's output. Seen at runtime: `ERROR app.api: token health check failed` under uvicorn.
- [x] With `APP_ENV=prod`, poll output contains no extracted title.

**Verification:** `uv run pytest tests/test_api.py tests/test_scheduler.py`; a manual check with `.\tasks.ps1 serve` and `curl -i localhost:8000/health`.

**Dependencies:** task 7 · **Files:** `app/api.py`, `app/main.py`, `app/jobs/poll.py`, `tests/test_api.py` · **Scope:** M

### Checkpoint: after tasks 5–8

- [ ] `.\tasks.ps1 check` is green, and the integration tests pass in CI or on the dev database.
- [ ] Against the dev database, `serve` with the scheduler on and `DRY_RUN=true` writes `job_runs` rows, and `/health` flips to 503 when polling is broken, for example with a wrong `TEST_CALENDAR_ID`.

### Task 9: Make retries visible

**Description:** Count retries in `call_with_retry`, carry the count in
`SpanUsage`, and write it to `spans.retry_count`. Coordinate with the
price-table session, which may also touch `trace.py`: keep this change to the
retry plumbing, and rebase onto their merge if both land.

**Acceptance criteria:**
- [ ] A call that succeeds after two 429s writes `retry_count = 2`.
- [ ] A call that exhausts its retries still writes its error span, with the count.

**Verification:** `uv run pytest tests/test_tool_loop.py tests/test_obs.py`; `.\tasks.ps1 check`.

**Dependencies:** none · **Files:** `app/extraction/llm.py`, `app/obs/trace.py`, `tests/test_obs.py` · **Scope:** S

### Task 10: Token metadata that survives, and `reauth --minted-under`

**Description:**
- `tasks.ps1` forwards arguments to `reauth`.
- `reauth` requires `--minted-under testing|production` and stores it next to `issued_at`.
- `TokenStore.save()` preserves every metadata field on routine refreshes.

**Acceptance criteria:**
- [ ] `reauth` without the flag exits with a usage error.
- [ ] Metadata survives a routine save.
- [ ] Existing tokens without `minted_under` load as `testing`.

**Verification:** `uv run pytest tests/test_tokens.py`; `.\tasks.ps1 check`.

**Dependencies:** none · **Files:** `tasks.ps1`, `app/google/reauth.py`, `app/google/tokens.py`, `tests/test_tokens.py` · **Scope:** S

### Task 11: Refresh evidence and per-token states

**Description:**
- Every refresh outcome is written to `job_runs` (`job = 'token_refresh'`), keyed to the token's `issued_at`.
- Token state is computed per token: `testing`, `production-unconfirmed`, `production-confirmed` or `expired`, as defined in the spec's A5.
- `/health` exposes the states.

**Acceptance criteria:**
- [ ] Confirmation needs a recorded successful refresh after `issued_at` + 7 days.
- [ ] An older token's failure never marks a newer token expired.
- [ ] `invalid_grant` gives `expired`.

**Verification:** token and API tests; `.\tasks.ps1 check`.

**Dependencies:** tasks 7, 8, 10 · **Files:** `app/google/tokens.py`, `app/google/auth.py`, `app/api.py`, `tests/test_tokens.py` · **Scope:** M

### Task 12: Standby token failover

**Description:**
- `bootstrap` also writes `GOOGLE_TOKEN_STANDBY_B64` to its own absolute path.
- On `invalid_grant` for the primary, the primary is recorded `expired` — the evidence — and the credentials fail over to the standby.
- `/health` returns 503 only when no token is usable.

**Acceptance criteria:**
- [ ] With a failing primary and a valid standby, polling continues, and health shows the primary `expired` and the standby in use.
- [ ] With both failing, `/health` returns 503.
- [ ] Without a standby configured, behaviour is unchanged.

**Verification:** token, bootstrap and API tests with fakes; `.\tasks.ps1 check`.

**Dependencies:** task 11 · **Files:** `app/bootstrap.py`, `app/google/auth.py`, `app/google/tokens.py`, `tests/test_tokens.py` · **Scope:** M

### Task 13: Checkpoint purge job and the M15 runbook

**Description:** An hourly purge, plus the rewritten runbook.
- **Purge:**
  - deletes checkpoints of terminal threads (`SKIPPED`, `REJECTED`, `CREATED`) through the saver's thread deletion;
  - deletes `FAILED` threads' checkpoints after 7 days;
  - clears reasoning text from terminal ledger rows older than 7 days;
  - never touches parked threads.
- **Runbook** (`docs/DEPLOY.md` and `.env.example`):
  - billing before the first deploy;
  - `--ha=false`;
  - connection strings;
  - absolute paths;
  - the production key, primary token and standby token;
  - the observe-mode variables;
  - the uptime monitor;
  - `fly ssh console` usage.

**Acceptance criteria:**
- [ ] The purge removes only what it should: tests seed each ledger status and a parked thread.
- [ ] Following DEPLOY.md needs no step outside it.

**Verification:** purge tests with the integration marker; `.\tasks.ps1 check`; a read-through of DEPLOY.md against the spec.

**Dependencies:** tasks 3, 7, 12 · **Files:** `app/jobs/purge.py`, `app/jobs/scheduler.py`, `tests/test_purge.py`, `docs/DEPLOY.md`, `.env.example` · **Scope:** M

### Checkpoint: after tasks 9–13

- [ ] `.\tasks.ps1 check` is green, and the integration tests pass in CI or on the dev database.
- [ ] End-to-end run against the dev database, with the dev token and `DRY_RUN=true`:
  - poll;
  - `job_runs` filled;
  - token state shown;
  - purge removes skipped threads;
  - `approve --list` shows `dry_run`.
- [ ] **The owner reviews before the deploy.**

---

## Phase 3 · Deploy (day 0)

### Task 14: Deploy in observe mode [owner + agent]

**Description:**
- **[owner]**
  - Enable billing, set the budget alert, and publish the OAuth app.
  - Generate the production `FERNET_KEY`.
  - Run `.\tasks.ps1 reauth --minted-under production` into the production token path.
  - Set the secrets.
  - Create the database and the uptime monitor.
- **Agent, after the owner approves the exact commands:**
  - `fly deploy --ha=false` and `fly scale count 1`;
  - check `/health`;
  - run `approve --list` over `fly ssh console`.

**Acceptance criteria:**
- [ ] One machine is running.
- [ ] `/health` returns 200, with the token `production-unconfirmed` and `dry_run: true`.
- [ ] `job_runs` gains a row every interval.
- [ ] `approve --list` works over ssh.
- [ ] The uptime monitor is green.
- [ ] Day 0 is recorded in the running notes.

**Verification:** the commands above, recorded in the running notes.

**Dependencies:** tasks 1–13 · **Files:** `docs/plans/M15-go-live.md` (running notes) · **Scope:** XS

### Checkpoint: day 0

- [ ] Deployed, healthy, observe mode confirmed.
- [ ] Every setting the spec lists appears in the running notes.

---

## Phase 4 · Measure (built during the window)

### Task 15: Gmail metadata client methods

**Description:** Add `users.getProfile`, fully paginated listing over a
window, and `threads.get` with `format=metadata` for the given headers, all
returning typed metadata records. No bodies.

**Acceptance criteria:**
- [ ] Pagination follows every page token.
- [ ] Only `format=metadata` is requested.
- [ ] Records carry label ids, `internalDate` and the named headers only.

**Verification:** `uv run pytest tests/test_gmail.py`; `.\tasks.ps1 check`.

**Dependencies:** none · **Files:** `app/google/gmail.py`, `tests/test_gmail.py` · **Scope:** S

### Task 16: `measure mail`: counts and the flagged set

**Description:** Implement the B2 rules and outputs, plus the guards.
- **Rules:**
  - owner detection by `SENT`;
  - drafts ignored;
  - address normalisation;
  - the automated rules;
  - excluded senders;
  - the thread as the unit;
  - the 48-hour rule;
  - answered later.
- **Outputs:** the Primary, other-category, automated and no-category split; the weekly rate; the informational snapshot.
- **Guards:** the time-zone guard, and a printed header of windows, time zone, seed and commit. It writes counts only, to `results/`.
- **Settings:** `OWNER_ALIASES` and `MEASURE_EXCLUDE_SENDERS`, plus a `measure` entry in `tasks.ps1`.

**Acceptance criteria:**
- [ ] Every B2 definition has a test.
- [ ] No header value appears in any written output, asserted against seeded fixtures.
- [ ] It refuses to run with the default `UTC` zone.

**Verification:** `uv run pytest tests/test_measure.py`; `.\tasks.ps1 check`; one agent-run pass on the owner's mailbox, reviewing the counts-only output.

**Dependencies:** task 15 · **Files:** `app/jobs/measure.py`, `app/config.py`, `tasks.ps1`, `tests/test_measure.py` · **Scope:** M

### Task 17: `measure mail --label`, the owner-only sampler

**Description:**
- Seeded random samples: 40 flagged threads (all if fewer), with two questions each, and 20 unflagged human threads, with one question.
- Precision and miss rate with Wilson 95% intervals.
- The corrected weekly rate uses the precision lower bound.
- It refuses to run without an interactive terminal.
- It writes only counts.

**Acceptance criteria:**
- [ ] The TTY guard refuses non-interactive runs.
- [ ] The Wilson computation is tested against known values.
- [ ] Nothing but counts is written.

**Verification:** `uv run pytest tests/test_measure.py`; `.\tasks.ps1 check`. The labelling run itself is task 18, done by the owner.

**Dependencies:** task 16 · **Files:** `app/jobs/measure.py`, `tests/test_measure.py` · **Scope:** S

### Task 18: `measure cost` (blocked until the price-table fix merges)

**Description:**
- Prices raw span columns through the merged pricing API at 15 Dec 2026 and 15 Jan 2027.
- Aborts on an unpriced model.
- Computes the meeting rate and the extraction line (measured, or *unmeasured* plus its bound).
- Estimates embeddings.
- Totals retries.
- Builds the projection, with the M18 estimate lines, the list-price lines, and a reconciliation table for the billing figures the owner enters.
- Refuses a localhost `DATABASE_URL`.

**[owner]** Run `.\tasks.ps1 measure mail ... --label` in your own terminal
(about 20 minutes).

**Acceptance criteria:**
- [ ] The cost tests from the spec's Testing section pass.
- [ ] The labelling counts are recorded.
- [ ] The corrected rate is computed.

**Verification:** `uv run pytest tests/test_measure.py`; `.\tasks.ps1 check`.

**Dependencies:** task 16, the price-table merge · **Files:** `app/jobs/measure.py`, `tests/test_measure.py` · **Scope:** M

### Checkpoint: measurement ready

- [ ] `.\tasks.ps1 check` is green.
- [ ] `measure mail` has run.
- [ ] The labelling is done.
- [ ] `measure cost` has run against the instance's partial window.

---

## Phase 5 · Run the window

### Task 19: Day 1 planted proposal and mid-window redeploy [owner + agent]

**Description:**
- **[owner]** Send a meeting email from another account on day 1. Add that sender to `MEASURE_EXCLUDE_SENDERS`.
- **Agent:** once it parks, list it over ssh. Around day 5, redeploy the same image and confirm it is still listed.

**Acceptance criteria:**
- [ ] The proposal is visible before and after the redeploy.
- [ ] No `claimed` row is stranded.
- [ ] Both are recorded in the running notes.

**Verification:** `approve --list` over ssh; the ledger query in the running notes.

**Dependencies:** task 14 · **Files:** the running notes · **Scope:** XS

### Task 20: Standby token on day 4 [owner]

**Description:** Mint a second production token with
`reauth --minted-under production` into the standby path, then upload it as
`GOOGLE_TOKEN_STANDBY_B64`.

**Acceptance criteria:**
- [ ] `/health` shows both tokens.
- [ ] The primary is still in use.

**Verification:** `curl -i` on `/health`.

**Dependencies:** tasks 12, 14 · **Files:** none · **Scope:** XS

---

## Phase 6 · Close (day 8 and after)

### Task 21: Exit evidence, results and go/no-go

**Description:**
- Run the gap query with sentinels, and review every `ok = false` tick and `FAILED` row.
- Export the uptime monitor's history.
- Record the token states.
- Run `measure cost` over the window; the owner enters the billing figures; reconcile.
- Assemble `results/volume-YYYY-MM-DD.md` from `measure mail` and `measure cost`, then review it for personal data.
- Compute both go/no-go results against the committed thresholds, and write them in the running notes.
- Sweep every parked proposal.
- Update the INDEX status, and the README *Status* and *Known limitations* sections once the price-table session has merged.

**Acceptance criteria:**
- [ ] All five exit criteria in the spec hold, each with its evidence linked in the running notes.
- [ ] The results file cites the threshold commit `52ddc5e`.

**Verification:** re-read the exit criterion against the running notes. The owner reviews before M15 is marked done.

**Dependencies:** tasks 17–20 · **Files:** `results/volume-*.md`, `docs/plans/M15-go-live.md`, `docs/plans/INDEX.md`, `README.md` · **Scope:** S

### Checkpoint: M15 complete

- [ ] Exit criterion 1–5 met.
- [ ] Go/no-go decisions recorded.
- [ ] Next module chosen from the results: M16, M17 or M20.

---

## Optional

### Task 22: M13 reviewer eval delta

Run `.\tasks.ps1 eval --extractor gemini_reviewed` on the paid tier, outside
the unattended window. Publish the result and close M13, whatever it shows.
