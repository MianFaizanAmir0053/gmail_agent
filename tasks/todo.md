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
  - `fly deploy --ha=false` and `fly scale count 1`, **from the tag `m15-day0`** (`adac00e`, M15's code-complete commit), not the branch head, which by then carries M16's queue. Decided by the owner on 2026-10-01; see DEPLOY.md §3;
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

- [x] `.\tasks.ps1 check` is green, and the integration tests pass on Neon: 125 passed on 2026-10-01; the one failure is the known one.
- [x] `GraphSession.resume` is called only from `app/channel/worker.py`. Enforced by `tests/test_one_resumer.py` rather than a one-off search.
- [ ] End to end on Neon with a planted email: **not run.** The dev Google token is not on this machine; minting it is M15's owner step (`.\tasks.ps1 reauth`). The same path ran on Neon with a fake graph instead: park, `decide`, worker, settled, for confirm, edit and cancel (`tests/test_worker.py`).

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
- [x] A missing or wrong secret gets 401; an unset or blank secret gets 503. The secret is checked before the body is read: an unauthenticated request with a broken body gets 401, not 422.
- [x] ~~The handlers are sync.~~ **Changed, as for the webhook in 16.11:** the handlers are `async`, check the secret first, and run their database work in the thread pool.
- [x] A decision request returns without waiting on the graph (202, 409 with the current revision, 404, 422), and wakes the worker. A malformed request, including a `sweep` or an overlong correction, is refused before the queue sees it, and a 422 never echoes the input.
- [x] A subscription is stored once per endpoint, refreshed on repeat, and can be removed. A non-https endpoint is refused.

**Verification:** `uv run pytest tests/test_web_api.py tests/test_config.py`; `.\tasks.ps1 check`.

**Dependencies:** 16.5, 16.9 · **Files:** `app/web_api.py`, `app/api.py`, `app/config.py`, `tests/test_web_api.py`, `tests/test_config.py` · **Scope:** M

### Task 16.13: The `Channel` protocol

**Description:**
- `Channel` has `announce_proposal(message_id)` and `alert(code)`.
- `TelegramChannel` wraps today's notify code.
- The park step and the scheduler announce through every configured channel. Exceptions and time-outs are isolated per channel.

**Acceptance criteria:**
- [x] A failing or hanging channel does not stop the others (fake channels). Calls run on a small shared thread pool with a 15-second deadline, in parallel.
- [x] With Telegram unconfigured, nothing breaks: no channel is built, and announcing is a no-op. Telegram needs both a bot token and an allowlist.
- [x] Poll no longer calls Telegram directly. Poll, the worker and reconciliation announce through `configured_channels(settings)`. Token alerts move in 16.15.
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
- [x] No payload carries proposal content: seeded strings never appear, and the message id does not either.
- [x] `webpush` is called with the timeout, the `ttl` and the urgency. Each send gets a fresh claims dict, because `webpush` writes the push service's audience into the one it is given (verified in its source).
- [x] A 404 or 410 deletes the subscription; a passing failure (503) keeps it. Logs name the push service, never the endpoint.
- [x] `uv lock --check` passes, and only `pywebpush` and its dependencies were added: `py-vapid`, `http-ece`, and `aiohttp` with its stack, which `pywebpush` 2.x requires. `.\tasks.ps1 vapid` prints a pair that `pywebpush` loads and that browsers accept (a 65-byte uncompressed point).
- Push turns on only when both `VAPID_PRIVATE_KEY` and `WEB_APP_URL` are set. The app's URL is the VAPID subject.

**Verification:** `uv run pytest tests/test_webpush.py`; `.\tasks.ps1 check`.

**Dependencies:** 16.12, 16.13 · **Files:** `pyproject.toml` (and `uv.lock`), `app/channel/webpush.py`, `tasks.ps1`, `tests/test_webpush.py` · **Scope:** M

### Task 16.15: Token alerts, once per state change

**Description:**
- **`check_token` fix.** It alerts from `token_state`, as `/health` reads it. Today a production token would get the countdown alert every 12 hours from day five.
- **Alerts:** a Testing token within two days of expiry, a token turning `expired`, and failover to the standby.
- **Delivery.** An `alerts_sent` row is written only after at least one push service returns 2xx. With no subscriptions, or a failed send, the next check retries.
- **Health.** `/health` shows the subscription count, refreshed hourly into memory.

**Acceptance criteria:**
- [x] A production token never triggers the countdown alert. The state is read exactly as `/health` reads it: the helpers moved to `app/obs/token_report.py`, and `/health` behaves the same.
- [x] Each alert code goes out once per state change, including across a restart. `alerts_sent` is keyed by the code and the token's `issued_at`, so a new token alerts afresh (tested on Neon).
- [x] With no subscriptions, nothing is recorded, and the next check retries. The check now runs hourly instead of every 12 hours, so a retry is not half a day away.
- [x] `/health` shows the subscription count, refreshed by the hourly check. `null` means not yet counted, and zero is visible without being a failure.
- An expired token with a usable standby sends `standby_in_use` (mail still flows) rather than `token_expired` (mail stopped).

**Verification:** `uv run pytest tests/test_alerts.py tests/test_scheduler.py`; `.\tasks.ps1 check`.

**Dependencies:** 16.13, 16.14 · **Files:** `app/jobs/scheduler.py`, `app/channel/alerts.py`, `app/api.py`, `tests/test_alerts.py`, `tests/test_scheduler.py` · **Scope:** M

### Checkpoint: the backend is done

- [x] `.\tasks.ps1 check` is green, and the integration tests pass on Neon (129 passed on 2026-10-01; the one failure is the known one). After the review fixes: 741 unit tests pass, and the 17 affected integration tests pass on Neon.
- [x] A fresh-context review read the backend against D3, D4, D6 and D7. It found no HIGH issues and eight smaller ones, all fixed (`cc1623a`, recorded in the spec).
- [ ] The owner reviews before the web app is built.

---

## Phase 4 · The web app

### Task 16.16: The read-only role

**Description:**
- `008_web_reader.sql` creates `web_reader` `NOLOGIN` inside a `DO` block, and grants `SELECT` on the tables the app shows.
- The test gets the role with `GRANT web_reader TO CURRENT_USER` and `SET ROLE`, so no password is involved.
- **[owner]** Sets the `LOGIN PASSWORD` in Supabase's SQL editor at deploy (16.23).

**Acceptance criteria:**
- [x] 008 applies and re-runs on Neon. The role is created `NOLOGIN` until the owner sets its password.
- [x] As `web_reader`, `SELECT` works on exactly what the app shows: `runs`, `spans` and `eval_runs` for the analytics pages, `proposals` and `decisions` for the timeline. Every other table is refused, including the ledger, the checkpoints and the push subscriptions. `INSERT`, `UPDATE` and `DELETE` fail (16 tests on Neon).

**Verification:** the integration tests on Neon.

**Dependencies:** 16.4 · **Files:** `migrations/008_web_reader.sql`, `tests/test_web_reader.py` · **Scope:** S

### Task 16.17: The timeline

**Description:**
- `/` becomes the timeline: pending proposals first, then recent decisions.
- A card shows the title, the time in the owner's zone, attendees, conflicts, reviewer issues, a `dry run` badge and the revision.
- A `deciding` card shows "Applying…", and the page re-reads every 3 seconds while any decision is open.
- The current overview moves to `/analytics`, behind the same sign-in.

**Acceptance criteria:**
- [x] Pending, deciding, decided and failed proposals render from seeded rows. This was checked in the browser pane against Neon, signed in with a session minted locally from the dummy test secret, on a desktop and a 375-pixel phone width:
  - times show in the owner's zone, and a card at revision 3 offers no Edit;
  - the card being decided shows "Applying…";
  - recent decisions show their outcome. The seeded rows were removed afterwards.
- [x] The page re-reads only while a decision is open (a unit test, and seen working: settling the decision in the database turned "Applying…" into the outcome without a reload).
- Found in the browser: each re-read also prefetched every nav page, and so ran their queries every 3 seconds. Nav links no longer prefetch, and only the timeline itself re-reads.

**Verification:** the web checks; a local run against Neon.

**Dependencies:** 16.1, 16.16 · **Files:** `dashboard/src/app/page.tsx`, `dashboard/src/app/analytics/page.tsx`, `dashboard/src/components/ProposalCard.tsx`, `dashboard/src/lib/proposals.ts`, `dashboard/src/app/layout.tsx` · **Scope:** M

### Task 16.18: Deciding from the card

**Description:**
- Server actions for **Confirm**, **Edit** (with a correction box, hidden at revision 3) and **Cancel**.
- Each action calls `auth()` and requires the owner. It then posts to Fly's `/api/decisions` with the Bearer secret from the server environment.
- A stale answer shows the latest version.

**Acceptance criteria:**
- [x] Every action checks the owner first (`isOwnerSession`, tested in `access.ts`). The form rules and the owner-facing messages live in `decisionForm.ts` and are tested too. No raw server error ever reaches the page.
- [x] `WEB_API_SECRET` never appears in the browser bundle. A build with a canary secret and a canary API host found neither in `.next/static`, nor in the server build, since both are read at runtime. `fly.ts` imports `server-only`, so a client component that pulls it in fails the build.
- [x] Against a local API on Neon, in the browser:
  - Confirm queued a `confirm` (via web, revision 1), and the card showed "Applying…";
  - Edit queued an `edit` with its correction;
  - after a worker-style settle, the edit came back at revision 2;
  - a Cancel on a card whose proposal had moved to revision 2 was refused with "This proposal changed. The latest version is shown.", and recorded nothing.

  The worker itself did not run: this machine has no Google token. The decisions were settled by hand, as the worker's tests show it does.
- Found in the browser: the card order was not total, so the 3-second re-read could move cards under the owner's thumb, and a tap landed on another card's button. The order now breaks ties by message id.

**Verification:** the web checks; the local run.

**Dependencies:** 16.12, 16.17 · **Files:** `dashboard/src/app/actions.ts`, `dashboard/src/lib/fly.ts`, `dashboard/src/components/ProposalCard.tsx`, `dashboard/src/components/EditForm.tsx` · **Scope:** M

### Task 16.19: Push in the web app

**Description:**
- **Service worker.** It caches static assets only. A push shows a generic notification, and a tap opens `/`.
- **Subscription.** The app asks for permission and subscribes with the VAPID public key. It re-posts the subscription through a server action every time it opens, and `pushsubscriptionchange` re-subscribes.
- **iPhone.** In Safari, outside the installed app, the page explains "Add to Home Screen".

**Acceptance criteria:**
- [x] The caching rule admits only static assets. The node test runs the worker's own file. In the browser, after visiting `/` and `/analytics`, the cache held 9 files, all under `/_next/static/`, and no page or API answer.
- [x] The subscription is re-posted on every open (`PushSetup`). It also goes through a same-origin route, so the service worker can re-post after `pushsubscriptionchange`. The route checks the owner and forwards to Fly with the server's secret.
- [ ] A push from a local API reaches a desktop browser, and a tap opens `/`: **not possible here.** The browser pane blocks notifications and has no push service. Verified instead: the worker registers and takes control, and the page reports the blocked state in words. The real push is exit criterion 2, on both phones, at deploy (16.23, 16.25).
- Found in the browser: the caching rule's separate file sat behind the sign-in gate, so a fetch without a session would have broken the worker. The rule now lives inside `sw.js`, which keeps the spec's exact four exempt paths.

**Verification:** the web checks; the local run.

**Dependencies:** 16.14, 16.18 · **Files:** `dashboard/public/sw.js`, `dashboard/public/sw-rules.js` (and its test), `dashboard/src/components/PushSetup.tsx`, `dashboard/src/app/actions.ts` · **Scope:** M

### Task 16.20: Web checks in CI

**Description:** A CI job runs `npm ci`, `npm run typecheck`, `npm run build` and `npm test` in `dashboard/` on Node 24.

**Acceptance criteria:**
- [x] The job runs on every push, and it is green. Run 36839072810 on `83bb537`, 2026-10-01: web, check and docker all succeeded. The earlier runs on this branch had failed in the Python job, on two tests: the retrieval test `main` had already fixed, now merged in; and a test that expected Postgres 18's RESTRICT error where CI's Postgres 16 raises a foreign-key error.

**Verification:** the CI run on the pushed branch.

**Dependencies:** 16.1 · **Files:** `.github/workflows/ci.yml`, `dashboard/package.json` · **Scope:** XS

### Checkpoint: ready to deploy

- [x] The Python and web checks are green, locally and in CI. Locally on 2026-10-01: 742 unit tests, lint, format and mypy; 58 web tests, typecheck and build; the integration tests on Neon, the retrieval test included once `main`'s fix was merged. CI: run 36839072810, all three jobs green.
- [ ] Locally: sign in, see a planted proposal, decide it, and receive a push in a desktop browser. Done on 2026-10-01 without the push: the browser pane has no push service, so the push is proven on the phones at deploy (16.23, 16.25).
- [ ] The owner reviews before the deploy.

A sixth adversarial review read the web app on 2026-10-01: one HIGH issue (a tap could land on a card that moved into place) and fourteen smaller ones. All were addressed: the fixes, and the three parts left as they are with reasons, are in the spec's Review section.

---

## Phase 5 · Retention, runbook, deploy and exit

### Task 16.21: Retention for the new records

**Description:**
- The purge clears `proposals.payload` and `decisions.correction` 7 days after the ledger row reaches a final status or FAILED. The clock is the ledger's.
- `action_type`, `pipeline_version`, `via`, `revision`, `outcome` and the timings stay.
- Expired pairing codes are deleted.

**Acceptance criteria:**
- [x] Content is cleared on day 7 and not before, for final and FAILED rows. An open proposal is never cleared. Tested at day 6 and day 8 for REJECTED and FAILED. A pending card and a deciding one keep their content even beside a final ledger, aged 30 days.
- [x] M24's columns survive. The test compares every kept column of both tables before and after, timestamps included.
- [x] Expired pairing codes are deleted, and a live one is kept.

**Verification:** `uv run pytest tests/test_purge.py`; the integration tests on Neon; `.\tasks.ps1 check`.
- 2026-10-01: `tests/test_purge.py` on Neon, 11 passed, twice. The full integration run had one failure and one error. The failure is the known `test_vector_mode_cannot_reach_a_keyword_only_match`. The error was in the teardown of an existing purge test, where Neon closed the checkpointer's connection; the file passed in full on its own before and after. Locally, 741 unit tests passed, and lint, format and mypy were clean.

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
- [x] Following the section needs no step outside it. Checked by a read-through against the spec. `docs/DEPLOY.md` §9 covers the sign-in project, the keys, the `web_reader` role, Vercel, Fly, the deploy-day order, revoking a device, and what stays as it was. The read-through found three steps missing, all now in: the Data API off, SSL enforced (both in §1.3, since M15's day 0 needs them too), and Supabase's CA certificate for the web app.
- [x] `.env.example` entries for both apps: `WEB_API_SECRET`, `VAPID_PRIVATE_KEY` and `WEB_APP_URL` for Fly; `NEXT_PUBLIC_VAPID_PUBLIC_KEY` and `DATABASE_CA_CERT` for the web app.

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

Built on 2026-10-01, ahead of the iPhone test, because the owner moved every test to the end. It is switched off (`PAIRING_ENABLED=false` on Fly and Vercel) until that test shows Google sign-in failing inside the installed app.

**Acceptance criteria:**
- [x] A code dies after 5 attempts, and after 5 minutes. `tests/test_pairing.py` on Neon, 14 passed. Two tests were checked against deliberately broken code: without the issuing lock, two live codes; without `FOR UPDATE`, the right code redeemed twice.
- [x] A grant works once. The redeemed code is the grant: Auth.js's `authorize` redeems it on the server and issues the session, so there is no separate grant step.
- [ ] The owner signs in inside the installed iPhone app through pairing: at the end tests, and only if Google sign-in fails there.

**Verification:** `uv run pytest tests/test_pairing.py`; the web checks; the owner's iPhone.
- 2026-10-01: 784 unit tests and the full integration suite on Neon (174) passed; 68 web tests, typecheck and build passed. Locally, against the Neon test database with throwaway secrets: the sign-in page showed the code box, a wrong and an ended code were refused with Auth.js's generic message, the code from `/pair` signed the browser in as the owner, and with pairing off only Google was offered.

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

---

# M17 · Action policy — tasks

Spec: [`docs/plans/M17-action-policy.md`](../docs/plans/M17-action-policy.md). Every task leaves `.\tasks.ps1 check` green. Integration tests run on the Neon test database.

## Phase 1 · Bind approvals to what runs

### Task 17.1: Records and the audit writer

**Description:** Migration `010_action_policy.sql` (D8): `control`, `outbound_actions` (decision foreign key restricting deletes), `confirmed_contacts`, `model_spend`, `audit_log` with its append-only trigger, `proposals.tool` and `proposals.args_hash`, and `web_reader`'s reads. `app/policy/audit.py` writes rows without content. The test fixture and `poll --reset` delete `outbound_actions` before `decisions`.

**Acceptance criteria:**
- [x] The migration applies and can be re-run.
- [x] `UPDATE` and `DELETE` on `audit_log` fail; a seeded email string never reaches it.
- [x] `web_reader` reads `control`, `confirmed_contacts` and `audit_log`, and writes nothing.

**Verification:** `uv run pytest tests/test_audit.py tests/test_web_reader.py`; the integration tests on Neon.

**Dependencies:** None · **Files:** `migrations/010_action_policy.sql`, `app/policy/audit.py`, `app/jobs/poll.py`, `tests/conftest.py`, tests · **Scope:** S

### Task 17.2: The keyed hash, computed before parking

**Description:** `app/policy/hashing.py` (D2): the tool for an extraction, the arguments `act` runs (one function, shared with `act`), and the HMAC over the canonical form, keyed from `FERNET_KEY`. Every proposal row write computes `tool` and `args_hash` from the thread's payload under the current code; nothing about the hash lives in the graph state. The owner's aliases join `OWNER_EMAIL` in being stripped from guests.

**Acceptance criteria:**
- [x] The hash is stable under key order, time zone, and guest order and case, and changes with any argument, the calendar or the hash version.
- [x] Nothing in `await_approval` calls Gmail or the database.
- [x] A different `FERNET_KEY` gives a different hash.

**Verification:** `uv run pytest tests/test_hashing.py tests/test_graph.py tests/test_park.py`.

**Dependencies:** 17.1 · **Files:** `app/policy/hashing.py`, `app/graph/nodes.py`, `app/graph/state.py`, `app/extraction/payloads.py`, `app/channel/park.py`, tests · **Scope:** M

### Task 17.3: Checkpoints written before moving on

**Description:** Every graph invocation (`start`, resume, re-drive) uses `durability="sync"` (D3).

**Acceptance criteria:**
- [x] A test fails if an invocation in `app/graph/runner.py` omits it.

**Verification:** `uv run pytest tests/test_runner.py tests/test_worker.py`.

**Dependencies:** None · **Files:** `app/graph/runner.py`, tests · **Scope:** XS

### Task 17.4: What the owner saw travels with the decision

**Description:** The web card's form, the Telegram buttons and `approve --action confirm --expect` carry the hash prefix and the mode. `decide()` refuses a mismatch as stale, refuses a proposal with no hash as not ready, and inserts the `outbound_actions` row for a Confirm, in the claim's transaction. Telegram buttons sent before M17 are refused with a pointer to the web app.

**Acceptance criteria:**
- [x] A stale hash or mode is refused on every channel; nothing is recorded.
- [x] One Confirm, one action row; Edit, Cancel and Sweep create none; two concurrent Confirms create one (Neon).

**Verification:** `uv run pytest tests/test_decide.py tests/test_web_api.py tests/test_telegram.py tests/test_approve.py`; the web checks.

**Dependencies:** 17.2 · **Files:** `app/channel/decide.py`, `app/web_api.py`, `app/telegram/cards.py`, `app/telegram/handler.py`, `app/jobs/approve.py`, `dashboard/src/components/DecisionButtons.tsx`, `dashboard/src/lib/decisionForm.ts`, tests · **Scope:** M

### Task 17.5: The registry, and `act` through it

**Description:** `app/policy/registry.py` (D1, D2): tiers, the three calendar tools, `execute` with `DRY_RUN`, the pause check at entry, and the action row's checks. The worker resumes a Confirm with the action's id and nonce; `act` calls the registry; every INTERNAL and EXTERNAL attempt is audited.

**Acceptance criteria:**
- [x] No T3 tool is registered, and nothing but the registry (and `app/google/smoke.py`) calls the Calendar client's writes.
- [x] A changed argument, another decision's nonce, and a mismatched mode at execution are each refused and audited.
- [x] Under `DRY_RUN` the provider is never called.

**Verification:** `uv run pytest tests/test_registry.py tests/test_worker.py tests/test_graph.py`.

**Dependencies:** 17.4 · **Files:** `app/policy/registry.py`, `app/graph/nodes.py`, `app/channel/worker.py`, tests · **Scope:** M

### Task 17.6: Calendar writes that can be finished

**Description:** The deterministic id, the lookup before any later attempt, and the `409` path (D3). `step_for` re-drives `act` when the action is `approved`, `executing`, `done` or `dry_run`; giving up resolves an `executing` action by looking the event up.

**Acceptance criteria:**
- [x] With a crash injected after each step (fake Calendar, Neon), every case ends with exactly one event, the ledger `CREATED`, and no action left `executing`.
- [x] A `409` returns the existing event; a deleted event is not recreated.
- [x] `tests/test_one_resumer.py` still passes.
- [x] Found on the way: the probe (`app/jobs/calendar_probe.py`) is built here, with D3. The alert and the `/health` count for a write that could not be confirmed move to 17.11; the purge's week-old clear of a stored request waits for the M20 merge.
- [x] A fresh-context review of 17.5–17.6 found no path that writes under `DRY_RUN` or skips a check. Its findings are folded in: tools are checked only for a new action, giving up never fails a write that may exist, a `400` fails at once, a `404` counts only once the calendar is readable, any action is re-driven with its reason, and the worker applies nothing while paused.

**Verification:** `uv run pytest tests/test_calendar.py tests/test_worker.py tests/test_one_resumer.py`; Neon.

**Dependencies:** 17.3, 17.5 · **Files:** `app/google/calendar.py`, `app/tools/calendar_tool.py`, `app/channel/worker.py`, tests · **Scope:** M

### Task 17.7: Checks before a Confirm, and legacy proposals

**Description:** Before applying a Confirm whose action is `approved`, the worker checks the mode and recomputes the hash. A mode that differs expires the proposal (D2: a sweep, "made under another mode"); a hash that differs refuses the action, settles the decision as `no_effect`, and returns the proposal to `pending` with what is true now and the next generation. An M16 Confirm open at deploy is returned the same way ("approve again"). A reconciliation pass gives pending legacy proposals their tool and hash, and expires pending proposals made under the other mode. No action is left `approved` once its decision settles.

**Acceptance criteria:**
- [x] A proposal approved under dry run, applied after `DRY_RUN` is off, is expired ("made under another mode") and runs nothing; the old card is then refused as stale.
- [x] A changed canonical form returns the proposal to the owner under the next generation; the old card is refused as stale, the new one accepted.
- [x] A legacy pending proposal gets a hash, and can then be confirmed.
- [x] An M16 Confirm open at deploy is returned to the owner, a pending proposal made under the other mode is expired by reconciliation, and a settled decision leaves no `approved` action.

**Verification:** `uv run pytest tests/test_worker.py tests/test_reconcile.py`; Neon.

**Dependencies:** 17.6 · **Files:** `app/channel/worker.py`, `app/channel/park.py`, `app/channel/reconcile.py`, tests · **Scope:** M

### Checkpoint: bound

- [x] Checks green; integration tests on Neon.
- [x] Fresh-context review of 17.1–17.7 against D1–D3: one review of 17.5–17.6 and one of 17.7, each folded in (running notes in the spec).

## Phase 2 · Recipients

### Task 17.8: The recipient rule

**Description:** Participants from `threads.get` (D4): senders of mail received, recipients of mail sent. The comparator ignores dots and `+tags` for Gmail addresses only. `detect_conflicts` stores `outside_guests`; `decide()` refuses an unconfirmed outsider; the worker re-checks before a Confirm; the registry checks at execution. `POST /api/contacts`, and `app/jobs/contacts.py --remove`. The legacy pass computes outside guests too.

**Acceptance criteria:**
- [x] An address only in an inbound `Cc` is outside; a recipient of the owner's own mail, and a confirmed contact, pass.
- [x] An outsider blocks Confirm until allowed; an outsider added by an edit is marked.
- [x] Gmail being down fails nothing at resume.
- [x] A fresh-context review is folded in (running notes in the spec): above all, the checks read the thread Gmail files the message in, not the ledger's thread id.

**Verification:** `uv run pytest tests/test_recipients.py tests/test_decide.py tests/test_worker.py tests/test_web_api.py`; Neon.

**Dependencies:** 17.7 · **Files:** `app/policy/contacts.py`, `app/policy/participants.py`, `app/google/gmail.py`, `app/graph/nodes.py`, `app/channel/decide.py`, `app/channel/worker.py`, `app/web_api.py`, `app/jobs/contacts.py`, tests · **Scope:** L (split if it grows: the rule and its checks, then the API and CLI)

### Task 17.9: Allow on the card

**Description:** The card marks each outside guest, with **Allow**: a server action that checks the owner and calls Fly. Addresses already allowed are hidden, read from `confirmed_contacts`.

**Acceptance criteria:**
- [ ] Allow, then Confirm, works against a local API, in the browser. *Left for the owner's end tests: it needs a signed-in owner session and Fly running locally.*
- [ ] Confirm before Allow shows Fly's refusal in words. *Built and unit-tested (`describeAnswer`, the 422); seen in the browser at the end tests.*

**Verification:** the web checks; the browser.

**Dependencies:** 17.8 · **Files:** `dashboard/src/components/ProposalCard.tsx`, `dashboard/src/app/actions.ts`, `dashboard/src/lib/fly.ts`, `dashboard/src/lib/timeline.ts`, `dashboard/src/lib/proposals.ts`, tests · **Scope:** S

## Phase 3 · Budget, pause and the record

### Task 17.10: One metered path for model calls

**Description:** `app/policy/models.py` (D5): the wrapper around google-genai and the Gateway call, built wherever a client is built today; `model_spend` rows; the gate's refusals (`UnpricedModel`, `BudgetExhausted`); embedding estimates; search re-raising budget errors. A call-site test like `test_one_resumer.py`.

**Acceptance criteria:**
- [x] A model call outside the wrapper fails the call-site test.
- [x] An unpriced model is refused before any call.
- [x] Every metered call writes one `model_spend` row with no content.

**Verification:** `uv run pytest tests/test_models.py tests/test_budget.py`.

**Dependencies:** 17.1 · **Files:** `app/policy/models.py`, `app/policy/budget.py`, `app/obs/pricing.py`, `app/extraction/llm.py`, `app/extraction/pipeline.py`, `app/extraction/evaluation.py`, `app/agents/reviewer.py`, `app/rag/embed.py`, `app/rag/search.py`, `app/graph/runner.py`, tests · **Scope:** L (split if it grows: the wrapper and the call-site test, then each call site)

### Task 17.11: Where work stops, and what shows

**Description:** Poll's `allows_new_work()` and the reserve; the worker holding edits; ingestion stopping; `control.budget_state`; alerts at 80% and 100%, once per month and cap; `/health` fields, 503 for an unpriced configured model, and held decisions not counted as stuck. From 17.6: one alert when a calendar write cannot be confirmed ("A calendar write could not be confirmed"), and `/health` counting those decisions apart from stuck ones.

**Acceptance criteria:**
- [x] At the cap, poll claims nothing and the tick is still recorded as successful; Edit is held while Confirm and Cancel apply.
- [x] Each alert is sent once per month and cap; raising the cap re-arms it.
- [x] `/health` stays 200 at the cap.

**Verification:** `uv run pytest tests/test_budget.py tests/test_poll.py tests/test_worker.py tests/test_api.py tests/test_scheduler.py`.

**Dependencies:** 17.10 · **Files:** `app/policy/budget.py`, `app/jobs/poll.py`, `app/channel/worker.py`, `app/jobs/scheduler.py`, `app/obs/liveness.py`, `app/api.py`, tests · **Scope:** M

### Task 17.12: Pause, Resume and Withdraw

**Description:** `app/policy/control.py` (D6); `POST /api/pause`, `/api/resume` and `/api/decisions/withdraw`; `app/jobs/control.py`; poll, the worker, ingestion and the registry stopping; `/health`. In the web app: Pause / Resume and the banner in the header, and Withdraw on a held card. Already built in 17.6: the registry's `PausedError`, the worker applying nothing while paused, and the decisions job opening no session; withdraw requests must still be processed while paused.

**Acceptance criteria:**
- [x] Poll, the worker and ingestion stop within one tick, and resume afterwards; `/health` stays 200.
- [x] Withdraw returns a queued decision while paused.
- [ ] Pause, Resume and Withdraw work from the web app against a local API; every change is audited. (The audit half is tested; the browser check against a local API waits for the owner's end tests.)

**Verification:** `uv run pytest tests/test_control.py tests/test_worker.py tests/test_web_api.py`; the web checks; the browser.

**Dependencies:** 17.11 · **Files:** `app/policy/control.py`, `app/jobs/control.py`, `app/web_api.py`, `app/jobs/poll.py`, `app/channel/worker.py`, `app/jobs/scheduler.py`, `tasks.ps1`, `dashboard/src/app/layout.tsx`, `dashboard/src/components/ControlBar.tsx`, `dashboard/src/components/DecisionButtons.tsx`, tests · **Scope:** L (split if it grows: Fly side, then web)

### Task 17.13: The Activity page

**Description:** `/activity`: the latest 100 audit entries, read as `web_reader`, behind the owner's sign-in.

**Acceptance criteria:**
- [x] It renders every kind of entry. (Each kind's words are tested against `app/policy/audit.py`'s list; the browser check waits for the owner's end tests.)

**Verification:** the web checks; the browser.

**Dependencies:** 17.12 · **Files:** `dashboard/src/app/activity/page.tsx`, `dashboard/src/lib/activity.ts`, tests · **Scope:** S

### Task 17.14: Runbook and README

**Description:** `docs/DEPLOY.md`: the cap and how to raise it, Pause, Withdraw, contacts, the mode-check procedure for the end tests. README: the safety section. The spec's running notes.

**Acceptance criteria:**
- [x] A read-through against the spec finds no step outside the docs.

**Verification:** the read-through, recorded in the running notes.

**Dependencies:** 17.13 · **Files:** `docs/DEPLOY.md`, `README.md`, `docs/plans/M17-action-policy.md` · **Scope:** S

## Phase 4 · Review fixes

The two reviews of 2026-10-02 (the spec's running notes, "Reviews of 17.12–17.13, and of the whole module") found where Withdraw and Pause did not hold, and smaller gaps along the seams. Each task starts with a test that fails on the code at `81951cb`.

### Task 17.15: Withdraw and Pause hold at every step

**Description:**
- The worker reads a withdraw request in the same statement that takes the lease, and a settle declines any request it finds.
- Pause is read before each decision and again just before a resume or re-drive. The registry checks Pause before it reads Gmail.
- A Confirm stopped before its write can be withdrawn.
- A withdraw that fails backs off, and shows on `/health`.

**Acceptance criteria:**
- [x] A request made after the pass reads its decisions, but before the lease, is carried out. A request made while the decision is being applied is declined when the decision settles: `withdraw_declined` is audited in the same transaction.
- [x] A Pause pressed while a Confirm's guests are checked leaves its thread parked, and Withdraw then returns it. Later decisions in the same pass, Edits included, are not applied.
- [x] A Confirm stopped before its write, with its action `approved`, is withdrawn: rejected, "withdrawn by the owner", and nothing is sent. The action row is locked while it is checked.
- [x] A withdraw that raises is tried again five minutes later, and the rest of the pass goes on. A request more than an hour old counts on `/health`'s stuck clock, whether paused or not.
- [x] Withdrawing a proposal made under the other `DRY_RUN` is audited once, as an expiry.

**Verification:** `uv run pytest tests/test_worker.py tests/test_registry.py tests/test_scheduler.py`, and the same files on Neon with `-m integration`; `.\tasks.ps1 check`.

**Dependencies:** 17.14 · **Files:** `app/channel/worker.py`, `app/policy/registry.py`, `app/jobs/scheduler.py`, `app/policy/audit.py`, tests · **Scope:** M

### Task 17.16: The switches and the command line

**Description:**
- Resume moves each held decision's due time on by the length of the pause, and does not make it due afresh.
- The switch tells you what it switched, and fails loudly:
  - a missing `control` row raises;
  - Pause and Resume rows in the audit log say where they came from;
  - every connection the switches and Withdraw open has a timeout;
  - the command line names the database host, prints UTC, and gains `.\tasks.ps1 status`.
- The budget state gains `unpriced`, a model in use with no price. Migration 012 adds it, together with an index on `audit_log (decision_id)`.

**Acceptance criteria:**
- [x] A decision due fifty minutes before a pause is still counted as stuck after Resume. A decision that fell due during the pause starts its clock at Resume.
- [x] With the `control` row missing, `is_paused` and `switch` raise. The audit's `paused` and `resumed` rows carry "from the web app" or "from the command line".
- [x] With a model in use that has no price, the watch records `unpriced`, and the header says that new work has stopped.

**Verification:** `uv run pytest tests/test_control.py tests/test_watch.py tests/test_budget.py tests/test_scheduler.py`, on Neon too; `.\tasks.ps1 check`.

**Dependencies:** 17.15 · **Files:** `app/policy/control.py`, `app/jobs/control.py`, `app/store/db.py`, `app/policy/budget.py`, `migrations/012_review_fixes.sql`, `tasks.ps1`, tests · **Scope:** M

### Task 17.17: The seams

**Description:**
- **Expiry on a re-shown proposal:**
  - the resync expiry claims the row at the thread's revision;
  - a re-park's settle checks the mode, as a resync's does.
- **The command line's reconcile:** it records a missing row without binding or announcing it, and leaves both to the scheduler.
- **Guests:**
  - the one-hour guest hold runs from the later of `decided_at` and the last Resume;
  - a return to the owner keeps the guests that the check marked outside.
- **A found event:** its action is closed before the decision settles.
- **An end time:** `_has_event` requires one.

**Acceptance criteria:**
- [x] A resync or re-park whose payload was made under the other `DRY_RUN` expires: the proposal is not left failed, and is never announced.
- [x] After a two-hour pause, one Gmail error holds a Confirm for ten minutes and does not mark its guests outside.
- [x] A found event whose settle fails is left open as an error, not alerted as unconfirmed.
- [x] `approve --reconcile` writes no hash and announces nothing. (After the review it records no missing row at all: the scheduler's next pass records, binds and announces it.)

**Verification:** `uv run pytest tests/test_worker.py tests/test_reconcile.py tests/test_graph.py`, on Neon too; `.\tasks.ps1 check`.

**Dependencies:** 17.15 · **Files:** `app/channel/worker.py`, `app/channel/reconcile.py`, `app/jobs/approve.py`, `app/graph/build.py`, tests · **Scope:** M

### Task 17.18: The web app's switches and cards

**Description:**
- **Resume:** it asks a second time before it releases held decisions.
- **The header:**
  - a failed read leaves Pause available and the timeline standing;
  - the header is read again on each navigation.
- **Held cards:** they slow the timeline's re-read to every thirty seconds.
- **Withdraw:** a "settled" answer reads "That decision is no longer queued".
- **A held card's note:** it covers a model with no price.
- **The Activity page:**
  - a row with no proposal shows no "(cleared)";
  - times are given in the owner's zone.

**Acceptance criteria:**
- [x] One tap on Resume changes nothing. The second tap must be on "Resume now", which ignores taps for 600 ms after it appears: the review found that a narrow screen could put it where Resume was. (The browser check waits for the owner's end tests.)
- [x] With the switches unreadable, the timeline renders, and Pause is still shown.
- [x] With every open decision held, the page re-reads every thirty seconds, not three.

**Verification:** `cd dashboard; npm test; npm run typecheck; npm run build`. The browser checks wait for the owner's end tests.

**Dependencies:** 17.16 · **Files:** `dashboard/src/components/SwitchButton.tsx`, `dashboard/src/components/ControlBar.tsx`, `dashboard/src/app/page.tsx`, `dashboard/src/lib/timeline.ts`, `dashboard/src/lib/switches.ts`, `dashboard/src/lib/decisionForm.ts`, `dashboard/src/app/activity/page.tsx`, tests · **Scope:** M

### Checkpoint: M17 built

- [ ] Python and web checks green, locally and in CI.
- [x] Fresh-context review of the whole module (2026-10-02): its findings, and those of 17.12–17.13's review, are tasks 17.15–17.18.
- [x] A fresh-context review of 17.15–17.18: two reviews, all findings folded in (running notes).
- [ ] The exit criterion waits for the owner's end tests.

---

# M20 · Mail sync — tasks

Spec: [`docs/plans/M20-mail-sync.md`](../docs/plans/M20-mail-sync.md), revised after its third review round on 2026-10-01. Where a task and the spec differ, the spec wins.

### Task 20.1: Records

**Description:** Migration `011_mail_sync.sql` (D8): `gmail_messages` and its indexes, `gmail_cursors`, `gmail_fetch_queue`.

**Acceptance criteria:**
- [ ] It applies and can be re-run; `web_reader` reads none of it.

**Verification:** the integration tests on Neon.

**Dependencies:** None · **Files:** `migrations/011_mail_sync.sql`, `tests/test_web_reader.py` · **Scope:** XS

### Task 20.2: Gmail calls for sync

**Description:** The Gmail client gains:
- `history.list` pages (exclusive start);
- field-masked metadata fetches with the six headers (a new constant, not M15's);
- epoch-window id listing;
- 429 and 5xx retries bounded to 30 seconds in all for the pipeline's fetch;
- a pacer, shared by every Gmail call in the process, counting units by method;
- `CursorExpired` for a history `404`, and `MessageGone` for a fetch `404`.

**Acceptance criteria:**
- [ ] Every call is covered against a fake service, both `404`s included.
- [ ] Every metadata request carries the field mask and exactly the six headers; none asks for a body, snippet or subject.
- [ ] The pacer holds the sync to 2,000 units a minute.

**Verification:** `uv run pytest tests/test_gmail.py tests/test_mail_quota.py`.

**Dependencies:** None · **Files:** `app/google/gmail.py`, `app/mail/quota.py`, tests · **Scope:** M

### Task 20.3: Messages

**Description:** `app/mail/messages.py` (D1): classify (direction, `to_self`, category, the raw bulk signals), store idempotently, apply label additions and removals, and recompute direction and category.

**Acceptance criteria:**
- [ ] `SENT` gives `out`; mail from the owner to the owner is `to_self`; categories map; no category is `primary`; Promotions, Social, drafts, chats, spam and trash are not stored.
- [ ] Storing twice changes nothing; a label change applies without a fetch.
- [ ] A seeded subject, snippet and body never reach the table.

**Verification:** `uv run pytest tests/test_mail_messages.py`; Neon.

**Dependencies:** 20.1, 20.2 · **Files:** `app/mail/messages.py`, tests · **Scope:** S

### Task 20.4: Incremental sync, the switch-over, and catching up

**Description:** `app/mail/sync.py` (D3):
- the first run, from the old poller's stored id and time;
- the switch-over listing, once;
- incremental passes, with the cursor after each page and `caught_up_at` at the end;
- the fetch queue for per-message failures and label-change fetches;
- the catch-up on a history `404`, with its gap and the 7-day re-fetch;
- the advisory lock, and the 60-second bound;
- the scheduler's `mail_sync` job every 2 minutes.

**Acceptance criteria:**
- [ ] Each sync case in the spec's tests passes against a fake service.
- [ ] A crash mid-pass costs at most one page; a catch-up resumes after a restart.

**Verification:** `uv run pytest tests/test_mail_sync.py tests/test_scheduler.py`; Neon.

**Dependencies:** 20.3 · **Files:** `app/mail/sync.py`, `app/jobs/scheduler.py`, tests · **Scope:** L (split if it grows: passes and the cursor, then the switch-over and catch-up)

### Task 20.5: The backfill and the fetch queue

**Description:** Both run in the background, within the sync's quota share, after the incremental pass (D3). The backfill goes one epoch-second day at a time, newest first, from `feed_from` back to 90 days, resuming from `backfill_until`. The queue drops messages older than 90 days; five strikes mark a message `unreadable`.

**Acceptance criteria:**
- [ ] The backfill resumes after a restart, stops at 90 days, and never stores mail newer than `feed_from`.
- [ ] Outages charge no strike.

**Verification:** `uv run pytest tests/test_mail_sync.py`.

**Dependencies:** 20.4 · **Files:** `app/mail/sync.py`, tests · **Scope:** S

### Checkpoint: the mailbox is seen

- [ ] Checks green; Neon.

### Task 20.6: The pipeline's feed, the switch-over, and stranded claims

**Description:**
- Poll's candidates come from the feed (D4): the rule, `feed_from` less an hour, and the 7-day age limit with its `SKIPPED` record.
- `list_unread` is used only until the first sync run.
- The pipeline's fetch raises `MessageGone`, which poll records as `SKIPPED`.
- A classification skip records "not a meeting".
- At boot, stranded claims are recorded, released or failed.
- The new reasons join the purge's fixed reasons.

**Acceptance criteria:**
- [ ] Every feed case in the spec's tests passes, Google Groups mail included.
- [ ] Nothing older than `feed_from` less an hour, and nothing older than 7 days, reaches the pipeline.

**Verification:** `uv run pytest tests/test_poll.py tests/test_mail_feed.py tests/test_graph.py tests/test_ledger.py tests/test_purge.py`; Neon.

**Dependencies:** 20.5 · **Files:** `app/jobs/poll.py`, `app/mail/feed.py`, `app/graph/nodes.py` (`fetch` and `skip` only), `app/graph/build.py`, `app/store/ledger.py`, `app/api.py`, `app/jobs/purge.py`, tests · **Scope:** M

### Task 20.7: Recall and liveness

**Description:** The daily `mail_recall` job (D5): sync recall with repair, feed recall leaving out paused and capped hours, the too-old check, and category agreement both ways. `/health` returns 503 when no pass has reached the end of history for three intervals, and shows its bearer-only fields (D6).

**Acceptance criteria:**
- [ ] Each recall case in the spec's tests passes.
- [ ] A lagging or dead sync gives 503; a fresh boot does not.

**Verification:** `uv run pytest tests/test_mail_recall.py tests/test_api.py tests/test_liveness.py`.

**Dependencies:** 20.4 · **Files:** `app/mail/recall.py`, `app/jobs/scheduler.py`, `app/api.py`, `app/obs/liveness.py`, tests · **Scope:** M

### Task 20.8: Retention, the CLI and the runbook

**Description:** The purge (D7); `python -m app.mail.sync --once|--status|--show|--catch-up|--check-feed` (D8); `docs/DEPLOY.md` and the README.

**Acceptance criteria:**
- [ ] Day 179 kept, day 181 deleted; gone rows a week after `gone_at`; ledger rows untouched.
- [ ] Each CLI command works against a fake service and Neon.
- [ ] A read-through against the spec finds no step outside the docs.

**Verification:** `uv run pytest tests/test_purge.py tests/test_mail_cli.py`; Neon; the read-through.

**Dependencies:** 20.6, 20.7 · **Files:** `app/jobs/purge.py`, `app/mail/sync.py`, `docs/DEPLOY.md`, `README.md`, tests · **Scope:** M

### Checkpoint: M20 built

- [ ] Python checks green, locally and in CI.
- [ ] Fresh-context review of the module.
- [ ] The exit criterion waits for the owner's end tests.

---

# M18 · Untrusted input — tasks

Spec: [`docs/plans/M18-untrusted-input.md`](../docs/plans/M18-untrusted-input.md), approved by the owner on 2026-10-02. Where a task and the spec differ, the spec wins. The tasks begin after M17's review fixes.

**Injection payloads live in `data/injection/`, and are cited by case id.** Tests, reviews and notes never quote them: quoting them in a session made auto mode block its shell on 2026-10-02.

## Phase 1 · Mail enters clean

### Task 18.1: The scrubber

**Description:** `app/policy/scrub.py` (D2). It provides:
- D1's normalisation;
- credential mail, recognised by the strong phrases;
- codes near a cue word, with the exclusions for times, years, dates and phone numbers;
- links rewritten to `[link: host]`, except allowlisted meeting hosts, whose path is kept and whose query keeps only the keys a meeting needs.

Known wrappers are unwrapped first. Links are handled before codes, and counts are logged by kind. The first fixtures, real-format credential mail and meeting mail that must survive, start `data/injection/`.

**Acceptance criteria:**
- [x] Every strong phrase flags a message, in the subject or the body. No meeting-mail fixture is flagged.
- [x] Codes are removed whatever their separators: spaces, dashes, non-breaking or zero-width. Times, years, dates, phone numbers, rooms and order numbers survive.
- [x] An allowlisted link keeps its host and path and loses any passcode or token. A wrapped link is unwrapped, and an obfuscated one is found. Scrubbing twice changes nothing.

**Verification:** `uv run pytest tests/test_scrub.py`; `.\tasks.ps1 check`.

**Dependencies:** 17.18 · **Files:** `app/policy/scrub.py`, `data/injection/`, `tests/test_scrub.py` · **Scope:** M

### Task 18.2: The body the owner sees

**Description:** `extract_body` (D1):
- attachments, and every part inside a `message/rfc822` part, are skipped;
- HTML is preferred over `text/plain`;
- hidden content is removed before the tags are: comments, `<style>`, `<script>`, `<head>`, elements hidden by an inline style, and the `hidden` attribute;
- the text is normalised.

`get_message` scrubs before a message leaves the client. Credential mail leaves it flagged, with its body replaced by a fixed notice.

**Acceptance criteria:**
- [x] Each hidden-text fixture loses its hidden part. White-on-white text stays, recorded as the known gap.
- [x] A message whose `text/plain` part differs yields the HTML's text. An attached message's text never appears.
- [x] A credential message leaves the client with the flag and the fixed notice, and with no code and no link.

**Verification:** `uv run pytest tests/test_gmail.py tests/test_scrub.py`.

**Dependencies:** 18.1 · **Files:** `app/google/gmail.py`, `app/contracts.py`, `data/injection/`, `tests/test_gmail.py` · **Scope:** M

### Task 18.3: Credential mail set aside

**Description:** The graph's first node records a flagged message as SKIPPED ("carried a sign-in code"), before classify runs. "carried a sign-in code" joins the purge's fixed reasons (D10).

**Acceptance criteria:**
- [ ] A credential message ends SKIPPED with the fixed reason, and no model is called: the gate records no spend.
- [ ] Its checkpoint and its ledger row hold no code, no link and no body text.

**Verification:** `uv run pytest tests/test_graph.py tests/test_poll.py tests/test_purge.py`, and the same on Neon.

**Dependencies:** 18.2 · **Files:** `app/graph/nodes.py`, `app/graph/build.py`, `app/jobs/purge.py`, tests · **Scope:** S

### Checkpoint: mail enters clean

- [ ] Checks green; Neon.

## Phase 2 · Readers without tools, and the owner's own channel

### Task 18.4: The reviewer removed

**Description:** Decision 4:
- the review node and its routes leave the graph;
- `app/agents/reviewer.py`, `app/eval/reviewed.py` and the `gemini_reviewed` extractor go;
- `REVIEWER_ENABLED`, the reviewer's model and its prompt leave the settings and the pipeline version's inputs.

Cards keep rendering the `review_issues` of payloads parked before M18.

**Acceptance criteria:**
- [ ] The graph has no review node, and `Deps` holds no reviewer.
- [ ] A payload parked before M18, `review_issues` included, still renders on the web card and in Telegram.

**Verification:** `uv run pytest tests/test_graph.py tests/test_versioning.py tests/test_config.py tests/test_telegram.py`; `.\tasks.ps1 check`; the graph tests on Neon.

**Dependencies:** 18.3 · **Files:** `app/graph/build.py`, `app/graph/nodes.py`, `app/graph/runner.py`, `app/graph/versioning.py`, `app/config.py`, the removed modules and `tests/test_reviewer.py` · **Scope:** M (mostly deletions)

### Task 18.5: Search removed from extraction

**Description:** Decision 3:
- `ExtractionPipeline` loses its searcher and `SEARCH_SUFFIX`;
- `graph_session` builds no searcher;
- `SEARCH_CONTEXT_ENABLED` goes;
- `app/rag/demo.py` stops running an extractor that searches.

The search code stays, for M19. A structural test checks that `ExtractionPipeline` takes no searcher, and that no `tools` argument reaches `structured_call` from `app/extraction/` (D4).

**Acceptance criteria:**
- [ ] The structural test fails on the code before the change, and passes after it.
- [ ] Ingestion and the retrieval eval still run.

**Verification:** `uv run pytest tests/test_pipeline.py tests/test_no_tools.py tests/test_rag_search.py tests/test_config.py`; `.\tasks.ps1 check`.

**Dependencies:** 18.4 · **Files:** `app/extraction/pipeline.py`, `app/extraction/prompts.py`, `app/graph/runner.py`, `app/config.py`, `app/rag/demo.py`, `tests/test_no_tools.py`, `tests/test_search_context_tool.py` · **Scope:** M

### Task 18.6: The owner's channel

**Description:** D3:
- the correction goes in the system instruction of the re-extraction that applies it;
- markers are made fresh for each call, and marker-shaped text inside the email is defused;
- From, To, Subject and the body all sit inside the markers;
- the system instruction explains what the text between the markers is;
- the cut happens before the markers are added;
- the Gateway's evaluation path is built the same way;
- the scrubber runs again at assembly;
- `PIPELINE_REVISION` goes up.

**Acceptance criteria:**
- [ ] The correction appears only in the system instruction. All sender text sits between the call's markers, and each forged-structure fixture stays inside them.
- [ ] A body cut to fit keeps its closing marker.
- [ ] A checkpoint made before M18 is scrubbed when its prompt is assembled.

**Verification:** `uv run pytest tests/test_pipeline.py tests/test_evaluation.py tests/test_models.py`; `.\tasks.ps1 check`.

**Dependencies:** 18.5 · **Files:** `app/extraction/prompts.py`, `app/extraction/pipeline.py`, `app/extraction/evaluation.py`, `app/graph/versioning.py`, tests · **Scope:** M

### Task 18.7: Titles, locations and Telegram

**Description:** D6. The title and the location are scrubbed before park and inside `event_args`, so the card and the hash see the same text. Telegram disables link previews on every send and every edit. The card's first line is a fixed label.

**Acceptance criteria:**
- [ ] A title or location carrying a link, a code or a line shaped like an instruction is scrubbed on the card, in Telegram and in the event's arguments. An allowlisted meeting link stays.
- [ ] Every Telegram call that sends text disables previews.
- [ ] A pending proposal hashed before the change comes back to the owner ("the proposal changed") rather than failing.

**Verification:** `uv run pytest tests/test_hashing.py tests/test_park.py tests/test_telegram.py tests/test_worker.py`; Neon for the worker.

**Dependencies:** 18.6 · **Files:** `app/policy/hashing.py`, `app/channel/park.py`, `app/telegram/cards.py`, `app/telegram/client.py`, tests · **Scope:** M

### Checkpoint: readers without tools

- [ ] Checks green; Neon.

## Phase 3 · What the owner sees and what is stored

### Task 18.8: Where a guest came from, on Fly and in Telegram

**Description:** D5. At park, each guest gets one of these sources:
- in the thread;
- an allowed contact;
- named in the email;
- named in a quoted or forwarded section, found with `app/rag/clean.py`'s detectors;
- not found in the email.

The payload stores them, and notes when the email has a quoted or forwarded section. Telegram shows each source, and marks the last two as warnings.

**Acceptance criteria:**
- [ ] Each source is computed from a fixture, a Calendar invitation's "Who:" list included.
- [ ] An address the model invented is "not found in the email", and blocks the Confirm until allowed (M17).

**Verification:** `uv run pytest tests/test_participants.py tests/test_graph.py tests/test_telegram.py`; the graph tests on Neon.

**Dependencies:** 18.7 · **Files:** `app/policy/participants.py`, `app/graph/nodes.py`, `app/channel/park.py`, `app/telegram/cards.py`, tests · **Scope:** M

### Task 18.9: Where a guest came from, on the web card

**Description:** The web card lists each guest with its source. The two warnings sit beside Allow, and the card notes when the email has a quoted or forwarded section.

**Acceptance criteria:**
- [ ] Each source renders. A payload from before M18 renders without sources.
- [ ] The layout key counts the sources, so a warning cannot move a button under a tap.

**Verification:** `cd dashboard; npm test; npm run typecheck; npm run build`. The browser check waits for the owner's end tests.

**Dependencies:** 18.8 · **Files:** `dashboard/src/components/ProposalCard.tsx`, `dashboard/src/lib/timeline.ts`, tests · **Scope:** S

### Task 18.10: Stored text

**Description:** D7:
- `skip` records a fixed phrase ("a meeting with no start time"), never the model's reasoning;
- `runs.error`, `spans.error` and `ingest_runs.error` keep the exception's type and a scrubbed message;
- `LlmError` stops embedding the model's output;
- the purge cuts these errors to the type after a week.

"a meeting with no start time" joins the purge's fixed reasons (D10).

**Acceptance criteria:**
- [ ] A seeded code, link and sentence of email text never reaches `runs`, `spans`, `ingest_runs` or the ledger through an error or a skip.
- [ ] A week later, the purge has left only the type.

**Verification:** `uv run pytest tests/test_obs.py tests/test_retries.py tests/test_purge.py tests/test_rag_ingest.py`, and the same on Neon.

**Dependencies:** 18.3 · **Files:** `app/graph/nodes.py`, `app/obs/trace.py`, `app/graph/runner.py`, `app/rag/ingest.py`, `app/extraction/llm.py`, `app/jobs/purge.py`, tests · **Scope:** M

### Checkpoint: what the owner sees

- [ ] Checks green, Python and web; Neon.

## Phase 4 · The injection suite and the evals

### Task 18.11: The fixtures, and the deterministic suite

**Description:** D8's fixtures, completed as recorded Gmail payloads, each with what must hold:
- forged structure;
- guests;
- titles and locations;
- meeting mail that must survive;
- each attack paraphrased three ways and hidden three ways.

The tests run every fixture through the real `get_message` preparation and the prompt assembly.

**Acceptance criteria:**
- [ ] Every fixture's "must hold" holds, through the preparation and in the assembled prompt.
- [ ] A failure names its case id, and never prints the payload.

**Verification:** `uv run pytest tests/test_injection.py`.

**Dependencies:** 18.10 · **Files:** `data/injection/`, `tests/test_injection.py` · **Scope:** M

### Task 18.12: The compliant fake model

**Description:** A fake model that does whatever each injection asks, driven through the graph to park, `decide()` and the registry.

**Acceptance criteria:**
- [ ] Nothing is booked without a Confirm bound to the exact arguments.
- [ ] Every injected guest is marked, and blocks the Confirm.
- [ ] A forged correction changes nothing.

**Verification:** `uv run pytest tests/test_injection_flow.py`, on Neon (`-m integration`).

**Dependencies:** 18.11 · **Files:** `tests/test_injection_flow.py`, the fakes it needs · **Scope:** M

### Task 18.13: The model run, and the CI gate

**Description:** `app/eval/injection.py` and `.\tasks.ps1 injection-eval`:
- every fixture goes through the real pipeline, from the same preparation, to park;
- Gmail and Calendar are fakes, and production's settings are used whatever the environment says;
- each case runs five samples, and the results go to `results/injection-<stamp>.json`.

`results/injection-baseline.json` records a hash of the code that shapes what the model sees. A CI test fails when that hash no longer matches, or when the baseline has a failure.

**Acceptance criteria:**
- [ ] Changing a prompt, the scrubber, the body preparation or a fixture fails the CI test until the run is redone.
- [ ] The committed baseline has no failures.

The run spends on the development key: a few hundred calls, well inside the cap.

**Verification:** `uv run pytest tests/test_injection_baseline.py`; the run itself, recorded in the spec's running notes.

**Dependencies:** 18.12 · **Files:** `app/eval/injection.py`, `tasks.ps1`, `results/injection-baseline.json`, `tests/test_injection_baseline.py` · **Scope:** M

### Task 18.14: The golden set and the retrieval eval, again

**Description:** D9:
- the golden set runs through the same preparation production uses, with new fixtures that hold meeting links, passcodes and dial-in numbers;
- chunks in the development database are deleted and re-ingested;
- the retrieval eval is re-run;
- the new runs become the baselines;
- threads parked in development before M18 are drained.

**Acceptance criteria:**
- [ ] Exact match falls by at most one fixture, and `is_meeting` F1 not at all. Every new fixture's time and link survive.
- [ ] Recall at 5 stays within two points of the last run. The "order reference" queries are judged by hand.

**Verification:** `.\tasks.ps1 eval --extractor gemini`; `.\tasks.ps1 retrieval-eval --by-kind`; results committed.

**Dependencies:** 18.13 · **Files:** `app/eval/dataset.py`, `app/eval/run.py`, the golden fixtures, `results/` · **Scope:** M

### Task 18.15: Runbook and README

**Description:** `docs/DEPLOY.md`:
- draining parked threads before the deploy;
- re-indexing;
- what removing search and the reviewer means;
- re-running the injection eval after any change to a prompt or to the scrubber.

README: the safety section. The spec: its running notes.

**Acceptance criteria:**
- [ ] A read-through against the spec finds no step outside the docs.

**Verification:** the read-through, recorded in the running notes.

**Dependencies:** 18.14 · **Files:** `docs/DEPLOY.md`, `README.md`, `docs/plans/M18-untrusted-input.md` · **Scope:** S

### Checkpoint: M18 built

- [ ] Python and web checks green, locally and in CI, with the committed injection run current.
- [ ] Fresh-context review of the module, citing injection cases by id.
- [ ] Exit criterion 3 on the deployed stack, and 4 at the owner's end tests.
