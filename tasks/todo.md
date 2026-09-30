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
- [x] Boot recovery marks only stale `claimed` rows; fresh claims and other statuses are untouched. Passed on Neon.

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
- [x] The gap query catches gaps at the start, middle and end of a window. Passed on Neon.

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

- [x] `.\tasks.ps1 check` is green.
- [x] The integration tests pass on the Neon test database: `uv run --env-file .env.test pytest -m integration`. Result: 54 passed; 1 failed, the known `test_vector_mode_cannot_reach_a_keyword_only_match`.
- [x] Against the Neon test database, `serve` with the scheduler on writes `ok = false` rows to `job_runs`, and `/health` flips to 503 when polling is broken. Seen on 2026-09-30: 4 rows (`GoogleAuthNotConfiguredError`); `no successful poll in three intervals` after 4.5 minutes.
  - Break polling with `TEST_CALENDAR_ID` unset, so `graph_session` fails before any Gmail read.
  - Neon holds test data only. The healthy path, with real mail, is first exercised on Supabase at task 14.

### Task 9: Make retries visible

**Description:** Count retries in `call_with_retry`, carry the count in
`SpanUsage`, and write it to `spans.retry_count`. Coordinate with the
price-table session, which may also touch `trace.py`: keep this change to the
retry plumbing, and rebase onto their merge if both land.

**Acceptance criteria:**
- [x] A call that succeeds after two transient failures writes `retry_count = 2`, checked against the spans table on Neon.
- [x] A call that exhausts its retries still writes its error span, with the count (3 of 4 attempts were retries).

**Verification:** `uv run pytest tests/test_tool_loop.py tests/test_obs.py`; `.\tasks.ps1 check`.

**Dependencies:** none · **Files:** `app/extraction/llm.py`, `app/obs/trace.py`, `tests/test_obs.py` · **Scope:** S

### Task 10: Token metadata that survives, and `reauth --minted-under`

**Description:**
- `tasks.ps1` forwards arguments to `reauth`.
- `reauth` requires `--minted-under testing|production` and stores it next to `issued_at`.
- `TokenStore.save()` preserves every metadata field on routine refreshes.

**Acceptance criteria:**
- [x] `reauth` without the flag exits with a usage error (exit code 2).
- [x] Metadata survives a routine save, including fields this version does not know.
- [x] Existing tokens without `minted_under` load as `testing`.

**Verification:** `uv run pytest tests/test_tokens.py`; `.\tasks.ps1 check`.

**Dependencies:** none · **Files:** `tasks.ps1`, `app/google/reauth.py`, `app/google/tokens.py`, `tests/test_tokens.py` · **Scope:** S

### Task 11: Refresh evidence and per-token states

**Description:**
- Every refresh outcome is written to `job_runs` (`job = 'token_refresh'`), keyed to the token's `issued_at`.
- Token state is computed per token: `testing`, `production-unconfirmed`, `production-confirmed` or `expired`, as defined in the spec's A5.
- `/health` exposes the states.

**Acceptance criteria:**
- [x] Confirmation needs a recorded successful refresh after `issued_at` + 7 days.
- [x] An older token's failure never marks a newer token expired, both in memory and in `job_runs` (checked on Neon).
- [x] `invalid_grant` gives `expired`. Evidence is reloaded from `job_runs` at boot, so a redeploy keeps a confirmation.

**Verification:** token and API tests; `.\tasks.ps1 check`.

**Dependencies:** tasks 7, 8, 10 · **Files:** `app/google/tokens.py`, `app/google/auth.py`, `app/api.py`, `tests/test_tokens.py` · **Scope:** M

### Task 12: Standby token failover

**Description:**
- `bootstrap` also writes `GOOGLE_TOKEN_STANDBY_B64` to its own absolute path.
- On `invalid_grant` for the primary, the primary is recorded `expired` — the evidence — and the credentials fail over to the standby.
- `/health` returns 503 only when no token is usable.

**Acceptance criteria:**
- [x] With a failing primary and a valid standby, polling continues, and health shows the primary `expired` and the standby in use. The dead primary is not retried every poll.
- [x] With both failing, loading credentials raises and `/health` returns 503.
- [x] Without a standby configured, behaviour is unchanged.

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
- [x] The purge removes only what it should: tests seed each ledger status and a parked thread. 3 tests passed on Neon.
- [x] Following DEPLOY.md needs no step outside it: a read-through against the spec on 2026-09-30.

**Verification:** purge tests with the integration marker; `.\tasks.ps1 check`; a read-through of DEPLOY.md against the spec.

**Dependencies:** tasks 3, 7, 12 · **Files:** `app/jobs/purge.py`, `app/jobs/scheduler.py`, `tests/test_purge.py`, `docs/DEPLOY.md`, `.env.example` · **Scope:** M

### Checkpoint: after tasks 9–13

- [x] `.\tasks.ps1 check` is green: 472 unit tests pass in about 10 seconds.
- [x] The integration tests pass on Neon: 60 passed. The only failure is the known `test_vector_mode_cannot_reach_a_keyword_only_match`.
- [x] ~~End-to-end run against the dev database with the dev token~~ **moved to task 14.**
  - Polling with the real token puts real mail bodies into checkpoints, and Neon holds test data only.
  - The healthy-path end-to-end is task 14's day-0 checks on Supabase, the chosen home for real mail.
  - Each piece is already covered here:
    - polling and failed ticks: the stalled-poll run on Neon;
    - the purge and the tick records: the Neon integration tests;
    - token state and `dry_run` in `approve --list`: unit tests.
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
- [x] Pagination follows every page token.
- [x] Only `format=metadata` is requested.
- [x] Records carry label ids, `internalDate` and the named headers only.

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
- [x] Every B2 definition has a test (26 tests).
- [x] No header value appears in any written output, asserted against seeded fixtures.
- [x] It refuses to run with the default `UTC` zone.
- [ ] One pass on the owner's real mailbox. Pending: the local Google token is unreadable on this machine, so run `.	asks.ps1 reauth` locally first.

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
- [x] The TTY guard refuses non-interactive runs (stdin and stdout must both be a terminal).
- [x] The Wilson computation is tested against known values; the go/no-go uses the precision lower bound.
- [x] Nothing but counts is written. What the owner sees on screen is not kept.

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
- [x] The cost tests from the spec's Testing section pass (12 tests; span loading checked on Neon). With no classifications there is no decision. Found while testing:
  - `gemini-embedding-001` is not on the pricing page (2026-09-30), so embeddings are estimated at Gemini Embedding 2's $0.20 per million tokens. M18 must check that model's status.
  - The local `.env` sets `EXTRACTION_MODEL=gemini-2.5-pro`, which is unpriced (tiered pricing). Production keeps the priced default, or cost cannot be measured.
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

---
---

# M16 · Web channel — tasks

Spec: [`docs/plans/M16-web-channel.md`](../docs/plans/M16-web-channel.md) · Plan: [`plan.md`](plan.md), second half

- **M15's conventions at the top of this file apply:** test first, `.\tasks.ps1 check` green before committing, **[owner]** marks, S and M sizes, and integration tests on Neon with `uv run --env-file .env.test pytest -m integration`.
- **Web checks:** `cd dashboard && npm run typecheck && npm run build && npm test`. Tests run with Node 24's `node --test`; no test framework is added.
- **`DRY_RUN` stays `true` throughout.**

---

## Phase 1 · Prove the risky parts

### Task 16.1: Owner-only sign-in slice

**Description:**
- First, check whether Next 16.3 expects `proxy.ts` rather than `middleware.ts`, and where it runs on Vercel. If that differs from D5's region note, correct the spec.
- Replace the shared-token gate with Auth.js v5 (`next-auth@5.0.0-beta.32`, pinned exactly) and Google sign-in.
- Admit only a verified email equal to `OWNER_EMAIL`. A blank `OWNER_EMAIL` admits nobody.
- Exempt exactly `/manifest.webmanifest`, `/sw.js`, `/icons/*` and `/api/auth/*`.
- Add a manifest and icons, so iPhone installs a real web app rather than a bookmark.
- Add `/me`, which shows the signed-in email and needs no database.
- Keep the allow-list and the exempt-path rule in a module with no framework imports.

**Acceptance criteria:**
- [x] The owner's verified email is admitted. Another email, an unverified one, and any email while `OWNER_EMAIL` is blank are refused (unit tests, 11 passing).
- [x] The gate exempts exactly the four listed paths. Unit tests cover it, and a local run confirmed it: gated pages answer 307, and exempt paths answer without a session.
- [x] `DASHBOARD_TOKEN` is gone from the code and the docs. M09's spec keeps it as history.
- [x] Found on the way: `next` 16.3.1 had critical advisories. With the owner's approval, it is upgraded to 16.3.8, and `sharp` to 0.35.5. `npm audit` now reports 0, and the checks and the local gate probe pass again.

**Verification:** the web checks.

**Dependencies:** none · **Files:** `dashboard/package.json` (and lock), `dashboard/src/auth.ts`, `dashboard/src/middleware.ts`, `dashboard/src/lib/access.ts` (and its test), the manifest and `/me` · **Scope:** M

### Task 16.2: Day-1 phone test [owner + agent]

**Description:**
- **[owner]** Create the sign-in Google Cloud project, separate from the Gmail project. Publish it "In production" with only `openid email profile`, and create a web OAuth client for the Vercel URL.
- **[owner]** Create the Vercel Hobby project with `dashboard/` as its root. Set `AUTH_SECRET`, `AUTH_GOOGLE_ID`, `AUTH_GOOGLE_SECRET` and `OWNER_EMAIL`.
- **Agent:** add `dashboard/vercel.json` with `"regions": ["sin1"]`, and deploy.
- **[owner]** Sign in with Android Chrome and inside the installed iPhone app. Then try a second Google account.

Pages that read the database may fail until 16.23. Only sign-in is under test.

**Acceptance criteria:**
- [ ] `/me` shows the owner signed in on Android and inside the installed iPhone app. Otherwise the iPhone failure is recorded, and 16.24 is scheduled.
- [ ] A second Google account is refused by the allow-list, not by Google, because the sign-in project is in production.
- [ ] The results are in the M16 running notes.

**Verification:** the owner's report, recorded in the running notes.

**Dependencies:** 16.1 · **Files:** `dashboard/vercel.json`, the running notes · **Scope:** S

### Checkpoint: sign-in proven

- [ ] The web checks pass.
- [ ] Whether pairing is needed is recorded.

### Task 16.3: Payload additions, pipeline version and the thread's revision

**Description:**
- `await_approval` adds `review_issues`, `action_type` and `pipeline_version` to the payload at every park.
- `graph_session` computes `pipeline_version` once and carries it in `Deps`. It hashes the extraction and classify models, `reviewer_enabled`, `reviewer_model`, `search_context_enabled`, and the extraction, classify, search and reviewer prompts.
- `GraphSession` gains:
  - `revision(message_id)`: `revisions + 1`, read from the thread's state;
  - `redrive(message_id)`: runs `invoke(None)` through the tracer.

**Acceptance criteria:**
- [x] Toggling the reviewer, or changing any listed model or prompt, changes `pipeline_version`. Nothing else does. The reviewer's model and prompt count only while the reviewer runs, and the search prompt only while search does.
- [x] An edit that adds a guest turns `calendar_hold` into `calendar_invite` at the re-park.
- [x] The revision is 1 at the first park and 2 after an edit. It is read from state, never from the payload, so older payloads get the same answer.
- [x] `redrive` re-runs a node that failed after an edit, and the owner's correction still reaches it.

**Verification:** `uv run pytest tests/test_graph.py tests/test_versioning.py`; `.\tasks.ps1 check`.

**Dependencies:** none · **Files:** `app/graph/nodes.py`, `app/graph/runner.py`, `app/graph/versioning.py`, `tests/test_graph.py`, `tests/test_versioning.py` · **Scope:** M

### Task 16.4: Migration 007 and the park step

**Description:**
- `007_proposals.sql` creates `proposals`, `decisions`, `alerts_sent`, `push_subscriptions` and `pairing_codes`. A partial unique index allows one open decision per message.
- `app/channel/park.py` works in one transaction. It writes the `proposals` row and marks the ledger `awaiting_approval`, conditional on the non-final status it read. Then it calls an announce hook.
- Poll uses the park step instead of its own `mark` and `_notify`. The hook keeps today's Telegram notify until 16.13.

**Acceptance criteria:**
- [x] 007 applies and re-runs on Neon.
- [x] A park writes both rows or neither: a failure injected between the two writes leaves neither.
- [x] The ledger write never overwrites a final status, and a park never reopens a decided proposal.
- [x] Poll's tests pass, and a parked message has a `proposals` row with the thread's revision. On Neon, 76 integration tests pass; the one failure is the known one.
- Until 16.10 and 16.11, an edit made through the CLI or Telegram re-parks without updating the `proposals` row. Nothing is deployed in between.

**Verification:** `uv run pytest tests/test_park.py tests/test_poll.py`; the integration tests on Neon; `.\tasks.ps1 check`.

**Dependencies:** 16.3 · **Files:** `migrations/007_proposals.sql`, `app/channel/park.py`, `app/jobs/poll.py`, `tests/test_park.py`, `tests/test_poll.py` · **Scope:** M

### Task 16.5: `decide()` records and enqueues

**Description:** `app/channel/decide.py`, as D1 describes:
- Validate: an edit needs a correction, there is no edit at revision 3, and a message with no `proposals` row is refused as not found.
- Then, in one transaction on the caller's own connection:
  - claim the proposal (`pending` at the given revision becomes `deciding`);
  - insert the `decisions` row with `outcome = NULL`, copying `action_type` and `pipeline_version` and computing latency from `parked_at`.
- It never touches the graph. It returns queued, stale, not found or invalid.

**Acceptance criteria:**
- [x] Each action enqueues exactly one decision.
- [x] A stale revision is refused, and nothing is enqueued. So is a second tap on the same card.
- [x] **Two concurrent decisions on one revision: one wins, and the other is refused**, on Neon, with two connections released together.
- [x] An empty correction, an edit at revision 3 and a missing row are refused. So is a correction over 2,000 characters, because it goes into a model prompt.
- [x] Each decision records its `via`, `action_type` and `pipeline_version`, and its latency from when that revision parked.

**Verification:** `uv run pytest tests/test_decide.py`; the integration tests on Neon; `.\tasks.ps1 check`.

**Dependencies:** 16.4 · **Files:** `app/channel/decide.py`, `tests/test_decide.py` · **Scope:** S

### Task 16.6: The worker moves decisions from stored state

**Description:** `app/channel/worker.py`, `apply_open(session)`. It takes each open decision whose retry time has come and moves it through D1's table:
- parked at the decision's revision: resume;
- parked at the next revision: settle `reparked` through the park step, in the same transaction;
- ledger final: settle `decided`;
- mid-graph without `act` next: re-drive;
- `act` next: settle failed, reason `act interrupted`;
- anything else: settle failed.

A settle is one transaction of conditional writes.

**Acceptance criteria:**
- [x] Every row of D1's table has a test that seeds that stored state: the step rule is a pure function with unit tests, and the paths through a real graph are tested on Neon.
- [x] `act` in `next` is never re-driven. The graph's ledger counts its marks, and the worker adds none.
- [x] A late failed settle never overwrites a final ledger status, and an outcome is written only once.
- [x] A re-park carries the new revision and payload, and is announced.
- A parked thread at a revision the decision cannot explain is settled as failed rather than guessed at.

**Verification:** `uv run pytest tests/test_worker.py`; `.\tasks.ps1 check`.

**Dependencies:** 16.5 · **Files:** `app/channel/worker.py`, `app/graph/runner.py`, `tests/test_worker.py` · **Scope:** M

### Task 16.7: Worker retries, lease and crash convergence

**Description:**
- **Retries.** A resume or re-drive that raises costs one attempt. The next attempt waits 1 minute, then 10. The third failure settles: `no_effect` if the thread is still parked at the decision's revision, failed otherwise.
- **Lease.** Each decision is leased for 10 minutes by a conditional `UPDATE`.
- **Proof.** Inject a failure after each step: after the lease, after the resume, inside the settle, and after the settle. The next tick must finish the job, and nothing may be applied twice.

**Acceptance criteria:**
- [x] A failure after each step converges on the next tick. The fake graph counts exactly one application. Crashes are simulated with a `BaseException`, so no error handler runs, as when a process dies:
  - after taking the lease;
  - after the resume;
  - inside the settle, which rolls back whole;
  - after the settle.
- [x] Attempts wait 1 and 10 minutes, and the third failure settles. A thread still parked at the decision's revision comes back as `no_effect`, and the owner can decide again. A thread stuck mid-graph fails.
- [x] A second worker skips a leased decision, and an expired lease is taken over.
- Every failure costs an attempt, including a failed settle or a step that made no progress. That is what bounds the work a broken decision can cause.

**Verification:** `uv run pytest tests/test_worker.py`; the lease test on Neon; `.\tasks.ps1 check`.

**Dependencies:** 16.6 · **Files:** `app/channel/worker.py`, `tests/test_worker.py`, and `migrations/007_proposals.sql` only if a column is missing (007 is not yet deployed) · **Scope:** S

### Checkpoint: the queue works

- [ ] `.\tasks.ps1 check` is green, and the integration tests pass on Neon, apart from the known failure.
- [ ] With a fake graph, a proposal goes park, `decide`, worker, settled, for confirm, edit and cancel.
- [ ] The owner reviews before the queue is wired in.

---

## Phase 2 · Put every path on the queue

### Task 16.8: Reconciliation

**Description:** `app/channel/reconcile.py`, as D3 describes.
- It reads the thread of every ledger row that is `claimed`, `awaiting_approval`, or FAILED within seven days. A live interrupt with no `proposals` row gets the park step.
- Legacy payloads get their action type from their attendees, and `pipeline_version` `pre-m16`.
- A `pending` or `failed` proposal whose thread is gone and whose ledger is final becomes `decided`.
- A `deciding` proposal is never touched.
- It runs at boot after `fail_stranded`, hourly with the purge, and on `approve --reconcile`.

**Acceptance criteria:**
- [x] A live interrupt with no row gets one, whichever of the three ledger statuses it has (11 tests on Neon).
- [x] A legacy park gets its revision from state and `pipeline_version` `pre-m16`.
- [x] A `deciding` proposal is untouched. A proposal whose thread is still parked is not closed, even beside a final ledger.
- [x] It runs when the scheduler starts, which comes after boot's `fail_stranded`, and hourly after that. `approve --reconcile` prints what it did.
- A `claimed` row younger than 10 minutes is skipped, because a poll may be parking it right now.

**Verification:** `uv run pytest tests/test_reconcile.py tests/test_scheduler.py`; the integration tests on Neon; `.\tasks.ps1 check`.

**Dependencies:** 16.4 · **Files:** `app/channel/reconcile.py`, `app/api.py`, `app/jobs/scheduler.py`, `app/jobs/approve.py`, `tests/test_reconcile.py` · **Scope:** M

### Task 16.9: The `decisions` job, wake-up and health

**Description:**
- The scheduler runs the worker as the `decisions` job: every 15 seconds, `max_instances=1`. It starts no new decision once `STOPPING` is set.
- In the web process, a recorded decision wakes the job with `modify_job(next_run_time=now)`.
- `/health` reports the oldest open decision's age and returns 503 past one hour. It reads an in-memory record, which the job refreshes each tick and a new decision also updates, so a wedged job still shows.

**Acceptance criteria:**
- [x] The job is registered next to poll, purge, token_health and reconcile, with `max_instances=1`.
- [x] A wake runs the job at once. Without a scheduler in the process (the CLI), it does nothing, and the next tick finds the decision.
- [x] `/health` returns 503 for a decision open over an hour, with no database call per request. It also reports the oldest open decision's age.
- [x] After a shutdown signal, the worker starts no new decision.
- An empty queue costs one query and no graph session. A failing job is recorded in `job_runs` at most every five minutes, because it runs every fifteen seconds. The query itself is tested on Neon.

**Verification:** `uv run pytest tests/test_scheduler.py tests/test_api.py tests/test_liveness.py`; `.\tasks.ps1 check`.

**Dependencies:** 16.7 · **Files:** `app/jobs/scheduler.py`, `app/obs/liveness.py`, `app/api.py`, `tests/test_scheduler.py`, `tests/test_api.py` · **Scope:** M

### Task 16.10: `approve.py` onto the queue

**Description:**
- Decisions go through `decide(via="cli")`. The CLI then waits for the outcome and prints it, or says the worker did not answer in time.
- `--sweep-all` enqueues a `sweep` for every pending proposal and waits for the queue to drain.
- `--list` shows each proposal's revision, status and `dry_run`.

**Acceptance criteria:**
- [x] Confirm, edit, cancel and sweep all go through `decide`, and the CLI never resumes a thread itself. It decides on the proposal's current revision, because the operator names a message, not a revision.
- [x] The outcome is printed, or the time-out is stated. The wait is tested with a fake clock, and gives up rather than hangs when no app is running.
- [x] `--sweep-all` queues a sweep for every pending proposal and waits for the queue to drain. It runs reconciliation first, so a parked thread with a missing row is swept too. A proposal already being decided keeps its decision.
- The CLI uses an autocommit connection, so the worker in another process sees each decision as soon as it is recorded. It needs no Google credentials except for `--reconcile` and `--sweep-all`.

**Verification:** `uv run pytest tests/test_approve.py`; `.\tasks.ps1 check`.

**Dependencies:** 16.9 · **Files:** `app/jobs/approve.py`, `tests/test_approve.py` · **Scope:** S

### Task 16.11: Telegram onto the queue

**Description:**
- Telegram's button data gains the revision. A card from before M16, without one, is refused with a pointer to the web app.
- The handler records the decision through `decide(via="telegram")` and answers "Queued".
- The webhook becomes a plain `def` handler.

**Acceptance criteria:**
- [x] A stale Telegram card is refused. So is a card or an edit prompt from before M16, with a pointer to the web app.
- [x] The handler never resumes a thread. It records the decision, answers "Queued", and wakes the worker.
- [x] ~~The webhook handler is not a coroutine function.~~ **Changed:** the webhook stays `async` and runs its database work in the thread pool. A plain `def` handler would parse the body before the secret check, because FastAPI reads a body parameter before the handler runs. It needs no graph session now.
- [x] A test fails if any module other than `app/channel/worker.py` calls `resume` or `redrive` (`tests/test_one_resumer.py`). This enforces D1's rule automatically rather than by a search at a checkpoint (review of the queue, finding 1).
- The announce hook now gets the stored record, not the raw payload. Telegram needs the revision for its buttons, and the record also keeps the model's reasoning out of the chat: review finding 8, done here rather than in 16.13.

**Verification:** `uv run pytest tests/test_telegram.py tests/test_api.py`; `.\tasks.ps1 check`.

**Dependencies:** 16.5 · **Files:** `app/telegram/cards.py`, `app/telegram/handler.py`, `app/api.py`, `tests/test_telegram.py` · **Scope:** M

### Checkpoint: one path

- [ ] `.\tasks.ps1 check` is green, and the integration tests pass on Neon.
- [ ] `GraphSession.resume` is called only from `app/channel/worker.py`, checked with a search.
- [ ] End to end on Neon: a planted email parks, `approve` confirms it, and the ledger, `proposals` and `decisions` agree. This needs the dev Google token; without it, run a fake graph and say so.

---

## Phase 3 · API, push and alerts

### Task 16.12: The web API on Fly

**Description:**
- Routes:
  - `POST /api/decisions` (202 queued, 409 stale, 404 not found, 422 invalid);
  - `POST /api/push-subscriptions` and `DELETE /api/push-subscriptions` (upsert by endpoint).
- Auth is `Authorization: Bearer <WEB_API_SECRET>`, compared with `compare_digest`. An unset or blank secret means 503.
- `WEB_API_SECRET` and `VAPID_PRIVATE_KEY` join `_blank_secret_is_unset`.
- The handlers are plain `def`, and each opens its own connection.

**Acceptance criteria:**
- [ ] A missing or wrong secret gets 401; an unset or blank secret gets 503.
- [ ] The handlers are sync.
- [ ] A decision request returns without waiting on the graph, and wakes the worker.
- [ ] A subscription is stored once per endpoint and can be removed.

**Verification:** `uv run pytest tests/test_web_api.py tests/test_config.py`; `.\tasks.ps1 check`.

**Dependencies:** 16.5, 16.9 · **Files:** `app/web_api.py`, `app/api.py`, `app/config.py`, `tests/test_web_api.py`, `tests/test_config.py` · **Scope:** M

### Task 16.13: The `Channel` protocol

**Description:**
- `Channel` has `announce_proposal(message_id)` and `alert(code)`.
- `TelegramChannel` wraps today's notify code.
- The park step and the scheduler announce through every configured channel. Exceptions and time-outs are isolated per channel.

**Acceptance criteria:**
- [ ] A failing or hanging channel does not stop the others (fake channels).
- [ ] With Telegram unconfigured, nothing breaks.
- [ ] Poll no longer calls Telegram directly.
- [x] Channels receive the stored record, never the raw interrupt payload (review of the queue, finding 8). Done in 16.11, because Telegram's buttons needed the revision.

**Verification:** `uv run pytest tests/test_channels.py tests/test_poll.py`; `.\tasks.ps1 check`.

**Dependencies:** 16.4 · **Files:** `app/channel/channels.py`, `app/telegram/notify.py`, `app/channel/park.py`, `app/jobs/poll.py`, `tests/test_channels.py` · **Scope:** M

### Task 16.14: Web push

**Description:**
- Add `pywebpush`, which was approved with the spec.
- `WebPushChannel` sends a generic payload to every subscription: "A proposal needs you" or "Google sign-in needs attention".
- Each send uses `timeout=10`, a `ttl` of 24 hours and `Urgency: high`. The VAPID `sub` is the app's URL.
- A 404 or 410 deletes the subscription.
- `.\tasks.ps1 vapid` generates the key pair.

**Acceptance criteria:**
- [ ] No payload carries proposal content: seeded strings never appear.
- [ ] `webpush` is called with the timeout, the `ttl` and the urgency.
- [ ] A 410 deletes the subscription.
- [ ] `uv lock --check` passes, and only `pywebpush` and its dependencies were added.

**Verification:** `uv run pytest tests/test_webpush.py`; `.\tasks.ps1 check`.

**Dependencies:** 16.12, 16.13 · **Files:** `pyproject.toml` (and `uv.lock`), `app/channel/webpush.py`, `tasks.ps1`, `tests/test_webpush.py` · **Scope:** M

### Task 16.15: Token alerts, once per state change

**Description:**
- **`check_token` fix.** It alerts from `token_state`, as `/health` reads it. Today a production token would get the countdown alert every 12 hours from day five.
- **Alerts:** a Testing token within two days of expiry, a token turning `expired`, and failover to the standby.
- **Delivery.** An `alerts_sent` row is written only after at least one push service returns 2xx. With no subscriptions, or a failed send, the next check retries.
- **Health.** `/health` shows the subscription count, refreshed hourly into memory.

**Acceptance criteria:**
- [ ] A production token never triggers the countdown alert.
- [ ] Each alert code goes out once per state change, including across a restart.
- [ ] With no subscriptions, nothing is recorded, and the next check retries.
- [ ] `/health` shows the subscription count.

**Verification:** `uv run pytest tests/test_alerts.py tests/test_scheduler.py`; `.\tasks.ps1 check`.

**Dependencies:** 16.13, 16.14 · **Files:** `app/jobs/scheduler.py`, `app/channel/alerts.py`, `app/api.py`, `tests/test_alerts.py`, `tests/test_scheduler.py` · **Scope:** M

### Checkpoint: the backend is done

- [ ] `.\tasks.ps1 check` is green, and the integration tests pass on Neon.
- [ ] The owner reviews before the web app is built.

---

## Phase 4 · The web app

### Task 16.16: The read-only role

**Description:**
- `008_web_reader.sql` creates `web_reader` `NOLOGIN` inside a `DO` block, and grants `SELECT` on the tables the app shows.
- The test gets the role with `GRANT web_reader TO CURRENT_USER` and `SET ROLE`, so no password is involved.
- **[owner]** Sets the `LOGIN PASSWORD` in Supabase's SQL editor at deploy (16.23).

**Acceptance criteria:**
- [ ] 008 applies and re-runs on Neon.
- [ ] As `web_reader`, `SELECT` works and `INSERT` fails.

**Verification:** the integration tests on Neon.

**Dependencies:** 16.4 · **Files:** `migrations/008_web_reader.sql`, `tests/test_web_reader.py` · **Scope:** S

### Task 16.17: The timeline

**Description:**
- `/` becomes the timeline: pending proposals first, then recent decisions.
- A card shows the title, the time in the owner's zone, attendees, conflicts, reviewer issues, a `dry run` badge and the revision.
- A `deciding` card shows "Applying…", and the page re-reads every 3 seconds while any decision is open.
- The current overview moves to `/analytics`, behind the same sign-in.

**Acceptance criteria:**
- [ ] Pending, deciding, decided and failed proposals render from seeded rows.
- [ ] The page re-reads only while a decision is open.

**Verification:** the web checks; a local run against Neon.

**Dependencies:** 16.1, 16.16 · **Files:** `dashboard/src/app/page.tsx`, `dashboard/src/app/analytics/page.tsx`, `dashboard/src/components/ProposalCard.tsx`, `dashboard/src/lib/proposals.ts`, `dashboard/src/app/layout.tsx` · **Scope:** M

### Task 16.18: Deciding from the card

**Description:**
- Server actions for **Confirm**, **Edit** (with a correction box, hidden at revision 3) and **Cancel**.
- Each action calls `auth()` and requires the owner. It then posts to Fly's `/api/decisions` with the Bearer secret from the server environment.
- A stale answer shows the latest version.

**Acceptance criteria:**
- [ ] Every action checks the owner first. The check lives in `access.ts` and is tested.
- [ ] `WEB_API_SECRET` never appears in the browser bundle: a search of the build output finds nothing.
- [ ] Against a local API on Neon, confirm, edit and cancel each end with records that agree, and a stale card is refused.

**Verification:** the web checks; the local run.

**Dependencies:** 16.12, 16.17 · **Files:** `dashboard/src/app/actions.ts`, `dashboard/src/lib/fly.ts`, `dashboard/src/components/ProposalCard.tsx`, `dashboard/src/components/EditForm.tsx` · **Scope:** M

### Task 16.19: Push in the web app

**Description:**
- **Service worker.** It caches static assets only. A push shows a generic notification, and a tap opens `/`.
- **Subscription.** The app asks for permission and subscribes with the VAPID public key. It re-posts the subscription through a server action every time it opens, and `pushsubscriptionchange` re-subscribes.
- **iPhone.** In Safari, outside the installed app, the page explains "Add to Home Screen".

**Acceptance criteria:**
- [ ] The caching rule admits only static assets (node test).
- [ ] The subscription is re-posted on every open.
- [ ] A push from a local API reaches a desktop browser, and a tap opens `/`.

**Verification:** the web checks; the local run.

**Dependencies:** 16.14, 16.18 · **Files:** `dashboard/public/sw.js`, `dashboard/public/sw-rules.js` (and its test), `dashboard/src/components/PushSetup.tsx`, `dashboard/src/app/actions.ts` · **Scope:** M

### Task 16.20: Web checks in CI

**Description:** A CI job runs `npm ci`, `npm run typecheck`, `npm run build` and `npm test` in `dashboard/` on Node 24.

**Acceptance criteria:**
- [ ] The job runs on every push, and it is green.

**Verification:** the CI run on the pushed branch.

**Dependencies:** 16.1 · **Files:** `.github/workflows/ci.yml`, `dashboard/package.json` · **Scope:** XS

### Checkpoint: ready to deploy

- [ ] The Python and web checks are green, locally and in CI.
- [ ] Locally: sign in, see a planted proposal, decide it, and receive a push in a desktop browser.
- [ ] The owner reviews before the deploy.

---

## Phase 5 · Retention, runbook, deploy and exit

### Task 16.21: Retention for the new records

**Description:**
- The purge clears `proposals.payload` and `decisions.correction` 7 days after the ledger row reaches a final status or FAILED. The clock is the ledger's.
- `action_type`, `pipeline_version`, `via`, `revision`, `outcome` and the timings stay.
- Expired pairing codes are deleted.

**Acceptance criteria:**
- [ ] Content is cleared on day 7 and not before, for final and FAILED rows. An open proposal is never cleared.
- [ ] M24's columns survive.

**Verification:** `uv run pytest tests/test_purge.py`; the integration tests on Neon; `.\tasks.ps1 check`.

**Dependencies:** 16.4 · **Files:** `app/jobs/purge.py`, `tests/test_purge.py` · **Scope:** S

### Task 16.22: Runbook and environment

**Description:**
- A DEPLOY.md section covering:
  - Vercel;
  - the separate sign-in project;
  - the `web_reader` password step;
  - VAPID keys and `WEB_API_SECRET`;
  - revoking a device by rotating `AUTH_SECRET`, which signs out every device.
- `.env.example` entries for both apps.

**Acceptance criteria:**
- [ ] Following the section needs no step outside it. Checked by a read-through against the spec.

**Verification:** the read-through, recorded in the running notes.

**Dependencies:** 16.12–16.19 · **Files:** `docs/DEPLOY.md`, `.env.example`, `dashboard/.env.example` · **Scope:** S

### Task 16.23: Deploy [owner + agent]

**Description:** After M15 closes (task 21), unless the owner decides otherwise:
- Apply 007 and 008 on Supabase.
- **[owner]** Set the `web_reader` password.
- Set the Fly secrets (`WEB_API_SECRET` and the VAPID keys), and deploy with `fly deploy --ha=false`.
- Point Vercel at the Fly API and the `web_reader` URL, and deploy.
- **[owner]** Open the app on both phones and allow notifications.

**Acceptance criteria:**
- [ ] `/health` returns 200, with at least two subscriptions and no open decision.
- [ ] The timeline shows the proposals D3 created at boot.

**Verification:** `curl -i` on `/health`; the owner's phones.

**Dependencies:** 16.21, 16.22, M15 task 21 · **Files:** the running notes · **Scope:** S

### Task 16.24: Pairing — only if 16.2 failed on iPhone

**Description:**
- `POST /api/pairing/codes` gives a signed-in session a 6-digit code. The code lasts 5 minutes and is stored as its SHA-256.
- `POST /api/pairing/redeem` allows 5 attempts, after which the code is dead.
- A successful redeem returns a single-use grant, which the installed app turns into its own session.

**Acceptance criteria:**
- [ ] A code dies after 5 attempts, and after 5 minutes.
- [ ] A grant works once.
- [ ] The owner signs in inside the installed iPhone app through pairing.

**Verification:** `uv run pytest tests/test_pairing.py`; the web checks; the owner's iPhone.

**Dependencies:** 16.2, 16.12 · **Files:** `app/channel/pairing.py`, `app/web_api.py`, `dashboard/src/app/pair/page.tsx`, `tests/test_pairing.py` · **Scope:** M

### Task 16.25: Exit evidence [owner + agent]

**Description:** Run the spec's five exit criteria on both phones in dry run, and record each one with its evidence in the running notes. Then update INDEX.

**Acceptance criteria:**
- [ ] All five exit criteria hold, each with linked evidence.
- [ ] No decision stays open for more than an hour during the run.

**Verification:** re-read the exit criterion against the running notes. The owner reviews before M16 is marked done.

**Dependencies:** 16.23, and 16.24 if it was built · **Files:** `docs/plans/M16-web-channel.md`, `docs/plans/INDEX.md` · **Scope:** XS

### Checkpoint: M16 complete

- [ ] Exit criteria 1–5 are met.
- [ ] The owner has signed off.
