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
