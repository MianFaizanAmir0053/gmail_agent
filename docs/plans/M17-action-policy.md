# M17 · Action policy

**Est.** 5 days · **Depends on** M15, and M16's decision queue · **Blocks** M18, M19, M22, M24, M27

Plan and decisions: [`ASSISTANT-PLAN.md`](../../ASSISTANT-PLAN.md), section "Security model".

## Goal

Every side effect the agent can cause goes through one registry that enforces
the rules in code:
- what tier the action is;
- whether the owner approved these exact arguments, under the `DRY_RUN` setting they saw;
- whether its recipients are people the owner already deals with;
- whether the month's model budget allows the work;
- whether the owner has paused everything.

A calendar write interrupted halfway can be finished without booking twice.
Every attempt leaves a record that quotes no email.

M17 is the first of the two modules that must land before `DRY_RUN` goes off.
M18 is the second: it stops email content from steering what the model writes
into an event. Turning `DRY_RUN` off stays the owner's step, in the
end-to-end tests.

## Why this comes next

The code as of `4c7df5b` (mapped on 2026-10-01):
- **An approval binds only a revision.** `decide()` claims `(message_id, revision)`. `act` then runs whatever the checkpoint's extraction holds (`app/graph/nodes.py`, `act` and `_to_tool_input`). Nothing ties the event that is created to the card the owner saw.
- **The setting a proposal was approved under is recorded, never checked.** `proposals.dry_run` is written at park; nothing compares it with `DRY_RUN` when `act` runs. A proposal approved under dry run would book for real once `DRY_RUN` is turned off.
- **`act` cannot be repeated.** The insert carries no event id, so a second run books a second event, and a crash between Google's insert and the ledger mark loses the event's id. M16's worker therefore never re-drives `act`, and settles such a decision as failed.
- **Guests are whoever the model wrote.** Nothing compares them with the email thread. `search_context` returns other threads' participants, and the reviewer can add guests: an injected email could steer an invite to an outsider, and only the owner's eye stands in the way.
- **Nothing limits spend.** Costs are recorded per span, and only inside a trace. Nothing stops a call when the month's spend is high, and nothing bounds what one large email costs.
- **There is no pause.** The only switches are environment variables that need a redeploy.

## Decisions (1 Oct 2026)

- **Spend cap: $40 a month** on model calls, warning at 80%. When it is reached, every model call stops, fail closed, until the month ends or the owner raises the cap.
- **Guests outside the thread are blocked until allowed.** The card marks them. Confirm is refused until the owner allows each address once, which keeps it as a confirmed contact, or removes it with Edit.
- **Testing happens at the end**, with the owner's other end-to-end tests. `DRY_RUN` stays `true` until then.

The owner approved this spec on 2026-10-01, after two adversarial review
rounds, with a third running.

## Scope

**In:**
- a tool registry with risk tiers, enforced by the executor;
- `DRY_RUN` enforced in the registry, not only in the Calendar client;
- approvals bound to a keyed hash of the exact arguments and to the `DRY_RUN` setting the owner saw. They are checked when the decision is recorded, before it is applied, and at execution;
- deterministic calendar event ids, so the worker can finish an interrupted `act`;
- the recipient rule, confirmed contacts, and "Allow";
- a fail-closed monthly spend cap, metering every model call, and a bound on each call's input;
- Pause, Resume and Withdraw;
- an append-only audit log, and an Activity page.

**Out:**
- keeping the owner's words apart from email content, tool-less readers, and showing where each argument came from (M18);
- planner tools (M19), connectors and pinned tool definitions (M22);
- autonomy per action type (M24);
- which calendar is written to, and whether guests are emailed: unchanged (see Open questions);
- turning `DRY_RUN` off.

---

## Design

### D1. The registry

`app/policy/registry.py`. Every action with a side effect is a registered
tool, and the registry is the only code that runs one.

```python
class Tier(IntEnum):
    READ = 0       # T0: runs without approval
    INTERNAL = 1   # T1: reversible and internal; one tap; eligible for M24
    EXTERNAL = 2   # T2: leaves the owner's account; approval every time
# T3 has no member: forbidden actions are never registered.

@dataclass(frozen=True, slots=True)
class Tool(Generic[A]):
    name: str
    tier: Tier
    args: type[A]                       # pydantic, extra="forbid"
    run: Callable[[A, RunContext], Outcome]
```

| Tool | Tier | Notes |
|---|---|---|
| `calendar.freebusy` | READ | The conflict check |
| `calendar.create_hold` | INTERNAL | No guests; refused if the arguments carry any |
| `calendar.create_invite` | EXTERNAL | At least one guest |

A test pins the exact set of registered tools. Registering a new one fails it,
which is where the owner's "ask first" applies.

`Registry.execute(name, args, *, approval)`:
1. **Unknown tool:** refused.
2. **Paused** (D6): raises `Paused`. Nothing runs, and the worker re-drives the action after Resume without counting an attempt. An action already past this point completes.
3. **READ:** runs. Not audited: reads are frequent and change nothing.
4. **INTERNAL and EXTERNAL:** D2's checks at execution, then D4's for EXTERNAL. Under `DRY_RUN` the provider is never called, and the outcome is `dry_run`; otherwise the provider runs.
5. Every INTERNAL and EXTERNAL attempt writes an audit row (D7), refusals included.

`CalendarClient` keeps its own `dry_run` guard as a second layer.

**Only the registry writes.** A test, like `tests/test_one_resumer.py`, fails
if any module other than the registry calls `create_event` or `delete_event`.
Two operator tools are named exceptions, both against the test calendar only:
`app/google/smoke.py`, and D3's probe.

### D2. Approvals bound to arguments

**The hash.** `args_hash` is an HMAC-SHA256 over a canonical JSON form:
- sorted keys;
- times in UTC as `YYYY-MM-DDTHH:MM:SSZ`;
- guests lower-cased, de-duplicated and sorted;
- absent fields as `null`;
- the target `calendar_id`, and `hash_version` (`1`).

It covers everything the provider receives, the event description included.
The key is derived from `FERNET_KEY` (HKDF, info `mailagent-args-v1`). A hash
kept for good therefore cannot be used to confirm a guessed title or time.

**Computed once, before parking.** `detect_conflicts`, the node before
`await_approval`, builds the arguments `act` would run from the same
extraction. It stores `tool`, `args_hash` and the outside guests (D4) in the
graph state. `await_approval` only copies them into the interrupt payload. On
a resume LangGraph runs `await_approval` again from its start, so nothing
there may call Gmail or the database.

**The mode a proposal shows is always the current setting.** Every write of a
`proposals` row takes `dry_run` from `DRY_RUN`, not from the payload, and
recomputes `args_hash` from the payload under the current code. That applies
to parks, re-parks, reconciliation and returns to the owner. `DRY_RUN` changes
only at a restart. At boot, pending proposals whose stored mode differs are
rewritten and announced again. The card shows a `dry run` or `live` badge.

**What the owner saw travels with the decision.** A Confirm carries the hash's
first 12 characters and the mode shown, on every channel:
- **the card's form:** both fields, hidden;
- **Telegram:** both in the button's data, and outside guests marked in the card's text. Buttons sent before M17 are refused, with a pointer to the web app, as M16 did with its own older buttons;
- **the CLI:** `approve --list` prints a token such as `3f9a1c07be42-live`, and `approve --action confirm --expect <token>` requires it whole.

`decide()` refuses a Confirm whose hash prefix or mode differs from the
proposal row's. It answers "stale" with the current version, as for a stale
revision.

**At decide.** A Confirm, in the transaction that claims the proposal, inserts
an `outbound_actions` row. A proposal parked before M17 that has no hash yet
is refused as not ready (see "Legacy").

`outbound_actions`:

| Column | Meaning |
|---|---|
| `id` | Primary key |
| `decision_id` | The decision it belongs to: unique, and a foreign key that restricts deletes |
| `message_id`, `tool`, `tier` | What it is for |
| `args_hash` | Copied from the proposal at that revision |
| `dry_run` | Copied from the proposal: the setting the owner approved under |
| `nonce` | 32 random hex characters, carried through the resume |
| `status` | `approved`, `executing`, `done`, `dry_run`, `refused` or `failed` |
| `event_id` | The deterministic id, stored when the action starts executing |
| `reason` | A fixed phrase, for refusals and failures |
| `created_at`, `started_at`, `finished_at` | Timing |

When a decision settles, by any path, an action still `approved` becomes
`refused` in the same transaction. No action is left `approved` or
`executing` once its decision has settled (but see D3, giving up).

**Before applying a Confirm,** the worker checks an action still in
`approved`:
- its `dry_run` equals `DRY_RUN`;
- the thread's payload hashes, under the current code, to its `args_hash`. A deploy that changed the canonical form, the description or the calendar fails this;
- D4's recipient rule still holds, read live from Gmail.

If any check fails, nothing runs:
- the action becomes `refused`, with a fixed reason ("approved under another mode", "the proposal changed", "guests outside the thread");
- the decision settles as `no_effect` with that reason;
- the proposal returns to `pending` at the same revision, with the current mode, hash and outside guests, and is announced again.

The thread stays parked, so no graph change is needed. A card or button from
before the change carries the old hash or mode, so `decide()` refuses it.

A Gmail error during the check holds the decision for the next tick. It costs
no attempt.

**At execution,** the registry loads the action row and checks the status
first:
- **`executing`, `done` or `dry_run`:** this decision's own earlier attempt. It is finished by the stored `event_id` (D3), with no new hash or mode check. A deploy, a key rotation or a mode change after the first attempt therefore cannot strand an event already made;
- **`approved`:** the nonce must match, compared in constant time; the arguments must hash to `args_hash`; and `DRY_RUN` must equal the action's. On success the status becomes `executing`, with the `event_id`, in the same transaction. A mismatch here, after the worker's checks, can only follow a restart between those checks and execution. It is refused and audited, and `act` marks the message FAILED with the reason;
- **anything else:** refused.

The nonce is single-use per decision: a re-drive of the same decision
continues its own action, and nothing can run another decision's.

**Legacy.**
- **Pending proposals parked before M17** have no hash. A reconciliation pass, run at boot and hourly, computes `tool`, `args_hash` and the outside guests from the thread's payload and the Gmail thread. It updates the row only while it is still `pending`.
- **A Confirm left open by M16 at deploy** has no action row. The worker returns it to the owner, as for a changed setting ("approve again").

### D3. Calendar writes that can be finished

**Deterministic id.** `event_id = "ma" + base32hex(HMAC(message_id + ":" +
args_hash))[:30]`, lower-case. Google accepts client ids of 5–1024 characters
from `a`–`v` and `0`–`9`. It is computed and stored when the action starts
executing, and every later attempt uses the stored value. A deploy that
changed how ids are derived therefore never creates a second event.

**Inserting.** The first attempt inserts with that id. Google warns that an id
collision may not be detected at creation time, so the `409` is not the only
guard:
- one worker, holding a lease, makes the first attempt;
- every later attempt asks `events.get` for the id first, and inserts only if Google has no such event;
- a `409` means the event exists: the client fetches it and uses it. That holds even when the owner has since deleted it (status `cancelled`), so a re-drive never recreates an event the owner removed.

**Checkpoints are written before moving on.** Every graph invocation uses
`durability="sync"`. LangGraph's default writes checkpoints asynchronously, so
a crash inside `act` could lose the step that recorded the approval. The
thread would then look parked and be resumed again.

**The worker finishes interrupted writes.** `step_for` re-drives a thread
whose `next` is `act` when its decision's action is `approved`, `executing`,
`done` or `dry_run`. The registry returns a stored `done` or `dry_run` outcome
without calling Google, and finishes `executing` by its stored id. Either way,
`act` then writes the ledger mark it missed. A thread with no action row was
approved before M17, and is still settled as failed (`act interrupted`).

**Giving up.** When a decision's attempts run out (M16 D1) and its action is
`executing`, the worker looks the event up:
- found: the action is `done`, the ledger `CREATED`, and the decision settles as `created`;
- not found: the action and the decision fail;
- the lookup itself fails (Google is down, which is usually why the attempts ran out): nothing is settled. The decision stays open and is retried hourly. One alert goes out ("A calendar write could not be confirmed"), and `/health` counts it apart from stuck decisions.

**A mode change after a resume.** If `DRY_RUN` is flipped by a restart while
an `approved` action's thread is already past `await_approval`, the re-drive's
mode check fails. `act` marks the message FAILED ("approved under another
mode"). This needs a restart in the seconds between resume and execution, and
the failure is visible.

**The probe.** `python -m app.jobs.calendar_probe` checks Google's real
behaviour on the test calendar only. It inserts an event with a fixed probe
id, inserts it again and expects a `409`, deletes it, and expects `events.get`
to return it as `cancelled` and a third insert to answer `409`. The owner runs
it at the end tests, before `DRY_RUN` goes off.

### D4. The recipient rule

**Who counts as a participant.** For the source thread, read with `threads.get`
(`format=metadata`, headers `From`, `To`, `Cc`, `Authentication-Results`) under
the existing `gmail.readonly` scope, leaving out messages in `SPAM` or `TRASH`:
- **the owner's own mail:** every recipient (`To`, `Cc`) of each message labelled `SENT`. The label is Gmail's, so a forged `From: owner` changes nothing;
- **mail the owner received:** the sender, but only when Gmail's `Authentication-Results` records `dmarc=pass` for the `From` domain. A sender writes their own `From` as freely as their `Cc`, and DMARC is the check that the domain stood behind it.

Everything else is outside until allowed:
- addresses only in the `To` or `Cc` of mail received;
- senders without a DMARC pass.

The owner's Allow is one tap per address, and it is kept.

A guest may be invited when the address is a participant or a confirmed
contact. Addresses are compared lower-cased and exact. For `gmail.com` and
`googlemail.com` only, dots and `+tags` are ignored, because Gmail ignores
them; elsewhere a `+tag` can be a different mailbox. The owner's own addresses
(`OWNER_EMAIL`, and now also `OWNER_ALIASES`) are stripped from guests at
extraction. A proposal whose only guest was an alias is therefore a hold, as
its action type says.

**Computed once, before parking.** `detect_conflicts` reads the thread, using
the Gmail thread id of the fetched email (the graph's own `thread_id` is the
message id), and stores `outside_guests` in the state. From there it reaches
the interrupt payload and the cards.

**Allowing.**
- **The web card:** each outside guest is marked "not in this email thread", with **Allow**. It records a confirmed contact through `POST /api/contacts` on Fly, from a server action that checks the owner. The card hides addresses already allowed, read from `confirmed_contacts` as `web_reader`.
- **The CLI:** `approve --allow <address>`.
- **Telegram:** the card marks outside guests and points to the web app.

**At decide.** A Confirm on an invite whose outside guests are not all
confirmed is refused (422, "allow or remove the guests outside the thread
first"). Nothing is recorded.

**Before applying, and at execution.** The worker re-reads the thread and the
contacts before it resumes a Confirm (D2). The registry repeats the check at
execution, reading Gmail and the contacts itself, never the graph state, so a
legacy thread is checked like any other. Gmail being down never blocks Cancel
or Edit: their resume reads nothing.

`confirmed_contacts`: `address` (primary key, normalised as above),
`allowed_at`, `via`, `message_id` (where it was allowed). Removing one is
CLI-only in M17: `python -m app.jobs.contacts --remove <address>`.

### D5. The spend cap

**One metered path.** Every model client is built by `app/policy/models.py`,
which wraps the google-genai client (`generate_content` and `embed_content`)
and the Gateway's HTTP call. A test, like `tests/test_one_resumer.py`, fails
if `genai.Client(` or the Gateway's URL appears anywhere else, so a call site
nobody wired cannot exist; M19's planner is covered by construction. The
wrapper:
1. **Before the call,** asks the gate, which refuses:
   - `UnpricedModel` when the model has no rate today. Fail closed: a model nobody priced can spend without limit;
   - `BudgetExhausted` when this month's spend has reached `MONTHLY_BUDGET_USD` (default 40).
   A refusal writes a `model_spend` row marked refused, at no cost, so "no model was called" can be checked.
2. **After the call,** writes a `model_spend` row: time, model, token counts, cost, and whether the cost is an estimate. No content.

This month's spend (UTC calendar months) is the sum of `model_spend`. Spans
stay as they are, for observability. The gate reads the month's total from
the database at most once a minute, and adds the calls it metered since.

**Embeddings** report no usage. They are priced from a character count (four
characters a token) at `gemini-embedding-001`'s published rate, which joins
`pricing.py`, and flagged as estimates.

**One call's cost is bounded.** An email's text is cut to 20,000 characters
before it enters any prompt, with a note saying so. A single call's input is
therefore bounded, and the reserve below bounds a message. Without this, a few
very large emails could use up the month's cap.

**The cap is per deployment.** Production's cap counts production's calls. The
eval harness and the model probe run locally, against the local database or
none. Development should use its own API key, so its spend never hides inside
production's budget. The runbook says so.

**Where work stops.**
- **Poll** asks `allows_new_work()` before claiming each message: spend plus a reserve of $0.10 must be under the cap. Otherwise it stops claiming. The tick still records as successful, so `/health` does not report polling as dead. Unclaimed mail waits in M20's feed, for up to its seven days.
- **A message that runs out mid-run** raises `BudgetExhausted`. Poll releases the claim, deleting its ledger row and its thread's checkpoint, so the message returns to the feed. It never becomes FAILED.
- **Search** re-raises budget errors, instead of quietly degrading to keyword search as it does for other embedding failures.
- **The worker** holds an Edit while the cap stops new work, because re-extraction calls a model. The Edit stays queued and costs no attempt, and the card says it is waiting. Confirm and Cancel call no model, so they still apply.
- **Ingestion** stops claiming batches.

**Alerts.** A push at 80% ("Model spending is at 80% of this month's cap") and
at 100% ("Model spending cap reached: mail processing has stopped"), once per
month and cap value (`alerts_sent`, subject = month and cap). Raising the cap
re-arms them. The words carry no amounts.

**Where it shows.** The gate writes `budget_state` (`ok`, `warning` or
`exhausted`) to the `control` row (D6) whenever it changes. The web app's
header shows "Spending cap reached" from it. With the bearer secret, `/health`
shows the state and the month's spend.
- An exhausted budget is not an outage: the status stays 200.
- Decisions held by it do not count toward `/health`'s one-hour clock for stuck decisions.
- A model in use with no price gives 503. A model counts as in use when a feature that calls it is on: the embedding model only with search or ingestion.

**Raising the cap** means setting `MONTHLY_BUDGET_USD` and restarting.

### D6. Pause

A single row, `control`: `id = 1`, `paused`, `changed_at`, `changed_via`,
and D5's `budget_state`.

- **From the web app:** Pause / Resume in the header, through `POST /api/pause` and `POST /api/resume` on Fly. Server actions check the owner.
- **From the CLI:** `python -m app.jobs.control pause|resume|status`. In production it runs on the instance, through `fly ssh console`: the image has no PowerShell. `.\tasks.ps1 pause` runs it locally, against the database `.env` names.
- **While paused, new work stops:**
  - poll claims nothing;
  - the worker applies no decision;
  - ingestion claims nothing;
  - the registry raises `Paused` for new actions, which the worker re-drives after Resume. An action already past the registry's entry check completes.
- **Withdraw** is a request the worker carries out, because only the worker may settle a decision it might have applied (M16 D1):
  - the card's **Withdraw** button records a request on the decision (`POST /api/decisions/withdraw`, which adds `withdraw_requested_at`);
  - the worker processes requests even while paused;
  - it settles the decision as `no_effect` ("withdrawn by the owner") only if the thread is still parked at the decision's revision and its action, if any, is still `approved`. The action becomes `refused` in the same transaction, and the proposal returns to `pending`;
  - otherwise the request is declined ("already being applied"), and the card says so.
- **What carries on:** reads, reconciliation, the purge, the token check, and M20's mail sync. None calls a model. They may still send the owner notifications: a reconciled proposal is announced, and a token alert goes out.
- **Health.** `/health` shows `paused` with the bearer. A paused tick records as successful, and held decisions do not count as stuck, so pausing is not an outage.

Every change is audited.

### D7. The audit log

`audit_log`: `id`, `at`, `kind`, `tool`, `tier`, `args_hash`, `dry_run`,
`outcome`, `decision_id`, `message_id`, `subject_hash`, `reason`.

- **Kinds:** `action_approved`, `action_executed`, `action_refused`, `action_failed`, `decision_withdrawn`, `withdraw_declined`, `paused`, `resumed`, `budget_warning`, `budget_exhausted`, `contact_allowed`, `contact_removed`, `write_unconfirmed`.
- **No content.** Keyed hashes, ids, outcomes and fixed phrases only. A contact's row carries a keyed hash of the address in `subject_hash`, linking it to the contact without spelling it out. A test seeds known strings and checks no row contains them.
- **Append-only.** A trigger rejects `UPDATE` and `DELETE`. Kept for good. It guards against code paths, not against the database owner: `TRUNCATE` is not blocked.
- **No foreign keys.** It holds ids, so a delete elsewhere never cascades into it.
- **Activity page** (`/activity`): the latest 100 entries, read as `web_reader`.

### D8. Records and retention

Migration `010_action_policy.sql` (additive, re-runnable):
- `control`, `outbound_actions`, `confirmed_contacts`, `model_spend`, `audit_log` and its trigger;
- `proposals.tool`, `proposals.args_hash` and `decisions.withdraw_requested_at`;
- `SELECT` for `web_reader` on `control`, `confirmed_contacts` and `audit_log`.

`outbound_actions.decision_id` restricts deletes, like `decisions` does.
The test fixture and `poll --reset` (development only) delete
`outbound_actions` before `decisions`.

None of these holds email content. `outbound_actions`, `model_spend` and
`audit_log` are M24's evidence and the budget's, and are kept for good.
Confirmed contacts stay until removed.

---

## Deliverables

- **Python:**
  - `app/policy/` (`registry`, `hashing`, `models`, `budget`, `control`, `audit`, `contacts`, `participants`);
  - the hash, the tool and the outside guests computed in `detect_conflicts`; `act` through the registry;
  - `durability="sync"` on every graph invocation;
  - proposal rows always written with the current mode, and the boot pass that refreshes them;
  - `decide()` checking the hash prefix and the mode, inserting the action, and refusing an unconfirmed outsider;
  - the worker: its checks before a Confirm; re-driving interrupted writes; giving up without failing blind; holding edits and all decisions as D5 and D6 say; carrying out Withdraw;
  - the reconciliation pass for legacy proposals;
  - the Calendar client's deterministic id, its lookup and its `409` handling, and the probe;
  - the metered model clients, built at every place a client is built today; the input bound; the embedding rate;
  - API routes for pause, resume, withdraw and contacts; `/health` fields;
  - Telegram buttons and cards, and the approve CLI's `--expect` and `--allow`;
  - `app/jobs/control.py`, `app/jobs/contacts.py`, `app/jobs/calendar_probe.py`.
- **Web:**
  - the hash prefix and mode in the card's form, and the mode badge;
  - Allow on the card;
  - Pause / Resume and the banner in the header;
  - Withdraw on a queued card;
  - the Activity page.
- **Migration** `010_action_policy.sql`.
- **Docs:** `docs/DEPLOY.md` (cap, pause, withdraw, contacts, the probe, the mode-check procedure), README's safety section.

## Commands

```powershell
.\tasks.ps1 check
.\tasks.ps1 pause            # local; on Fly: fly ssh console -C "sh -c 'cd /app && python -m app.jobs.control pause'"
uv run python -m app.jobs.calendar_probe   # test calendar only
uv run --env-file .env.test pytest -m integration -o addopts="" -q -p no:cacheprovider
cd dashboard; npm run typecheck; npm test; npm run build
```

## Testing

- **Registry:**
  - the registered set is pinned;
  - nothing but the registry and the two named operator tools calls the Calendar client's writes;
  - a hold with guests is refused;
  - under `DRY_RUN` the provider is never called;
  - while paused nothing new runs, and a refused action is re-driven after Resume, costing no attempt.
- **Binding:**
  - a changed argument (title, time, guest, calendar) is refused and audited;
  - another decision's nonce is refused;
  - a completed action returns its stored outcome and books nothing;
  - a Confirm carrying an old hash prefix or mode is refused as stale, on the web, Telegram and the CLI;
  - a `DRY_RUN` change rewrites pending proposals at boot, and returns queued Confirms to the owner;
  - a changed canonical form returns queued Confirms to the owner;
  - a re-drive after a code or key change finishes by the stored id;
  - an M16 Confirm open at deploy is returned to the owner;
  - a settled decision leaves no `approved` action.
- **Interrupted writes,** with a fake Calendar on Neon, a crash injected after each step:
  - every case ends with exactly one event, the ledger `CREATED`, and no action left `executing`;
  - a `409` returns the existing event; an event the owner deleted is not recreated;
  - give-up finds an event made before the crash; a failing lookup keeps the decision open and alerts once.
- **Recipients:**
  - an address only in an inbound `Cc` is outside;
  - a sender without `dmarc=pass` is outside, and one with it passes;
  - a recipient of mail labelled `SENT` passes, but a forged `From: owner` adds nothing;
  - a confirmed contact passes;
  - an outsider blocks Confirm until allowed, from the web or the CLI;
  - an outsider added by an edit is marked;
  - Gmail dots and `+tags` match; a `+tag` elsewhere does not;
  - Gmail being down holds a Confirm and never blocks Cancel or Edit.
- **Budget:**
  - an unpriced model in use is refused before any call, and the refusal is recorded;
  - `genai.Client(` outside the wrapper fails the construction test;
  - an email over the input bound is cut, with the note;
  - poll stops claiming at the cap, and the tick is still recorded as successful;
  - a message exhausted mid-run returns to the feed, not FAILED;
  - Edit is held while Confirm and Cancel apply, and the held Edit is not "stuck";
  - each alert is sent once per month and cap, and raising the cap re-arms it.
- **Pause and Withdraw:**
  - poll, the worker and ingestion stop within one tick, and resume afterwards;
  - Withdraw settles a decision that was never resumed, and declines one already applied, even with its lease cleared by a failed attempt;
  - `/health` stays 200.
- **Audit:**
  - every INTERNAL and EXTERNAL attempt is recorded;
  - `UPDATE` and `DELETE` fail;
  - no seeded email string appears in any row.
- **Web:** Allow, the mode badge, Pause, Withdraw and Activity, in the browser, against a local API.
- **End-to-end, by the owner at the end:** the probe, then the exit criterion.

## Boundaries

- **Always:**
  - every side effect through the registry;
  - every model client built by the metered wrapper;
  - approvals checked when recorded, before they are applied, and at execution;
  - audit rows without content;
  - `DRY_RUN` stays `true` until the owner's end tests.
- **Ask first:**
  - registering any new INTERNAL or EXTERNAL tool;
  - changing the cap's default, the reserve or the input bound;
  - any schema beyond `010`.
- **Never:**
  - a T3 tool;
  - a model-chosen recipient reaching an invite without passing D4;
  - executing a first attempt on a mismatched hash or setting;
  - content in the audit log or the spend record;
  - turning `DRY_RUN` off.

## Exit criterion

Proved on the deployed stack at the owner's end tests, with `DRY_RUN` off on
the test calendar, after the probe passes:

1. **Bound.**
   - Confirming a hold creates exactly one event, with the deterministic id.
   - The mode check: Pause, Confirm a proposal parked under dry run, switch `DRY_RUN` off and restart, then Resume. The proposal comes back with a `live` badge, and nothing is booked. The old card's Confirm is refused as stale.
2. **Finishable.** Shown by the fault-injection tests on Neon, against Google's behaviour as the probe confirmed it. No crash point leaves two events, or none where one was approved. A deployed check would need a crash timed to milliseconds.
3. **Recipients.** An invite whose guest appears only in an inbound `Cc` cannot be confirmed until that guest is allowed.
4. **Cap.** With `MONTHLY_BUDGET_USD` set below the month's spend:
   - polling stops and a push arrives;
   - `model_spend` shows only refusals from then on;
   - raising the cap restarts polling.
5. **Pause.**
   - Pause stops polling and applying within one tick.
   - Withdraw returns a queued decision.
   - Resume restarts both.
6. **Audit.** Every attempt has an audit row, and none quotes an email.

## Open questions

- **Which calendar, and whether guests are emailed.** Events go to `TEST_CALENDAR_ID`. The insert does not set `sendUpdates`, so Google sends guests no invitation email, though its documentation warns that some emails may still be sent. Both stay as they are in M17. Before `DRY_RUN` goes off for real use, the owner chooses the calendar, and whether an invite should email its guests.
