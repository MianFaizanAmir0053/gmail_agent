# M16 · Web channel

**Est.** 8 days · **Depends on** M15 · **Blocks** M19 (chat), M21 (ledger view), M24 (decision record)

Plan and decisions: [`ASSISTANT-PLAN.md`](../../ASSISTANT-PLAN.md).

## Goal

The owner sees every proposal on their phone, is told by push when one needs
them, and decides it there. Every decision is recorded, whatever the path,
with enough detail for M24 to compute autonomy per action type. None of it
depends on Telegram.

## Why this comes next

- **There is no working approval channel.** Telegram is blocked on the owner's network (M06 notes).
- **M24 needs every decision from the first one.** A decision nobody recorded is evidence lost for good.
- **It is the surface later modules build on.** M19's chat and M21's loose-ends ledger both land here.

## Decisions (30 Sep 2026)

- **Hosting.** The Next.js app runs on **Vercel Hobby**, with functions pinned to Singapore. The Python API stays on Fly.
- **Push.** On **Android** (Chrome) and **iPhone** (the installed web app, iOS 16.4+).
- **Chat in M16 is a timeline** with buttons and typed corrections. Free-form asks arrive with M19.

## Scope

**In:**
- one decision queue shared by every channel, applied by a single worker;
- `proposals`, `decisions`, `alerts_sent` and `pairing_codes` records;
- an authenticated API on Fly;
- the Next.js app with owner-only Google sign-in, the timeline and cards;
- a PWA with web push;
- channel adapters;
- reconciliation;
- retention.

**Out:**
- free-form chat and the planner (M19);
- argument-hash binding, a deterministic event id that makes `act` safe to re-drive, and turning `DRY_RUN` off (M17);
- the ledger view (M21);
- a custom domain.

---

## Design

### D1. Decisions are queued; one worker applies them

Every channel records a decision and returns. A single worker applies
decisions to the graph. Nothing else resumes a thread, so no recovery path can
race a live resume.

**Recording a decision.** `app/channel/decide.py`:

```python
def decide(
    conn: psycopg.Connection,
    message_id: str,
    *,
    action: Literal["confirm", "edit", "cancel", "sweep"],
    revision: int,
    correction: str = "",
    via: Literal["web", "cli", "telegram", "sweep"],
) -> DecisionResult: ...
```

1. **Validate before anything moves.**
   - An `edit` needs a non-empty correction. An empty one would route to `reject` and be logged as "declined by user" (`app/graph/build.py`, `_decision`).
   - An `edit` is refused at revision 3, where no edits are left. The graph would reject a third edit silently (`MAX_REVISIONS = 2`), so the card hides **Edit** there.
   - A message with no `proposals` row is refused as not found. D3 creates missing rows, and `.\tasks.ps1 approve --reconcile` runs D3 at once.
2. **Enqueue atomically.** On the caller's own connection, in one transaction:

   ```sql
   UPDATE proposals SET status = 'deciding', updated_at = now()
    WHERE message_id = %s AND status = 'pending' AND revision = %s
   RETURNING ...
   ```

   The same transaction inserts the `decisions` row with `outcome = NULL`. If no row comes back, the card was stale or another tap won, and the request is refused. A partial unique index allows one open decision per message.
3. **Wake the worker** if it runs in this process, as it does for the web API. Then return. `decide()` never touches the graph.

**Applying decisions.** `app/channel/worker.py` is the scheduler's
`decisions` job: every 15 seconds, `max_instances=1`, on the single Fly machine
(`--ha=false`). For each open decision whose retry time has come, it takes a
10-minute lease with a conditional `UPDATE`, reads the stored state, and moves
it one step:

| Stored state | Step |
|---|---|
| Parked at the decision's revision | Resume with the decision, then read the state again |
| Parked at the next revision, after an edit | Settle: the proposal is `pending` at that revision; outcome `reparked` |
| Ledger final (created, skipped, rejected) | Settle: the proposal is `decided`; the outcome is that status |
| Mid-graph (`next` set, no interrupt), without `act` in `next` | Re-drive with `invoke(None)`, then read the state again |
| Mid-graph with `act` in `next` | Settle as failed, reason `act interrupted`. Never re-driven, because `act` is not idempotent (`app/graph/build.py`). M17's deterministic event id lifts this |
| Anything else | Settle as failed |

- **Failures retry.** A resume or re-drive that raises costs one attempt. The next attempt waits 1 minute, then 10. After the third failure the worker settles: a thread still parked at the decision's revision returns to `pending` with outcome `no_effect`; anything else fails.
- **A settle is one transaction, and every write in it is conditional:** the proposal `WHERE status = 'deciding'`, the outcome `WHERE outcome IS NULL`, and a failed ledger `WHERE status = 'awaiting_approval'`. The lease stops a second worker; the conditions make a repeated settle harmless.
- **A crash anywhere converges.** The decision stays open, its lease expires, and the next tick reads the stored state again. A decision already applied is recognised from that state and never applied twice.
- **A re-park** settles through the park step (D2), inside the same transaction, so it writes its rows and announces itself as a poll's park does.
- **`ledger.mark` stays unconditional.** Only the worker resumes, so no second writer can reach a thread mid-decision.
- **Stuck is visible.** `/health` reports the oldest open decision's age and returns 503 past one hour.

The web API, `approve.py`, the Telegram handler and the sweep all call
`decide()`. `approve.py` then waits for the outcome and prints it, and
`--sweep-all` waits for the queue to drain.

### D2. Records (migration `007_proposals.sql`)

**The interrupt payload is the single source for what a proposal is.** It
gains:
- `review_issues`;
- `action_type`;
- `pipeline_version`.

All are computed in `await_approval` at every park. `pipeline_version` is
computed in `graph_session` from settings and carried in `Deps`.

**The revision comes from the thread's state, not the payload:** `revisions +
1`, the graph's own counter. A proposal parked before M16 therefore gets the
right value too.

**The park step.** Poll calls it after a run parks, the worker after a
re-park, and D3 for a missing row. In one transaction on the session's
connection, it writes the `proposals` row and marks the ledger
`awaiting_approval`, conditional on the non-final status it read. Then it
announces the proposal through every channel (D7).

**`proposals`**: one row per parked thread.

| Column | Meaning |
|---|---|
| `message_id` | Primary key; the ledger row it belongs to |
| `revision` | `revisions + 1` from the thread's state at the latest park |
| `status` | `pending`, `deciding`, `decided` or `failed` |
| `final_status` | The ledger status once decided |
| `action_type` | `calendar_hold` (no guests, tier T1) or `calendar_invite` (guests, T2). Recomputed at every park, so an edit that adds a guest turns a hold into an invite. **Never purged** |
| `pipeline_version` | A hash of everything that shapes a proposal: extraction and classify models, `reviewer_enabled`, `reviewer_model`, `search_context_enabled`, and the extraction, classify, search and reviewer prompts. **Never purged**; M24 resets its counts when it changes |
| `payload` | JSONB: title, start and end (UTC), zone, attendees, location, conflicts, reviewer issues. Purged (D8) |
| `dry_run` | As recorded at parking |
| `parked_at`, `updated_at` | Timestamps |

**`decisions`**: the queue, and the record. Rows are never deleted. Only the
worker's bookkeeping changes, and the outcome is written once.

| Column | Meaning |
|---|---|
| `id` | Primary key |
| `message_id`, `revision` | Which proposal, and which version the owner saw |
| `action` | confirm, edit, cancel or sweep |
| `via` | web, cli, telegram or sweep |
| `action_type`, `pipeline_version` | Copied from the proposal; never purged |
| `decided_at`, `latency_seconds` | When, and how long after that revision parked |
| `outcome` | `NULL` while open; then `reparked`, a ledger status, `no_effect` or `failed` |
| `attempts`, `next_attempt_at`, `lease_until`, `settled_at` | The worker's bookkeeping |
| `correction` | The owner's text, for edits. Purged (D8) |

A clean approval, for M24, is a `confirm` on revision 1 whose outcome is
`created` (or `skipped` under dry run), counted per `action_type` and per
`pipeline_version`. `pre-m16` proposals (D3) are excluded.

**`alerts_sent`**: `(code, subject, sent_at)`, unique on `(code, subject)`.
It is written only after delivery (D6).

**`pairing_codes`**: the iPhone fallback (D4), used only if needed. It
stores the code's SHA-256, the expiry, the attempts, and the id of the session
that issued it.

### D3. Reconciliation

It runs at boot (after `fail_stranded`), hourly with the purge, and on demand
with `.\tasks.ps1 approve --reconcile`.

- **A live interrupt always has a row.** D3 reads the thread of every ledger row that is not final: `claimed`, `awaiting_approval`, or `FAILED` within the seven days its checkpoint is kept. A live interrupt with no `proposals` row gets the park step (D2), which also restores the ledger to `awaiting_approval` and announces the proposal. This covers:
  - proposals parked before M16;
  - a crash between the checkpoint and the park step. Poll marks the ledger only after `pending()` returns (`app/jobs/poll.py`), so boot's `fail_stranded` would otherwise leave a parked thread marked FAILED;
  - a run that parked and then raised in the tracer's bookkeeping, which poll dead-letters as FAILED.
- **Legacy parks.** A payload from before M16 has no action type or pipeline version. The action type comes from its attendees, and `pipeline_version` is `pre-m16`, which M24 excludes.
- **Closed rows.** A `pending` or `failed` proposal whose thread is gone and whose ledger is final becomes `decided`.
- **Open decisions are the worker's.** D3 never touches a `deciding` proposal.

### D4. The API the web app calls (on Fly)

| Route | Does |
|---|---|
| `POST /api/decisions` | `{message_id, revision, action, correction?}` → `decide(via="web")`. Answers at once: 202 queued, 409 stale, 404 no proposal, 422 invalid |
| `POST /api/push-subscriptions` | Store or refresh a browser's push subscription (upsert) |
| `DELETE /api/push-subscriptions` | Remove one |
| `POST /api/pairing/codes` | iPhone fallback only (see below) |
| `POST /api/pairing/redeem` | iPhone fallback only (see below) |

- **Plain `def` handlers.** Their database calls are synchronous, and FastAPI runs a `def` handler in its thread pool, so a slow query cannot stall `/health`.
- **Auth.** `Authorization: Bearer <WEB_API_SECRET>`, compared with `compare_digest`. An unset **or blank** secret means 503.
  - `WEB_API_SECRET` and `VAPID_PRIVATE_KEY` join `_blank_secret_is_unset` in `app/config.py`. Otherwise a blank value becomes `SecretStr("")`, which counts as configured, and `compare_digest("", "")` is true: the webhook bug this repo already fixed once.
- **Server-side only.** The secret lives only in the Next.js server environment, and the browser never calls Fly. Owner identity is enforced in the web app: every page and server action calls `auth()` and requires the owner's session. A blank `OWNER_EMAIL` admits nobody.
- **Pairing, used only if the day-1 iPhone test fails.**
  - A signed-in session asks `codes` for a 6-digit code, valid for 5 minutes and bound to that session.
  - `redeem` allows **5 attempts** per code; after that the code is dead. Success returns a single-use grant, which the installed app turns into its own session.
  - Sessions are Auth.js JWTs, so revoking a device means rotating `AUTH_SECRET`, which signs out every device. The runbook says so.

### D5. The web app (Vercel)

**Sign-in**
- Auth.js v5 with Google: `next-auth@5.0.0-beta.32`, pinned exactly (the `beta` tag on 2026-09-30). A plain `npm i next-auth` installs 4.24.15, which has no `auth()`.
- It admits only a verified email equal to `OWNER_EMAIL`, on every route **except** `/manifest.webmanifest`, `/sw.js`, `/icons/*` and `/api/auth/*`. Browsers fetch a manifest without cookies, and iOS needs it to install a real web app rather than a bookmark.
- The sign-in client lives in a **separate Google Cloud project**, published "In production", asking only for `openid email profile`. If it shared the Gmail project and that project fell back to Testing, Google would refuse other accounts before the allow-list ever saw them, and exit criterion 1 would prove nothing.
- **iPhone risk.** Home Screen apps keep their own storage and open Google sign-in in an in-app browser. Sign-in inside the installed app is tested on a real iPhone on day 1; pairing (D4) is the fallback.

**Timeline and card**
- The timeline (`/`) shows pending proposals first, then recent decisions. The analytics pages sit behind the same sign-in.
- The card shows:
  - the title;
  - the time in the owner's zone;
  - attendees, conflicts and reviewer issues;
  - a `dry run` badge;
  - the revision.
- Its buttons are **Confirm**, **Edit** (hidden at revision 3), and **Cancel**. If the card is stale, the owner is shown the latest version.
- While a decision is open, the card shows "Applying…" and the page re-reads every 3 seconds. A confirm in dry run settles in seconds; an edit takes as long as re-extraction. A failed decision shows its reason.

**Reads**
- Through a read-only role, `web_reader`, over Supabase's session pooler (IPv4; Vercel has no IPv6 egress).
- Migration `008_web_reader.sql` creates the role `NOLOGIN` inside a `DO` block, so it can be re-run, and grants `SELECT` on the tables the app shows.
- The owner sets `LOGIN PASSWORD` once in Supabase's SQL editor. The password is never in the repo.

**PWA**
- A manifest, icons and a service worker. A push shows a generic notification, and tapping it opens `/`.
- The service worker caches **static assets only**: signed-in pages and API responses always come from the network.
- The app **re-posts its push subscription every time it opens**, and handles `pushsubscriptionchange`. A subscription deleted after a 410 is replaced the next time the owner opens the app.
- On iPhone Safari the page explains "Add to Home Screen".

**Region.** `vercel.json` sets `"regions": ["sin1"]`. Hobby allows exactly one
function region (Vercel docs, checked 2026-09-30). Routing middleware still
runs at the edge in every region, but the sign-in gate only reads the session
cookie, so no proposal content is handled there.

### D6. Push and alerts

- **Sending.** `pywebpush` with VAPID, called with:
  - a **10-second timeout**, because its default is none and one hung endpoint would block the poll it is called from;
  - **`ttl` = 24 hours and `Urgency: high`**, because its default `ttl` of 0 drops a push to a sleeping or locked phone.

  The VAPID `sub` is the app's URL, not the owner's email: it goes to Apple and Google.
- **Payloads are generic**: "A proposal needs you" or "Google sign-in needs attention", with no title, sender or time.
- **What triggers a push:**
  - a proposal parking, through the park step (a poll, a re-park after an edit, or D3);
  - a Testing token within two days of expiry;
  - a token turning `expired`;
  - failover to the standby.
- **Token alerts go out once per state change.** The `alerts_sent` row is written only after at least one push service accepted the push (2xx). With no subscriptions, or a failed send, the alert is retried at the next check rather than silenced.
- **Fix to `check_token`.** It alerts from `token_state`, as `/health` already reads it. Today it alerts on the seven-day countdown for every token, so a production token would trigger a push every 12 hours from day five.
- **Subscriptions** live in `push_subscriptions`. A 404 or 410 from the push service deletes the subscription. `/health` shows the subscription count, refreshed hourly into the in-memory record, so zero subscriptions is visible.
- **Keys.** `.\tasks.ps1 vapid` generates the pair.

### D7. Channels

A `Channel` protocol with `announce_proposal(message_id)` and `alert(code)`:

- **Implementations:** `WebPushChannel`, and `TelegramChannel` (the existing code, optional).
  - Telegram's button data gains the revision, so `decide` can refuse a stale card on that path too.
  - The handler records the decision and answers "Queued". Outcomes show in the web app.
  - The Telegram webhook becomes a plain `def` handler, because its database calls are synchronous.
- **Callers:** `poll` and the scheduler go through every configured channel.
- **Isolation:** exceptions and time-outs are isolated per channel.

### D8. Retention (same clock as M15)

- **Content:** `payload` and `correction` are cleared **7 days after the ledger row reaches a final status or FAILED**, keyed on the ledger rather than on `proposals.status`.
  - This matches M15's rule for model-written text; the owner approved no other schedule.
  - FAILED is included because the worker (D1) produces it, and otherwise its content would be kept for ever.
- **Kept:** `action_type`, `pipeline_version`, `via`, `revision`, `outcome` and timing stay, because they are M24's evidence and quote no email.
- **Pairing codes** are deleted when they expire.

---

## Deliverables

- **Python:**
  - `app/channel/` (`decide`, the worker, the park step, the `Channel` protocol, web push, Telegram);
  - the scheduler's `decisions` job, and the open-decision age in `/health`;
  - migrations 007 and 008;
  - the API routes;
  - the payload additions and `Deps.pipeline_version`;
  - conditional status writes;
  - reconciliation, and `approve --reconcile`;
  - the `check_token` fix and delivery-confirmed alerts;
  - `poll` moved onto the park step; `approve.py` and the Telegram handler (revision in button data, sync webhook) moved onto `decide`;
  - the purge extension;
  - the blank-secret validator entries;
  - `.\tasks.ps1 vapid`.
- **Web:**
  - Auth.js v5, pinned, with the gate exemptions;
  - the timeline and card;
  - server actions that check `auth()`;
  - the PWA (static-only caching, re-subscribe on open);
  - iPhone install guidance;
  - `vercel.json` with `sin1`;
  - the pairing screens, only if the day-1 iPhone test fails.
- **Docs:**
  - a DEPLOY.md section for Vercel, the separate sign-in project, the `web_reader` password step, VAPID, and revocation by rotating `AUTH_SECRET`;
  - `.env.example` entries.

## Commands

```powershell
.\tasks.ps1 check
.\tasks.ps1 migrate
.\tasks.ps1 vapid
.\tasks.ps1 approve --reconcile
```

```bash
cd dashboard && npm run typecheck && npm run build
```

## Testing

- **`decide`:**
  - Each action enqueues exactly one decision and touches no graph.
  - A stale revision is refused, and nothing is enqueued.
  - **Two concurrent decisions on one revision: one wins, and the other is refused**, tested on real Postgres.
  - An empty correction, an edit at revision 3, and a message with no `proposals` row are refused.
  - Every path records its `via`, `action_type` and `pipeline_version`.
- **Worker:**
  - Each stored state in D1's table takes its step.
  - **A crash after any step converges on the next tick, and no decision is applied twice.** Tested by injecting a failure after each step.
  - `act` in `next` is never re-driven.
  - Failed attempts back off, and the third settles the decision.
  - A second worker skips a leased decision.
  - The outcome is written once, and a late failed settle never overwrites a final ledger status.
  - A re-park carries the new revision and is announced.
- **Reconciliation.**
  - A live interrupt with no row gets one, whether its ledger is `claimed`, `awaiting_approval` or FAILED.
  - A legacy park gets its revision from state and `pipeline_version` `pre-m16`.
  - A `deciding` proposal is never touched.
- **Health.** An open decision older than an hour gives 503.
- **Versioning.**
  - Toggling the reviewer changes `pipeline_version`.
  - An edit that adds a guest changes `action_type` on re-park.
- **API:**
  - A missing or wrong secret gives 401; an unset or **blank** secret gives 503.
  - A blank `OWNER_EMAIL` admits nobody.
  - The handlers are sync.
  - A pairing code dies after 5 attempts and after 5 minutes, if the fallback is built.
- **Push:**
  - Payloads carry no proposal content (seeded strings).
  - `webpush` is called with a timeout, `ttl` of 24 h and `Urgency: high`.
  - A 410 deletes the subscription.
  - A failing or hanging channel does not stop the others.
- **Alerts:**
  - A production token never triggers the countdown alert.
  - Each alert code is sent once per state change, including across a restart. It is recorded only after a 2xx; with no subscriptions, it is retried.
- **Integration (Neon).** 007 and 008 apply and can be re-run. `web_reader` can select and cannot insert.
- **Web:**
  - `typecheck` and `build` in CI;
  - the allow-list unit test;
  - the gate exempts exactly the listed paths;
  - the service worker caches only static assets.
- **Runtime, by the owner:**
  - day 1, sign-in inside the installed iPhone app;
  - sign-in and push on both phones, including push to a locked phone;
  - a planted proposal decided from each.

## Boundaries

- **Always:**
  - every write goes through the API;
  - every decision goes through `decide`, and only the worker resumes a thread;
  - status writes are conditional;
  - push payloads stay generic;
  - `DRY_RUN` stays `true`;
  - content is cleared on M15's clock.
- **Ask first:**
  - dependencies other than `pywebpush` and Auth.js v5, both approved with this spec;
  - creating the Vercel project and the sign-in Cloud project (owner);
  - schema beyond 007 and 008;
  - any retention schedule other than D8.
- **Never:**
  - the browser holding `WEB_API_SECRET` or calling Fly;
  - proposal content in a push;
  - write access for `web_reader`;
  - a committed database password;
  - turning `DRY_RUN` off.

## Exit criterion

1. **Sign-in.**
   - The owner signs in on Android and inside the installed iPhone app; pairing is acceptable if Google sign-in cannot work there.
   - A second Google account is refused by the allow-list. This is checked with the sign-in project in production, so the refusal is the allow-list's and not Google's.
2. **Push.** A planted proposal appears within one poll interval, and a push arrives on both phones, including while they are locked.
3. **Decisions.** Confirm, Edit and Cancel from each phone work in dry run. The ledger, `proposals` and `decisions` agree, and a stale card is refused.
4. **Read-only.** An `INSERT` as `web_reader` fails.
5. **Record.** Every decision, from any path, is recorded with via, revision, action type, pipeline version, outcome and latency. No decision stays open for more than an hour.

## Open questions

*None.* Checked on 2026-09-30:
- **Vercel region:** Hobby allows one function region, set in `vercel.json`.
- **Auth.js:** v5's current beta is `5.0.0-beta.32`.
- **`pywebpush`:** `webpush()` defaults to `timeout=None` and `ttl=0`, and takes `Urgency` through `headers` (source on GitHub).

## Review

Three rounds of adversarial review, each by a reviewer with fresh context.
Rounds 1 and 2 reviewed the whole spec; round 3 reviewed D1–D3 against
`app/graph/runner.py`, `app/graph/build.py` and `app/store/ledger.py`.

Round 3 found that recording a decision and applying it could not safely
share one request:
- A resume that failed after consuming the decision was dead-lettered. Nothing re-drove the thread, and `act` cannot be retried blindly.
- The one-hour timeout on `deciding` could settle a resume that was still running.

Both came from resuming inside the caller while a separate recovery path
watched the clock. Hence D1's queue: only the worker resumes, and a crash
leaves an open decision rather than a guess.

The same round also found:
- a thread that parked without its ledger mark, which `fail_stranded` turned into FAILED. D3 now works from checkpoints.
- revisions missing from payloads parked before M16. The revision now comes from the thread's state.

The queue itself had no fresh review. Its proof is the worker's tests, which
inject a failure after each step. The owner declined a cross-model review and
approved the spec on 2026-09-30.

## Running notes

*Not started.*
