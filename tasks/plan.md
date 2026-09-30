# Implementation Plan: M15 · Go live and measure

## Overview

Harden the existing v1 pipeline so that it can run unattended in production,
deploy it in observe mode on the paid Gemini tier, and measure volume, loose
ends and cost against thresholds committed before measuring. The spec is
[`docs/plans/M15-go-live.md`](../docs/plans/M15-go-live.md), approved after
three adversarial review cycles and committed in `52ddc5e` on branch `v2-plan`.
The task list is [`tasks/todo.md`](todo.md).

## Architecture decisions

- **Fix v1 bugs before adding anything.** The *Edit* → *Cancel* routing bug, the image that ignores `uv.lock`, the silent retries and the health check that cannot fail all come first. Evidence gathered on a system with those bugs would be worthless.
- **Evidence lives in the database, not in files or flags.** `job_runs` records every scheduler tick and every token refresh. Token files are rewritten from secrets on each boot, so nothing learned at runtime may live there.
- **`/health` reads in-memory state.** Each tick already exercises the database; a public endpoint must not add load or leak exception text.
- **A standby Google token carries the eight-day window.** The window then completes whether or not "In production" removes the seven-day expiry. The primary token's fate is the evidence either way.
- **Measurement splits by where the data is.**
  - `measure mail` runs locally against Gmail metadata; its `--label` step is for the owner only.
  - `measure cost` runs on the instance against spans, and waits for the price-table fix.
- **No edits to files owned by the parallel price-table session**, which are `app/obs/pricing.py` and the README cost section.

## Phases

1. **Decide** (task 1): host, database, regions, uptime monitor — from live pricing pages.
2. **Harden** (tasks 2–13): v1 fixes, deploy reproducibility, liveness, retries, tokens, retention, runbook.
3. **Deploy** (task 14): day 0, observe mode.
4. **Measure** (tasks 15–18): measurement code, built while the window runs, and the owner's labelling.
5. **Run the window** (tasks 19–20): planted proposal, standby token, mid-window redeploy.
6. **Close** (task 21): exit evidence, results, go/no-go, sweep.

Checkpoints follow tasks 4, 8, 13, 14, 18 and 21.

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Google refuses consent for an unverified production app | Medium | Fall back to Testing tokens. A standby minted on day 4 still covers eight days; the fallback is recorded |
| The price-table session is late | Medium | Only task 17 and exit items 3–4 wait. Everything else proceeds |
| The price-table session and task 9 both touch `trace.py` | Medium | Task 9 changes only the retry-count plumbing, and is rebased onto that session's merge if both land |
| No local Docker or Postgres on this machine (WSL and Hyper-V disabled) | Medium | Integration tests run in CI or on a hosted dev database (task 1). The image is built by CI or Fly's remote builder. `uv lock --check` runs locally |
| The owner is unavailable on day 0, day 4 or day 8+ | Medium | Owner steps are listed per day in `todo.md`. The window can start whenever day 0 happens |
| Loose ends come out no-go | High | Planned for: M21 is re-planned before M16 builds its ledger view |
| A provider's free plan cannot hold an always-on database within $10/month | Medium | Task 1 decides with live prices, and the budget threshold already counts list prices |

## Open questions

- Host, database, regions and uptime monitor: resolved in task 1.

---

# Implementation Plan: M16 · Web channel

## Overview

Give the owner an approval channel that works on their network: an owner-only
web app on Vercel, with web push to Android and iPhone. Behind it sits a
decision queue on Fly that every channel shares. The spec is
[`docs/plans/M16-web-channel.md`](../docs/plans/M16-web-channel.md). It was
approved on 2026-09-30 after three adversarial review rounds and committed in
`78c5ca0`. The tasks follow M15's in [`todo.md`](todo.md), numbered 16.1 to
16.25.

## Architecture decisions

- **Decisions are queued, and one worker applies them.** Every channel records a decision and returns; only the worker resumes a thread. The third review round forced this: a recovery path cannot race a resume when only one thing ever resumes.
- **The worker moves one step at a time from stored state.** A crash leaves an open decision, and the next tick reads the state again. The worker's tests prove this by injecting a failure after each step. They are the only review the queue has had, so they are not optional.
- **The riskiest unknown goes first.** Sign-in inside an installed iPhone web app may not work. A thin sign-in slice goes to Vercel and onto the owner's phones in the first days, so the pairing fallback is decided early rather than at the end.
- **The web app has no write path of its own.** It reads through `web_reader` and writes only through the Fly API, from server actions that call `auth()`.
- **No new test dependency for the web app.** Node 24 runs TypeScript tests with `node --test`, so the allow-list, the gate and the service worker's caching rule live in small modules without framework imports.

## Phases

1. **Prove the risky parts** (16.1–16.7): the iPhone sign-in slice, then the decision queue from payload to worker.
2. **Put every path on the queue** (16.8–16.11): reconciliation, the scheduler job and health, the CLI, Telegram.
3. **API, push and alerts** (16.12–16.15).
4. **The web app** (16.16–16.20): the read role, the timeline, deciding from a card, push, CI.
5. **Retention, runbook, deploy and exit** (16.21–16.25).

The web slice (16.1–16.2) and the queue (16.3–16.7) do not depend on each
other and can proceed in either order. Checkpoints follow 16.2, 16.7, 16.11,
16.15, 16.20 and 16.25.

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Google sign-in fails inside the installed iPhone app | Medium | Tested in 16.2, in the first days. Pairing (16.24) is built only if it fails |
| The queue has a bug no review saw | High | 16.7 injects a failure after every step and requires convergence with exactly one application |
| M16 deploys while M15's eight-day window runs | Medium | 16.23 waits for M15 to close unless the owner decides otherwise: a new image mid-window would muddy the window's evidence |
| Next 16.3 expects `proxy.ts` rather than `middleware.ts`, or runs it somewhere D5 does not assume | Low | 16.1 checks the current docs first and corrects D5 if needed |
| Vercel Hobby's function limits | Low | Decisions return at once (202), so no request waits on the graph |
| Supabase's production database exists only from M15's day 0 | Low | Every build task uses the Neon test database; only 16.23 needs day 0 |

## Open questions

- None. The pairing fallback is decided by the result of 16.2.
