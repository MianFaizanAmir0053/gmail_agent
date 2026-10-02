# M17 · Action policy

**Est.** 5 days · **Depends on** M15, and M16's decision queue · **Blocks** M18, M19, M22, M24, M27

Plan and decisions: [`ASSISTANT-PLAN.md`](../../ASSISTANT-PLAN.md), section "Security model".

## Goal

Every side effect the agent can cause goes through one registry that enforces
the rules in code:
- what tier the action is;
- whether the owner approved these exact arguments, under the `DRY_RUN` setting the proposal was made and shown under;
- whether its recipients are people in the thread whom Gmail verified, whom the owner wrote to, or whom the owner allowed;
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
- **An approval binds only a revision.** `decide()` claims `(message_id, revision)`. `act` then runs whatever the checkpoint's extraction holds (`app/graph/nodes.py`, `act`). Nothing ties the event that is created to the card the owner saw.
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
rounds. The third round's findings were folded in the same day (see Review).

## Scope

**In:**
- a tool registry with risk tiers, enforced by the executor;
- `DRY_RUN` enforced in the registry, not only in the Calendar client;
- approvals bound to a keyed hash of the exact arguments and to the `DRY_RUN` setting. They are checked when the decision is recorded, before it is applied, and at execution;
- proposals made under another `DRY_RUN` setting expired, never re-shown;
- deterministic calendar event ids, and the exact request kept while a write is in flight, so the worker can finish an interrupted `act`;
- the recipient rule, confirmed contacts, and "Allow";
- a fail-closed monthly spend cap metering every model call; a bound on each prompt; a ceiling on each message;
- Pause, Resume and Withdraw;
- an append-only audit log, and an Activity page.

**Out:**
- keeping the owner's words apart from email content, tool-less readers, and showing where each argument came from (M18);
- planner tools (M19), connectors and pinned tool definitions (M22);
- autonomy per action type (M24);
- which calendar is written to, and whether guests are emailed: unchanged (see Open questions);
- turning `DRY_RUN` off.

**Depends on M20 for one promise.** Mail held by Pause or the spending cap
waits in M20's feed, for up to its seven days. M20 is built straight after
M17, and both ship together. Until then, mail held that way is only as safe as
the old poller's page of ten.

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

`Registry.execute(name, args, *, approval, message_id)`:
1. **Unknown tool:** refused.
2. **Paused** (D6): raises `PausedError`. Nothing runs. The worker releases its lease without counting an attempt, and picks the action up again as soon as the owner resumes.
3. **READ:** runs. Not audited: reads are frequent and change nothing.
4. **INTERNAL and EXTERNAL:** D2's checks, then D4's for EXTERNAL, all before anything is marked as started. Under `DRY_RUN` the provider is never called to write; D3 says what a later attempt may still do.
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
- the tool, the target `calendar_id`, and `hash_version` (`1`).

It covers everything the provider receives, the event description included.
The key is derived from `FERNET_KEY` (HKDF, info `mailagent-args-v1`). A hash
kept for good therefore cannot be used to confirm a guessed title or time.

**Taken at every row write.** Whenever a `proposals` row is written (park,
re-park, reconciliation, a return to the owner), its `tool` and `args_hash`
are computed from the thread's payload under the current code. Nothing about
the hash lives in the graph state.

**The mode a proposal was made under stays its mode.** `proposals.dry_run` is
the payload's, as M16 records it; a payload with none counts as a dry run. A
proposal whose mode differs from `DRY_RUN` is expired, never re-shown:
- **At boot,** for each `pending` proposal whose mode differs, a reconciliation pass records a sweep decision with the reason "made under another mode". The worker applies it, and the thread ends `REJECTED`, as M15's sweep does.
- **A queued Confirm** whose action's mode differs is not applied. The action is `refused`, the decision settles as `no_effect`, and the proposal is expired the same way.
- **`decide()`** refuses a Confirm on a row whose mode differs from `DRY_RUN`, as stale.

An approval therefore always runs under the setting the proposal was made
under, as the plan's capability map requires. Turning `DRY_RUN` off starts
afresh: new mail makes new proposals.

**What the owner saw travels with the decision.** A Confirm carries a token on
every channel:
- the hash's first 12 characters;
- the mode shown;
- the proposal's `generation`, which goes up whenever a proposal returns to the owner (D2 below, D4, D6).

The token travels like this:
- **the card's form:** hidden fields. The card shows a `dry run` or `live` badge;
- **Telegram:** in the button's data, and outside guests are marked in the card's text. Buttons sent before M17 are refused, with a pointer to the web app, as M16 did with its own older buttons;
- **the CLI:** `approve --list` prints the token, such as `3f9a1c07be42-live-2`, and `approve --action confirm --expect <token>` requires it whole.

`decide()` refuses a Confirm whose token differs from the row's: it answers
"stale" with the current version, as for a stale revision. A replayed Confirm
from before a Withdraw or a return carries an old generation, so it dies.

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
| `dry_run` | Copied from the proposal: the setting it was made and approved under |
| `nonce` | 32 random hex characters, carried through the resume |
| `status` | `approved`, `executing`, `done`, `dry_run`, `refused` or `failed` |
| `calendar_id`, `event_id`, `request` | Stored when the action starts executing (D3). `request` is the exact body sent, kept only while the write is in flight |
| `reason` | A fixed phrase, for refusals and failures |
| `created_at`, `started_at`, `finished_at` | Timing |

When a decision settles, by any path, an action still `approved` becomes
`refused` in the same transaction. No action is left `approved` once its
decision has settled.

**Before applying a Confirm,** the worker, holding its lease:
1. checks for a withdraw request first (D6);
2. checks an action still in `approved`:
   - its mode equals `DRY_RUN` (otherwise the proposal is expired, as above);
   - the thread's payload hashes, under the current code, to its `args_hash`. A deploy that changed the canonical form, the description or the calendar fails this;
   - D4's recipient rule holds, read live from Gmail.

If the hash or the recipients fail, nothing runs:
- the action becomes `refused`, with a fixed reason ("the proposal changed", "guests outside the thread");
- the decision settles as `no_effect` with that reason;
- the proposal returns to `pending` at the same revision, with the current hash and outside guests and the next `generation`, and is announced again.

The thread stays parked, so no graph change is needed.

**At execution,** the registry loads the action row and checks its status
first:
- **`executing`, `done` or `dry_run`:** this decision's own earlier attempt, finished by D3 from what the row stored. No new hash, mode or recipient check, and nothing rebuilt from current code;
- **`approved`:** every check runs before anything is marked started: the nonce, compared in constant time; the arguments' hash against `args_hash`; `DRY_RUN` against the action's; and for an invite, D4's recipients. Only when all pass does the status become `executing`, storing the `calendar_id`, the `event_id` and the exact `request`, in one transaction. A failed check is refused and audited, and `act` marks the message FAILED with the reason. Coming after the worker's own checks, that can only follow a restart in between;
- **anything else:** refused.

The nonce is single-use per decision: a re-drive of the same decision
continues its own action, and nothing can run another decision's.

**Legacy.**
- **Pending proposals parked before M17** have no hash. A reconciliation pass, at boot and hourly, computes `tool`, `args_hash` and the outside guests from the thread's payload and the Gmail thread. It updates the row only while it is still `pending` at the revision it read.
- **A Confirm left open by M16 at deploy** has no action row. The worker returns its proposal to the owner ("approve again"), with the next generation.

### D3. Calendar writes that can be finished

**Deterministic id.** `event_id = "ma" + base32hex(HMAC(message_id + ":" +
args_hash))[:30]`, lower-case. Google accepts client ids of 5–1024 characters
from `a`–`v` and `0`–`9`.

**The first attempt** stores the `calendar_id`, the `event_id` and the exact
request body on the action row as it becomes `executing`, then inserts.

**Every later attempt replays what was stored,** never rebuilding from
current code, settings or calendar:
- it asks `events.get` on the stored calendar for the stored id first. Google warns that an id collision may not be detected at creation time, so the `409` is not the only guard;
- found, the event is used, even if the owner has since deleted it (status `cancelled`), so a re-drive never recreates an event the owner removed;
- not found, and `DRY_RUN` is off: the stored request is inserted, unchanged;
- not found, and `DRY_RUN` is now on: nothing is written. The action is `refused` ("made under another mode"), and `act` marks the message FAILED. The kill switch never writes.

A `409` on any insert means the event exists: the client fetches it and uses
it. When the action finishes, `request` is cleared: it held the event's
content only while the write was in flight.

**Checkpoints are written before moving on.** Every graph invocation uses
`durability="sync"`. LangGraph's default writes checkpoints asynchronously, so
a crash inside `act` could lose the step that recorded the approval. The
thread would then look parked and be resumed again.

**The worker finishes interrupted writes.** `step_for` re-drives a thread
whose `next` is `act` whenever its decision has an action, whatever the
action's status:
- the registry checks an `approved` action as at any first attempt;
- it returns a stored `done` or `dry_run` outcome, or a `refused` or `failed` one with its reason, without calling Google;
- it finishes `executing` as above.

Either way, `act` then writes the ledger mark it missed, and a refusal's
fixed reason becomes the decision's. A thread with no action row was approved
before M17, and is still settled as failed (`act interrupted`).

**Giving up.** When a decision's attempts run out (M16 D1) and its action is
`executing`, the worker looks the event up:
- found: the action is `done`, the ledger `CREATED`, and the decision settles as `created`;
- not found: the action and the decision fail;
- the lookup itself fails (Google is down, which is usually why the attempts ran out): nothing is settled. The decision stays open and is retried hourly, and one alert goes out ("A calendar write could not be confirmed"). `/health` counts it apart from stuck decisions.

**A mode change after a resume.** If `DRY_RUN` is flipped by a restart while
an `approved` action's thread is past `await_approval` (resumed, then held by
a Pause raised at `act`), the re-drive's mode check fails. `act` marks the
message FAILED ("made under another mode"). Flipping the mode with work in
flight fails that work visibly rather than running it under the other setting.

**The probe.** `python -m app.jobs.calendar_probe` checks Google's real
behaviour, on the test calendar only. It is the one tool that ignores
`DRY_RUN`, and says so when it starts. With a fresh random id each run, it:
1. inserts an event, inserts it again and expects a `409`;
2. deletes it, expects `events.get` to return it as `cancelled`, and expects another insert to answer `409`.

The owner runs it at the end tests, before `DRY_RUN` goes off.

### D4. The recipient rule

**Who counts as a participant.** For the source thread, read with `threads.get`
under the existing `gmail.readonly` scope (`format=metadata`, headers `From`,
`To`, `Cc` and `Authentication-Results`, and a field mask that leaves out the
snippet), leaving out messages in `SPAM` or `TRASH`:
- **the owner's own mail:** every recipient (`To`, `Cc`) of each message labelled `SENT`. The label is Gmail's, so a forged `From: owner` changes nothing;
- **mail the owner received:** the sender, but only when the topmost `Authentication-Results` header is Gmail's own (authserv-id `mx.google.com`) and records `dmarc=pass` with `header.from` equal to the `From` address's domain. A sender writes their own `From` as freely as their `Cc`. Gmail's DMARC result is the check that the domain stood behind it, and only Gmail's topmost header can be trusted to be Gmail's.

Everything else is outside until allowed:
- addresses only in the `To` or `Cc` of mail received;
- senders without that DMARC pass.

The owner's Allow is one tap per address, and it is kept. A cold sender who
passes DMARC for their own domain does count as a participant: inviting
someone to the meeting they asked for is the normal case.

A guest may be invited when the address is a participant or a confirmed
contact. Addresses are compared lower-cased and exact. For `gmail.com` and
`googlemail.com` only, dots and `+tags` are ignored, because Gmail ignores
them; elsewhere a `+tag` can be a different mailbox. The owner's own addresses
(`OWNER_EMAIL` and, now also, `OWNER_ALIASES`) are stripped from guests at
extraction, so a proposal whose only guest was an alias is a hold.

**Computed before parking.** `detect_conflicts` reads the thread, using the
Gmail thread id of the fetched email (the graph's own `thread_id` is the
message id), and stores `outside_guests` in the state. From there it reaches
the interrupt payload and the cards. If the read fails, after its retries, or
the thread is gone, every guest is treated as outside: the proposal still
parks, and the owner can Allow. A Gmail outage therefore never fails an Edit's
re-park.

**Allowing.**
- **The web card:** each outside guest is marked "not in this email thread", with **Allow**. It records a confirmed contact through `POST /api/contacts` on Fly, from a server action that checks the owner. The card hides addresses already allowed, read from `confirmed_contacts` as `web_reader`.
- **The CLI:** `approve --allow <address>`.
- **Telegram:** the card marks outside guests and points to the web app.

**At decide.** A Confirm on an invite whose outside guests are not all
confirmed is refused (422, "allow or remove the guests outside the thread
first"). Nothing is recorded.

**Before applying, and at execution.** The worker re-reads the thread and the
contacts before it resumes a Confirm (D2). A Gmail error holds the decision
without costing an attempt, for up to an hour. After that, or on a `404`, the
proposal returns to the owner with every guest outside. The registry repeats
the check before an action starts executing, reading Gmail and the contacts
itself, never the graph state, so a legacy thread is checked like any other.
Cancel and Edit read nothing at resume, so Gmail being down never blocks them.

`confirmed_contacts`: `address` (primary key, normalised as above),
`allowed_at`, `via`, `message_id` (where it was allowed). Removing one is
CLI-only in M17: `python -m app.jobs.contacts --remove <address>`.

### D5. The spend cap

**One metered path.** Every model client is built by `app/policy/models.py`.
It wraps the google-genai client and the Gateway's HTTP call, and exposes
exactly two operations, `generate_content` and `embed_content`. It is not a
transparent proxy: streaming, async clients and caches are simply not there,
so nothing can reach the model around the meter. A test, like
`tests/test_one_resumer.py`, fails if `genai.Client(` or the Gateway's URL
appears anywhere else. M19's planner is covered by construction.

The wrapper:
1. **Before the call,** asks the gate, which refuses:
   - `UnpricedModel` when the model has no rate today. Fail closed: a model nobody priced can spend without limit;
   - `BudgetExhausted` when this month's spend has reached `MONTHLY_BUDGET_USD` (default 40);
   - `MessageTooCostly` when the message being processed has already spent `MESSAGE_CEILING_USD` (default 0.50).
   A refusal writes a `model_spend` row marked refused, at no cost, so "no model was called" can be checked.
2. **After the call,** writes a `model_spend` row: time, model, the message it served, token counts, cost, and whether the cost is an estimate. No content.

This month's spend (UTC calendar months) is the sum of `model_spend`. Spans
stay as they are, for observability. The gate reads the month's total from
the database at most once a minute, and adds the calls it metered since. It
needs a database: with none it refuses everything. The eval harness and the
model probe run against the local database. Development should use its own
API key, so its spend never hides inside production's budget; the runbook says
so.

**Embeddings** report no usage. They are priced from a character count (four
characters a token) at `gemini-embedding-001`'s published rate, which joins
`pricing.py`, and flagged as estimates.

**Each prompt is bounded.** The assembled text of every call, the email with
its headers and any search results included, is cut to 24,000 characters,
with a note saying so. A single call's cost is therefore bounded, and the
message ceiling bounds a message's. Without both, a few very large emails, or
one that sets off every retry and review, could use up the month's cap.

**Where work stops.**
- **Poll** asks `allows_new_work()` before claiming each message: spend plus a reserve of $0.10 must be under the cap. Otherwise it stops claiming. The tick still records as successful, so `/health` does not report polling as dead. Unclaimed mail waits in M20's feed.
- **A message stopped mid-run** by `BudgetExhausted` or `UnpricedModel` is released: poll deletes its ledger row and its thread's checkpoint, so it returns to the feed and is processed, from the start, once spending is allowed again. It never becomes FAILED.
- **A message over its ceiling** is recorded as `SKIPPED` ("too costly to read"), and audited.
- **Search** re-raises the gate's refusals, instead of quietly degrading to keyword search as it does for other embedding failures.
- **The worker** holds an Edit while the cap stops new work, because re-extraction calls a model. The Edit stays queued and costs no attempt, and the card says it is waiting. Confirm and Cancel call no model, so they still apply.
- **Ingestion** stops claiming batches.

**Alerts.** A push at 80% ("Model spending is at 80% of this month's cap") and
at 100% ("Model spending cap reached: mail processing has stopped"), once per
month and cap value (`alerts_sent`, subject = month and cap). Raising the cap
re-arms them. The words carry no amounts.

**Where it shows.** The gate writes `budget_state` (`ok`, `warning` or
`exhausted`) to the `control` row (D6) whenever it changes, and audits the
change. The web app's header shows "Spending cap reached" from it. With the
bearer secret, `/health` shows the state and the month's spend.
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
  - the registry raises `PausedError` for new actions. The worker releases the lease without counting an attempt, and makes the decision due at once, so it moves on as soon as the owner resumes. An action already past the registry's checks completes.
- **Withdraw** is a request the worker carries out, because only the worker may settle a decision it might have applied (M16 D1):
  - the card's **Withdraw** button records a request on the decision (`POST /api/decisions/withdraw`, which sets `withdraw_requested_at`);
  - the worker, holding its lease, looks for a request before anything else, and processes requests even while paused;
  - it settles the decision as `no_effect` ("withdrawn by the owner") only if the thread is still parked at the decision's revision and its action, if any, is still `approved`. The action becomes `refused` and the proposal returns to `pending` with the next `generation`, in the same transaction;
  - otherwise the request is declined ("already being applied"), and the card says so.
- **What carries on:** reads, reconciliation, the purge, the token check, and M20's mail sync. None calls a model. They may still send the owner notifications: a reconciled proposal is announced, and a token alert goes out.
- **Health.** `/health` shows `paused` with the bearer. A paused tick records as successful, and held decisions do not count as stuck, so pausing is not an outage.

Every change is audited.

### D7. The audit log

`audit_log`: `id`, `at`, `kind`, `tool`, `tier`, `args_hash`, `dry_run`,
`outcome`, `decision_id`, `message_id`, `subject_hash`, `reason`.

- **Kinds:** `action_approved`, `action_executed`, `action_refused`, `action_failed`, `decision_withdrawn`, `withdraw_declined`, `proposal_expired`, `paused`, `resumed`, `budget_warning`, `budget_exhausted`, `budget_ok`, `message_too_costly`, `contact_allowed`, `contact_removed`, `write_unconfirmed`.
- **No content.** Keyed hashes, ids, outcomes and fixed phrases only. A contact's row carries a keyed hash of the address in `subject_hash`, linking it to the contact without spelling it out. A test seeds known strings and checks no row contains them.
- **Append-only.** A trigger rejects `UPDATE` and `DELETE`. Kept for good. It guards against code paths, not against the database owner: `TRUNCATE` is not blocked.
- **No foreign keys.** It holds ids, so a delete elsewhere never cascades into it.
- **Activity page** (`/activity`): the latest 100 entries, read as `web_reader`.

M20's feed recall reads the pause and budget kinds, to leave out hours when no
work was meant to happen.

### D8. Records and retention

Migration `010_action_policy.sql` (additive, re-runnable):
- `control`, `outbound_actions`, `confirmed_contacts`, `model_spend`, `audit_log` and its trigger;
- `proposals.tool`, `proposals.args_hash`, `proposals.generation`, and `decisions.withdraw_requested_at`;
- `SELECT` for `web_reader` on `control`, `confirmed_contacts` and `audit_log`.

`outbound_actions.decision_id` restricts deletes, like `decisions` does. The
test fixture and `poll --reset` (development only) delete `outbound_actions`
before `decisions`.

None of these holds email content once a write is done: `outbound_actions`
keeps the request only while it is in flight, and the purge clears any left
after 7 days. `outbound_actions`, `model_spend` and `audit_log` are M24's
evidence and the budget's, and are kept for good. Confirmed contacts stay until
removed.

---

## Deliverables

- **Python:**
  - `app/policy/` (`registry`, `hashing`, `models`, `budget`, `control`, `audit`, `contacts`, `participants`);
  - the outside guests computed in `detect_conflicts`; `act` through the registry;
  - `durability="sync"` on every graph invocation;
  - proposal rows taking `tool`, `args_hash` and `generation` at every write; the boot pass that expires proposals made under another mode;
  - `decide()` checking the token, inserting the action, and refusing an unconfirmed outsider or a mismatched mode;
  - the worker:
    - withdraw requests first, then its checks before a Confirm;
    - re-driving interrupted writes from what was stored;
    - giving up without failing blind;
    - holding edits and all decisions as D5 and D6 say;
  - the reconciliation pass for legacy proposals;
  - the Calendar client's deterministic id, its lookup, its `409` handling, and replaying a stored request; the probe;
  - the metered model clients, built at every place a client is built today; the prompt bound; the message ceiling; the embedding rate;
  - API routes for pause, resume, withdraw and contacts; `/health` fields;
  - Telegram buttons and cards, and the approve CLI's `--expect` and `--allow`;
  - `app/jobs/control.py`, `app/jobs/contacts.py`, `app/jobs/calendar_probe.py`.
- **Web:**
  - the token in the card's form, and the mode badge;
  - Allow on the card;
  - Pause / Resume and the banner in the header;
  - Withdraw on a queued card;
  - the Activity page.
- **Migration** `010_action_policy.sql`.
- **Docs:** `docs/DEPLOY.md` (cap, pause, withdraw, contacts, the probe, what turning `DRY_RUN` off expires), README's safety section.

## Commands

```powershell
.\tasks.ps1 check
.\tasks.ps1 pause            # local; on Fly: fly ssh console -C "sh -c 'cd /app && python -m app.jobs.control pause'"
uv run python -m app.jobs.calendar_probe   # test calendar only; ignores DRY_RUN
uv run --env-file .env.test pytest -m integration -o addopts="" -q -p no:cacheprovider
cd dashboard; npm run typecheck; npm test; npm run build
```

## Testing

- **Registry:**
  - the registered set is pinned;
  - nothing but the registry and the two named operator tools calls the Calendar client's writes;
  - a hold with guests is refused;
  - under `DRY_RUN` the provider never writes;
  - while paused nothing new runs; the decision's lease is released, it costs no attempt, and it moves on at Resume.
- **Binding:**
  - a changed argument (title, time, guest, calendar) is refused and audited;
  - another decision's nonce is refused;
  - a completed action returns its stored outcome and books nothing;
  - a Confirm carrying an old hash prefix, mode or generation is refused as stale, on the web, Telegram and the CLI;
  - a replayed Confirm after a Withdraw dies on its generation;
  - a proposal made under another mode is expired at boot, and a queued Confirm on one expires it;
  - a changed canonical form returns queued Confirms to the owner;
  - a re-drive after a code, key or calendar change replays the stored request on the stored calendar;
  - with `DRY_RUN` on, a later attempt only looks the event up;
  - an M16 Confirm open at deploy is returned to the owner;
  - a settled decision leaves no `approved` action, and no `request` outlives its write.
- **Interrupted writes,** with a fake Calendar on Neon, a crash injected after each step:
  - every case ends with exactly one event, the ledger `CREATED`, and no action left `executing`;
  - a `409` returns the existing event; an event the owner deleted is not recreated;
  - give-up finds an event made before the crash; a failing lookup keeps the decision open and alerts once.
- **Recipients:**
  - an address only in an inbound `Cc` is outside;
  - a sender passes only on the topmost, Gmail-authored `dmarc=pass` for their `From` domain; a forged header lower down is ignored;
  - a recipient of mail labelled `SENT` passes, but a forged `From: owner` adds nothing;
  - a confirmed contact passes;
  - an outsider blocks Confirm until allowed, from the web or the CLI;
  - an outsider added by an edit is marked;
  - Gmail dots and `+tags` match; a `+tag` elsewhere does not;
  - a failed or `404` thread read marks every guest outside and never fails a re-park;
  - Gmail being down holds a Confirm for up to an hour and never blocks Cancel or Edit.
- **Budget:**
  - an unpriced model in use is refused before any call, and the refusal is recorded;
  - `genai.Client(` outside the wrapper fails the construction test, and the wrapper offers nothing but its two operations;
  - a prompt over 24,000 characters is cut, with the note;
  - a message over its ceiling is skipped as too costly;
  - poll stops claiming at the cap, and the tick is still recorded as successful;
  - a message stopped mid-run returns to the feed, not FAILED;
  - Edit is held while Confirm and Cancel apply, and the held Edit is not "stuck";
  - each alert is sent once per month and cap, and raising the cap re-arms it.
- **Pause and Withdraw:**
  - poll, the worker and ingestion stop within one tick, and resume afterwards;
  - a withdraw request is seen before the worker applies anything;
  - Withdraw settles a decision that was never resumed, and declines one already applied, even with its lease cleared by a failed attempt;
  - `/health` stays 200.
- **Audit:**
  - every INTERNAL and EXTERNAL attempt is recorded;
  - `UPDATE` and `DELETE` fail;
  - no seeded email string appears in any row.
- **Web:** the token and badge, Allow, Pause, Withdraw and Activity, in the browser, against a local API.
- **End-to-end, by the owner at the end:** the probe, then the exit criterion.

## Boundaries

- **Always:**
  - every side effect through the registry;
  - every model client built by the metered wrapper;
  - approvals checked when recorded, before they are applied, and at execution, every check before an action is marked started;
  - audit rows without content;
  - `DRY_RUN` stays `true` until the owner's end tests.
- **Ask first:**
  - registering any new INTERNAL or EXTERNAL tool;
  - changing the cap's default, the reserve, the prompt bound or the message ceiling;
  - any schema beyond `010`.
- **Never:**
  - a T3 tool;
  - a model-chosen recipient reaching an invite without passing D4;
  - executing a first attempt on a mismatched hash, setting or recipient;
  - rebuilding an interrupted write from current code;
  - content in the audit log or the spend record, or in an action once its write is done;
  - turning `DRY_RUN` off.

## Exit criterion

Proved on the deployed stack at the owner's end tests, with `DRY_RUN` off on
the test calendar, after the probe passes:

1. **Bound.**
   - Confirming a hold creates exactly one event, with the deterministic id.
   - The mode check: Pause, Confirm a proposal made under dry run, switch `DRY_RUN` off and restart, then Resume. The proposal is expired ("made under another mode"), nothing is booked, and the old card's Confirm is refused as stale.
2. **Finishable.** Shown by the fault-injection tests on Neon, against Google's behaviour as the probe confirmed it. No crash point leaves two events, or none where one was approved. A deployed check would need a crash timed to milliseconds.
3. **Recipients.** An invite whose guest appears only in an inbound `Cc` cannot be confirmed until that guest is allowed.
4. **Cap.** With `MONTHLY_BUDGET_USD` set below the month's spend:
   - polling stops and a push arrives;
   - `model_spend` shows only refusals from then on;
   - raising the cap restarts polling.
5. **Pause.**
   - Pause stops polling and applying within one tick.
   - Withdraw returns a queued decision, and its old card cannot confirm it.
   - Resume restarts both.
6. **Audit.** Every attempt has an audit row, and none quotes an email.

## Open questions

- **Which calendar, and whether guests are emailed.** Events go to `TEST_CALENDAR_ID`. The insert does not set `sendUpdates`, so Google sends guests no invitation email, though its documentation warns that some emails may still be sent. Both stay as they are in M17. Before `DRY_RUN` goes off for real use, the owner chooses the calendar, and whether an invite should email its guests.

## Review

Three adversarial rounds, each by a reviewer with fresh context, on 2026-10-01.
- **The first** moved every check that can fail out of `await_approval`, and made checkpoints synchronous. It narrowed "participants" to senders Gmail verified and recipients of the owner's own mail, and keyed the hash. It also metered every call through one wrapper, added Withdraw, and carried what the owner saw with each Confirm.
- **The second** made Withdraw a request the worker carries out, made re-drives finish from what was stored, and made a Pause hold work rather than fail it. It also bounded each call's input, and added the probe.
- **The third** stored the exact request and calendar while a write is in flight, and replayed them, never rebuilt. It ran every check before an action is marked started. It expired proposals made under another mode instead of re-showing them as live (the plan's capability map binds the mode a proposal was made under), and added a generation to the card's token so replayed Confirms die. It also pinned DMARC to Gmail's own topmost header, bounded the whole prompt and each message, and gave the probe a random id.

Three rounds still found substantive issues, narrower each time. What remains
is checked by the build's own reviews, the fault-injection tests, the probe,
and the owner's end tests.

## Running notes

### Tasks 17.1–17.4: records, the hash, checkpoints, the token (2026-10-01)

- **One place builds the arguments.** `event_args` in `app/policy/hashing.py` makes them for the hash at park and for the call at `act`, so the two cannot drift apart. The description names the message, and the hash covers it.
- **The owner's aliases reach the extraction.** `PIPELINE_REVISION` went to 2 for it, so M24 counts the change.
- **`write_park` returns the row's generation.** A card announced from the record carries the token the row will accept.

### Tasks 17.5–17.6: the registry, and writes that can be finished (2026-10-01)

- **`Registry.execute` is the only way `act` writes.** `execute_create_event` is gone. A test scans `app/` for `create_event`, `delete_event` and any write on a Calendar `events()` resource, and allows only the registry, the calendar client, the smoke test and the probe.
- **The READ tool is registered but not run through the registry.** The conflict check calls `freebusy` itself: a read needs no approval and is not audited.
- **The approval rides in the graph state.** `await_approval` keeps the resume's action id and nonce, so a re-drive after a crash continues the same action. The nonce was already stored in Postgres.
- **The action is identified by its id, message and nonce alone.** The tool `act` asks for is checked only for a new action, as one of its checks ("arguments differ from the approval"). A begun or settled action is finished or answered from what it stored, whatever the current code builds.
- **A refusal that names no action of this message's** (no approval, or another decision's nonce) is audited and touches no row.
- **A `400` fails the action at once** ("the calendar refused the write"): another attempt would be refused the same way. A time without a zone is refused before anything starts ("a time without a zone"), as `execute_create_event` had done since M04.
- **Two fixed reasons were added** to the audit's list: "attempts exhausted" and "a time without a zone". "The calendar refused the write" is now used too.
- **Pause.** The worker applies nothing while paused, and the decisions job opens no session, so a Confirm stays parked where a Withdraw can reach it (D6, the worker's part, brought forward from 17.12). The registry's `PausedError` is the second layer, for a pause that lands mid-apply: only an `approved` action stops; one already executing completes. The exception is named `...Error`, as the repository's are.
- **Giving up never fails a write that may exist.**
  - After the last attempt failed with an action `executing`, the lookup waits 10 minutes: Google can finish an insert after the call timed out on this side.
  - A `done` action answers from what it stored; a `dry_run` one settles as skipped, as `act` would have.
  - A `404` is taken as "no such event" only once the calendar itself has been read; otherwise the lookup counts as failed.
  - When Google cannot be asked, or the clean settle fails with a write begun, the decision stays open, asks again hourly, and `write_unconfirmed` is audited once. The alert and `/health`'s count move to 17.11.
- **Accepted as they are:** an attempt is audited when it ends, not when it starts, and a lookup that keeps failing retries hourly without limit, as D3 says, until the owner acts on the alert.
- **The probe** refuses `primary`. It deletes its event again whatever the checks find, including an insert whose reply was lost and one Google should have refused.
- **Tests.** The fake calendars count every insert asked for, so a blind second insert would show. One test reads the action from a second connection while Google is being called, to see that the start was committed first. A fresh-context review of 17.5–17.6 found no path that writes under `DRY_RUN` or skips a check; its other findings are folded in above.
- **Left for 17.7:** an M16 Confirm is still resumed without an approval. The registry refuses it, and the message is FAILED ("approval does not match"); 17.7 returns it to the owner instead. Refusing an `approved` action whenever its decision settles lands there too.
- **Left until after the M20 merge:** the purge's clearing of `outbound_actions.request` after 7 days (D8). The M20 build edits the purge and the scheduler's log line at the same time.

### Task 17.7: checks before a Confirm, and legacy proposals (2026-10-01)

- **Two ways back, by what went wrong.** A mode that differs expires the proposal: the decision settles as `no_effect`, and a sweep recorded in the same transaction ends the thread on the worker's next pass. A hash that differs, or a Confirm M16 left with no approval, returns the proposal to the owner as it is now.
- **A sweep can say why.** `decide()` takes a `reason` for a sweep only, and only one of the audit log's fixed phrases. The worker hands it to the graph, `reject` writes it to the ledger instead of M15's "swept: observe mode ended", and the purge keeps every fixed phrase. M24 therefore never counts a mode change as observe mode ending.
- **Every return to the owner is a new generation.** The park step's upsert raises it whenever a deciding row goes back to pending: a re-park after an edit, a resync, a give-up and the returns above. The give-up's return now also announces the card, since the old one's Confirm dies on its generation.
- **No action outlives its decision.** `_close` refuses an action still `approved` in the same transaction, and audits it, with a fixed reason for the path: "attempts exhausted", "the proposal changed", "approve again", "made under another mode", or "settled without running" for the rest. A reason a sweep was recorded with is kept when it settles.
- **Reconciliation** binds a pending proposal parked before M17 only while it is still pending at the revision read, and only when its payload can run; one without times stays "not ready". It also expires pending proposals made under the other mode that still await the owner, after closing those whose message is already final, so turning `DRY_RUN` off and restarting ends the old ones at boot. A thread it records only to expire is never announced.
- **Only the scheduler binds and expires.** Both passes run under production's `DRY_RUN`, calendar and key. A command-line `approve --reconcile`, run under its own, does neither: it could otherwise expire every live proposal, or bind one with a hash production would not accept.
- **Nothing from the other mode is shown again.** Every way back to the owner -- a changed hash, an M16 Confirm, a give-up, a resync -- expires the proposal instead when its payload was made under the other `DRY_RUN`. An expiry's sweep and its audit row are one transaction, audited once with the decision's id; a sweep that cannot be recorded rolls the settle back, so the decision is tried again.
- **Accepted:** an Edit queued under the old mode re-parks under the new one. The edit re-extracts in the running process, so the new revision is made under its mode.
- **A fresh-context review** of 17.7 found one test the change had broken (the 17.6 refusal test, now D3's "mode change after a resume", held at `act` by a Pause), the command-line expiry above, and smaller gaps, all folded in. Tests now cover a return whose only change is the generation, a changed `HASH_VERSION`, the reconciliation's own re-check of the row, and a failed settle refusing its approval.

### Tasks 17.8–17.9: the recipient rule, and Allow on the card (2026-10-01)

- **The thread is read for the rule alone.** `GmailClient.thread_headers` asks for `From`, `To`, `Cc` and `Authentication-Results`, with a field mask of labels and headers, so no snippet is ever received. A thread Gmail no longer has reads as empty: every guest is then outside.
- **Only Gmail's own topmost verdict counts.** A sender is a participant when the first `Authentication-Results` header is `mx.google.com`'s and its `dmarc=pass` names the `From` address's domain as `header.from`. A header written lower down by the sender is never read.
- **Its own comparator.** `guest_key` ignores dots and `+tags` for Gmail only; M15's `normalise_address` strips `+tags` everywhere, which D4 forbids. The web app's `guestKey` mirrors it, and both test files pin the same cases.
- **Outside guests are computed at park; contacts are applied where they count.** `detect_conflicts` stores the guests not in the thread. The card, `decide()`, the worker and the registry each subtract the contacts confirmed at that moment, so an Allow counts at once.
- **Before a Confirm on an invite,** the worker reads the thread and the contacts again. An outsider sends the proposal back with them marked. A Gmail error holds the Confirm for ten minutes at a time, without costing an attempt, for up to an hour after the owner confirmed; after that every guest counts as outside. The registry repeats the check before the action starts, through the same function, never the graph state. A Gmail error there fails the attempt: the worker read the thread moments before, so this is rare.
- **Contacts** are stored as guest keys, and audited with a keyed hash of the address. They are allowed through `POST /api/contacts` (204, 422 for anything that is not one address, 503 without `FERNET_KEY`) or `approve --allow`, and removed only with `python -m app.jobs.contacts --remove`.
- **Telegram** marks outside guests on the card, and answers a refused Confirm with a pointer to the web app.
- **The web card** lists each outside guest not yet allowed, with Allow: a server action that checks the owner, then calls Fly. Confirm stays on the card, and Fly's refusal while a guest is outside is shown in words.
- **Accepted:** reconciliation does not compute outside guests for invites parked before 17.8. Such a card shows none, and the worker's check before the Confirm finds any outsider and sends the card back marked.
- **A fresh-context review** found that the worker's and the registry's checks read the wrong thread: the ledger's `thread_id` holds the message id, as poll writes it. Both now ask Gmail which thread the message is filed in. The tests' fake threads now have ids of their own, so the mistake would show. The review's other findings are folded in too:
  - `Authentication-Results` is parsed rather than split. Quoted strings and comments are skipped, and a header with two DMARC results is refused, so a sender cannot write a pass of their own.
  - Each `To` and `Cc` value is read on its own, so one value the parser cannot read drops only itself.
  - Guests the owner has all allowed need no read of Gmail. After an hour of Gmail errors, allowed contacts still count.
  - A Gmail error at execution holds the Confirm like the first check (`HeldError`), and the registry asks Gmail before it locks the row, never during. A write already begun never waits on Gmail.
  - An out-of-date card is answered "stale" before it is told about guests.
  - Python and the web app trim and accept the same characters: ASCII space and a byte-order mark trimmed, printable ASCII only.
  - The card's layout key counts its outside guests, so the buttons an Allow moves are held for a moment.
  - The Telegram card says to allow guests in the web app.
- **Left for the owner's end tests:** Allow, then Confirm, in the browser against a local API (17.9's acceptance). It needs a signed-in owner session and Fly running locally.

### Task 17.10: one metered path for model calls (2026-10-02)

- **`app/policy/models.py` builds every model client,** and a test fails if `genai.Client(` or the Gateway's URL appears anywhere else. The Gateway's evaluator moved there from `app/extraction/evaluation.py`, which keeps the triage logic.
- **Three operations, not two:** `models.generate_content`, `models.embed_content`, and the Gateway's `evaluate`, which has a call shape of its own. Nothing else is offered: no stream, no async client, no cache.
- **One gate per session.** The graph session builds one gate on its connection, and the pipeline, the reviewer and search share it. A command-line tool (the eval harness, the model probe, the demos) gets one on a connection of its own, and so needs a database: the eval harness's spend is recorded like any other. Ingestion records its spend on a connection of its own too, committed call by call, so a run that fails half way has still recorded what it spent.
- **The message a call serves** comes from a context variable the session sets around each run. LangGraph carries it into the nodes, and a test checks this.
- **Prompts are bounded at the wrapper.** The longest text part is cut first, with a note, until the call's text is under 24,000 characters. Tool calls and their answers are left whole.
- **Embeddings** are priced at four characters a token and marked estimates. `gemini-embedding-001` is no longer listed on the pricing page, so it is priced at Gemini Embedding 2's $0.20 per million tokens, as M15's measurement priced it.
- **The settings** `MONTHLY_BUDGET_USD` (40) and `MESSAGE_CEILING_USD` (0.50) are new. What happens when the gate refuses -- poll stopping, a message released or skipped as too costly, the alerts and `/health` -- is 17.11's.
- **Search** passes the gate's refusals up instead of degrading to keyword search.
- **The review (2026-10-02)** found the cut aimed at the wrong place. Production sends each prompt as one string, with an owner's correction or the proposal under review after the email, and a cut from the end removed exactly those: an Edit could silently do nothing, and a sender could pad an email to switch the reviewer off. Now:
  - callers cut first, where they know what can give way: the email's body, never what follows it (`prompts.user_content(..., room=)`). A short email's prompt is byte for byte what it was, so the frozen baseline still stands;
  - the wrapper's cut stays as a backstop. It counts tool calls and their answers, never touches a model's own turn (it carries the thought signature), and cuts the user's text from the middle, keeping both ends;
  - the Gateway's state is held to the same bound: 23,000 characters, plus the question.
- **Also from the review:**
  - a call that reports no usage is estimated from characters and flagged, rather than recorded as free;
  - a report of more cached tokens than prompt tokens no longer stops the row being written;
  - search with no gate passed meters on a connection of its own, not on the caller's, where a rollback would erase the rows;
  - the eval harness and the demo share one gate per run, and a command-line gate waits at most 10 seconds for the database;
  - the wrapper's raw clients are private, and the call-site test now also catches other spellings of a Gemini client, the Gemini API's host, and any reach for the raw client;
  - the model probe lists models without the metered client, which offers no listing.
- **Accepted:** if the database fails just after a billed embedding, search falls back to keyword search and that row is lost. The session's next write fails anyway, and one embedding costs a fraction of a cent. The review's finding that poll and the worker dead-lettered a refusal was 17.11's work, and is done there.

### Task 17.11: where work stops, and what shows (2026-10-02)

- **Poll** asks the gate before each claim. At the cap it stops claiming, and the tick still records as successful. A message stopped mid-run by the cap or by an unpriced model is released: its checkpoint is deleted and its ledger row removed, so the feed offers it again, from the start. A message over its ceiling is skipped as "too costly to read", and audited.
- **The worker** leaves an Edit queued at the cap, costing no attempt (`waiting`); a Confirm or a Cancel still applies. A refusal in the middle of applying a decision holds it the same way. A message over its ceiling fails its Edit at once.
- **Ingestion** asks before each batch. A refusal mid-batch ends the run quietly: the next run's dedupe skips what this one embedded.
- **The watch** (`app/jobs/watch.py`) runs at start and every five minutes:
  - it writes the budget's state to `control` when it changes, and audits the change;
  - it sends `budget_warning` from 80% of the cap, and `budget_exhausted` once new work has stopped (the month's spend plus the $0.10 reserve reaches the cap), each once per month and cap value. A raised cap re-arms them. A budget that goes straight past 80% sends only the second;
  - it sends `write_unconfirmed` once per decision whose calendar write could not be confirmed;
  - its connection commits as it goes, so an alert's record is never rolled back into a resend. A failing watch is recorded in `job_runs` at most every half hour.
- **One sender** for every alert sent once: `send_alerts` and `Alert` in `app/channel/alerts.py`, renamed from the token alerts' `send_token_alerts` and `TokenAlert`.
- **`/health`**, with the bearer, shows `budget` (state, the month's spend, the cap), `unconfirmed_writes` and `unpriced_models`. A spent budget stays 200. A model in use with no price is a 503 ("a model in use has no price"): the classifier's and the extractor's always, the reviewer's with the reviewer on, the embedding model with search or ingestion on.
- **Held decisions are not stuck:** every decision while paused, an Edit while the state is `exhausted`, and a decision whose write could not be confirmed, which is counted apart. The decisions job opens no session for an Edit the cap holds.
- **Push tags:** the two budget alerts share `budget`, since the cap reached supersedes the warning; an unconfirmed write has `write`.
- **Left for 17.12:** the card saying an Edit is waiting, and the header's "Spending cap reached", come with the web app's pause work.

### Task 17.12: Pause, Resume and Withdraw (2026-10-02)

- **The switches** (`app/policy/control.py`): one function, `switch(paused=...)`, writes `paused`, `changed_at` and `changed_via`, and audits `paused` or `resumed`, only when something changes. Not `resume()`: `.resume(` stays the graph's, which only the worker may call (`tests/test_one_resumer.py`). From the web app (`POST /api/pause`, `POST /api/resume`), or the command line (`python -m app.jobs.control pause|resume|status`, and `.\tasks.ps1 pause` / `resume` locally). Resume wakes the worker.
- **While paused:**
  - poll claims nothing, checked before the pass and before each claim, and the tick still records as successful;
  - ingestion claims no batch, and does not ask Gmail;
  - the worker applies nothing but withdraw requests, and the decisions job opens a session only for those;
  - the registry's `PausedError`, from 17.6, stops an action that slipped past the worker's look.
- **Withdraw** names the decision, not the proposal, so a request from an old card cannot reach a decision made since; an operator's sweep is never the owner's to withdraw (`POST /api/decisions/withdraw`: 202 requested, 409 settled, 404 no such decision). It only records `withdraw_requested_at`; the worker carries it out:
  - requests come first, even while paused, and even while the decision waits to retry;
  - withdrawn only if nothing has run: the thread still parked at the decision's revision, and its action, if any, still `approved`. The decision settles as `no_effect` ("withdrawn by the owner"), the action is refused, and the proposal returns at the next generation, so the old card's Confirm is stale; audited `decision_withdrawn`. Nobody is notified: the owner asked;
  - otherwise declined ("already being applied"): the request is cleared, `withdraw_declined` audited, and the decision goes on as it was, with no attempt spent.
- **`/health`** shows `paused` with the bearer. Paused is not an outage, and held decisions are not stuck (17.11).
- **The web app:** Pause / Resume in the header on every page, with banners while paused, at 80% of the cap and at the cap. A queued card shows Withdraw, then "Withdrawing…", or "Already being applied" if the worker declined. A held decision says why it waits: paused, or an Edit at the cap. These were 17.11's leftovers too.
- **The header reads the switches on every page,** so a failed read renders the page without them rather than failing it; the redirect to sign in still passes through.
- **Not done here:** the browser check against a local API waits for the owner's end tests, with the rest of the web checks.

### Task 17.13: the Activity page (2026-10-02)

- **`/activity`** lists the latest 100 audit entries, newest first, read as `web_reader`, with each entry's proposal title while the proposal keeps it. The page is in the header's links.
- **Every kind has words of its own** (`dashboard/src/lib/activity.ts`). A test reads `app/policy/audit.py`'s `Kind` and fails if the two lists differ, so a kind added on Fly must be given words here. A kind the page does not yet know is shown as it is rather than failing the page.
- **No content:** the log holds none, and the page adds only the proposal's title, as the timeline does. The browser check waits for the owner's end tests.

### Reviews of 17.11 (2026-10-02)

Two adversarial reviews of `c7aee25`, one on where work stops and one on what shows, found 26 issues between them, several shared. Fixed:
- **A model with no price stops new work, as the cap does.** Poll had claimed, run and released the same message every tick, paying each time for the calls before the refusal, until the message's ceiling skipped it for good; an Edit was refused every fifteen seconds. The gate now knows the models in use, and `allows_new_work` waits while any has no price. The budget's state stays where spending is, and `/health` names the model.
- **A held Edit is pushed back, not left due.** The worker checks after taking the lease, and only when the Edit would run its re-extraction, then pushes it back five minutes, costing no attempt. Held Edits no longer fill every pass ahead of a Confirm, and no session opens every fifteen seconds for them. The decisions job no longer reads `budget_state`: the live gate is the one source of truth.
- **The stuck clock runs from when a decision became due,** not from when it was made, so a decision no longer counts as stuck the moment a hold ends. Resume makes what the pause held due afresh. An unconfirmed write is asked about hourly, so it is never stuck; a decision that keeps failing still is, within the hour. `/health`'s phrase is now "a decision has been due for over an hour".
- **The message ceiling is checked before an Edit runs.** A proposal still parked goes back to the owner ("too costly to read"), who can still Confirm or Cancel it. One already past its interrupt is SKIPPED, as poll records a message it could not afford. The audit row is written with the settle.
- **A write known to exist whose settle fails** is no longer alerted as "could not be confirmed": it is left open as an error, and the stuck clock reports it.
- **The watch** reads the clock once, so a look across midnight on the 1st files its alert under the right month. The alert's subject carries the cap to the cent. Each half is tried even if the other fails, and an alert no channel delivers is offered again hourly rather than every five minutes. The unconfirmed count moved from the fifteen-second decisions job to the watch, off the audit log's hot path, and `/health` shows when the watch last read the budget.
- **Smaller:**
  - poll's skip and its audit are written together, and a pass that stops short says why (`PollResult.held`);
  - the token check records its alerts on a connection that commits as it goes;
  - `MONTHLY_BUDGET_USD` and `MESSAGE_CEILING_USD` must be real, non-negative amounts;
  - the mail recall uses the shared sender;
  - ingestion asks Gmail nothing when stopped before it starts, and a stopped run is recorded as failed, saying so.
- **The purge clears a stored calendar request a week after its write began** (D8), the last item deferred from 17.6.

Accepted, and documented in the runbook:
- the message ceiling counts every run's spend on a message, including a run the cap stopped: it bounds what one message can cost in all;
- mail that waits more than seven days, at the cap or paused, is skipped as too old (M20's rule), so a long cap is better raised than waited out;
- "cap reached" fires when new work stops, $0.10 short of the cap, and a cap of $0.50 or less can reach it before the 80% warning;
- a graph run the gate stopped is recorded as failed in `runs`, whose schema allows no other word;
- the budget's state is sampled every five minutes, so its audit rows can lag the true stop by as much;
- a phone running the old service worker shows a new alert as a proposal until it loads the app once.
