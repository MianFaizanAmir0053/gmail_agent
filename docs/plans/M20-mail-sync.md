# M20 · Mail sync

**Est.** 4 days · **Depends on** M15 · **Blocks** M21 (loose ends)

Plan and decisions: [`ASSISTANT-PLAN.md`](../../ASSISTANT-PLAN.md).

## Goal

The agent sees every relevant message in the mailbox, inbound and sent, read
or unread, as it arrives. It reads Gmail's history from a stored cursor,
catches up on its own when the cursor expires, and checks its own recall, both
of the sync and of the feed. The meeting pipeline reads new Primary mail from
this feed instead of the newest page of unread mail. What the sync stores is
metadata only, until M18 can strip one-time codes.

## Why this comes next

The code as of `4c7df5b` (mapped on 2026-10-01):
- **The poller misses mail.** It reads one page of 10 unread messages per tick (`app/google/gmail.py`, `list_unread`), so a message the owner opens on the phone before the next tick is never seen. Busy hours overflow the page.
- **It never sees sent mail.** Promises the owner makes, and replies that close a loose end, are invisible. M21 needs both.
- **The cursor is decorative.** `poll_once` stores the profile's history id after each pass, and nothing reads it. `history.list` is never called, and an expired history id is handled nowhere.
- **It reads every tab.** `is:unread` has no label filter, so unread Promotions are classified too, at model cost.
- **The schema is Gmail-shaped,** with no room for a second source.

**Gmail's quota sets the pace** (usage-limits page, checked 2026-10-01):
6,000 units per user per minute, shared by everything that touches the
mailbox. `messages.get` costs 20 units, `messages.list` 5, `history.list` 2
and `threads.get` 40. History records carry only message and thread ids, with
label changes as additions and removals, so every new message costs one fetch.
Gmail's `after:` and `before:` read a date as Pacific midnight, so every
window here is given in epoch seconds.

## Decisions (1 Oct 2026)

- **The meeting pipeline reads new Primary mail, read or not.** Every new inbound message in Primary, even one the owner opened first. Promotions, Social, Updates and Forums are skipped, and so are bulk senders.
- **Metadata is kept for 180 days** (D7).
- **The age limit is 7 days** (D4): mail first reached later than that is recorded as skipped, not processed.
- **Testing happens at the end**, with the owner's other end-to-end tests.

The owner approved this spec on 2026-10-01, after two adversarial review
rounds. The third round's findings were folded in the same day (see Review).

**A consequence to know.** Feeding read mail as well as unread means
one-time-code and password-reset mail in Primary now reaches the classifier
(the paid tier, which does not train on it), where the old unread-only poller
mostly missed it. M20 keeps the model's reasoning about such mail out of the
ledger (D4). M18 strips the codes before anything is stored.

## Scope

**In:**
- a Gmail message table, filled from metadata;
- a cursor; incremental sync through `history.list`; catching up when the cursor expires;
- a 90-day backfill, and a fetch queue, run in the background;
- the meeting pipeline's feed, and the switch from the old poller;
- recovering claims stranded by a restart;
- daily recall checks, of the sync and of the feed;
- sync liveness in `/health`;
- retention for the new records.

**Out:**
- bodies, subjects and snippets: they can hold one-time codes, so they wait for M18;
- Promotions and Social mail: not stored;
- extracting obligations, and the ledger view (M21);
- other sources (M23 adds a table each);
- retrying FAILED messages in general (see Open questions).

---

## Design

### D1. The table

`gmail_messages`, one row per message, following the plan's rule of one table
per source (Outlook gets its own in M23):

| Column | Meaning |
|---|---|
| `account`, `message_id` | Primary key: the owner's address, the Gmail message id |
| `thread_id` | Gmail's thread id |
| `internal_at` | When Gmail received or sent it (`internalDate`) |
| `label_ids` | As Gmail reports them, kept current from history |
| `direction` | `out` when labelled `SENT` or `SCHEDULED` (a scheduled send may carry no `SENT` until it goes); otherwise `in` |
| `to_self` | Sent by the owner to the owner (any of `OWNER_EMAIL` and `OWNER_ALIASES` in `To`) |
| `category` | `primary`, `updates` or `forums`, from the labels; `primary` when no category label is present |
| `from_addr`, `to_addrs`, `cc_addrs` | Lower-cased addresses, as written |
| `has_list_unsubscribe`, `precedence`, `auto_submitted` | The bulk signals: a flag, not the header's URL |
| `arrived_via` | `history`, `switch_over`, `catch_up`, `recall`, `queue` or `backfill`: how the row first arrived, for diagnosis only |
| `first_seen_at`, `updated_at` | Timing |
| `gone_at` | When history reported the message deleted, or a fetch answered `404` |

**What is stored.** Messages labelled `SENT`, and every other message except
Promotions and Social: Primary, Updates (where deadlines and money often
arrive, for M21) and Forums. Drafts, chats, spam and trash are not stored. A
label change can bring a message in, for example one moved out of
Promotions; the fetch queue (D3) fetches it.

**Recomputed on every label change:** `direction` and `category`. `to_self`
comes from the headers, so moving a conversation to the Inbox, which labels
the owner's own replies `INBOX` too, changes nothing.

**No content.** No subject, body or snippet. Every metadata fetch asks for an
explicit field mask (`id,threadId,labelIds,internalDate,payload/headers`) and
exactly six headers: `From`, `To`, `Cc`, `List-Unsubscribe`, `Auto-Submitted`
and `Precedence`. That is a new constant, separate from M15's, which includes
`Subject`. A snippet is never even received. M18 adds content once it can
strip one-time codes.

**Not the recipient rule's source.** M17 reads thread participants from Gmail
itself, when it parks, because they must be current and complete. M21 reads
Gmail too for threads that cross the backfill's start or the purge.

### D2. The records

`gmail_cursors`, one row per account:
- `history_id`: the cursor;
- `feed_from`: the switch-over instant (D4);
- `switch_over_at`: when the switch-over listing ran, so it runs once;
- `caught_up_at`: the last pass that reached the end of history, which liveness reads;
- `backfill_until`: how far back the backfill has reached;
- `gap_from`, `gap_until`, `gap_progress`: an unfinished catch-up (D3), kept until it completes;
- `catch_ups`: how many there have been.

`gmail_fetch_queue`: messages to fetch outside the incremental pass (D3):
`message_id` (primary key), `reason`, `queued_at`, `strikes`, `status`
(`queued` or `unreadable`), and `failed_at`, when it last failed on its own.

Every write to the cursor is conditional on the value it replaces. Every sync
run holds a Postgres advisory lock, so a CLI run and the scheduled job never
interleave. A scheduled run that finds the lock taken skips its turn.

The single-row `sync_state` that poll writes stays in place, because
migrations are additive. The switch-over reads it (D3).

### D3. Sync

`app/mail/sync.py`, run by the scheduler every 2 minutes (`max_instances=1`).
Each run is bounded to 60 seconds and leaves the rest to the next tick.

**The first run starts where the old poller stopped.** The old poller stored
the profile's history id after each full pass (`sync_state.last_history_id`).
The first run uses it as the cursor, and sets `feed_from` to that pass's time
(`sync_state.updated_at`). Its first incremental pass then replays everything
since. If that id is null (a fresh database), the first run takes the profile's
current id, and `feed_from` is now. If it has expired, the first run is a
catch-up from `sync_state.updated_at`.

**Then, once:** the switch-over listing. Unread mail from the last 7 days that
is not in the other four categories (`is:unread -category:promotions
-category:social -category:updates -category:forums -in:chats`, as an epoch
window) is fetched and stored. That is what the old poller would still have
reached, had its page of ten not overflowed. `switch_over_at` records it. The
window runs to the moment of the listing, not to the run's start: on a fresh
database, mail accepted between the run's start (`feed_from`) and the profile
read that set the cursor is in no history page after the cursor, and newer
than anything the backfill stores.

**Incremental passes.** `history.list` from the cursor, with types
`messageAdded`, `messageDeleted`, `labelAdded` and `labelRemoved`, page by
page. Gmail returns records after the cursor, not including it:
- **added:** a message added and deleted within the same pass is never fetched: drafts are replaced constantly. Every other one is fetched and stored if D1 keeps it. A fetch answering `404` means the message is already gone: it is marked gone if stored, and passed over;
- **labels changed:** a stored row's labels take the record's additions and removals, with no fetch, and `direction` and `category` are recomputed. A message not stored that the change could bring in (out of `SPAM`, `TRASH`, Promotions or Social) goes to the fetch queue;
- **deleted:** `gone_at` is set.

After each page commits, the cursor moves to the last record handled. After the
last page, it moves to the response's `historyId`, and `caught_up_at` is set.
A crash or a deploy costs at most one page.

**Errors.**
- Only a failure of `history.list` itself stops a pass. The cursor stays at the last record handled, and the next tick retries.
- A fetch that fails for one message, other than with a `404`, sends that message to the fetch queue and the pass moves on. So one bad message never holds up the cursor. A row the database refuses counts the same: each row is stored in a savepoint of its own, so it never rolls back the rest of its page. NUL characters, which Postgres text cannot hold, are dropped from the headers first.
- An outage (5xx, 429 or the network) stops the pass without blaming any message. A fetch that fails that way is checked first with one cheap call, the profile (1 unit): if Gmail answers it, the failure is the message's own, and it is queued and struck like any other. Otherwise one message that answered 500 every time would stop every pass on it, and with it the cursor, the queue, the backfill and any catch-up. Only if the profile fails too is it an outage.

**The fetch queue** is worked in the background, after the incremental pass,
within the backfill's share of the quota. A fetch that shows a message older
than the backfill reaches (90 days before `feed_from`, not before today)
drops it. A per-message failure counts a strike; five strikes
mark it `unreadable`, counted in `/health`. Outages count no strikes. Entries
that never failed come first; those that did follow, fewest strikes and
longest ago first, so a head that fails every time cannot starve the rest.

**Catching up.** `history.list` answers `404` when Gmail no longer keeps the
cursor, typically after about a week. Only that `404` starts a catch-up. In
one transaction:
1. it reads the profile's current `historyId`;
2. it records the gap: `gap_from` is `caught_up_at` less an hour, and `gap_until` is now;
3. it takes the profile's id as the new cursor, so live mail flows again at the next pass.

The gap is then worked off in the background, newest first, recording
`gap_progress`; a restart resumes it. Two parts:
- every message Gmail lists in the gap is fetched and stored, refreshing the labels of rows already stored. The listing uses D1's set: `-category:promotions -category:social -in:chats -in:drafts`, in epoch seconds;
- every stored row from the last 7 days is fetched again by id. That is where mail trashed or moved to spam during the outage is seen, before the feed could process it.

Nothing is marked gone for not being listed: archived or recategorised mail is
not gone. Only a `404` or `messageDeleted` marks a row gone. `gap_*` is
cleared when both parts are done.

**The backfill** runs last, and only spends quota the rest leaves. It lists one
day at a time, newest first, from `feed_from` back to 90 days (D1's set, epoch
seconds), stores what D1 keeps, and records `backfill_until`, so a restart
resumes rather than starting over. Its windows end at `feed_from`, so nothing
it stores is newer than the switch-over. A busy mailbox takes hours, which is
fine: nothing waits for it.

**Quota.** One pacer, shared by every Gmail call in the process, counts units
by method. That covers the sync, the pipeline's fetches and M17's thread
reads. The sync spends at most 2,000 units a minute, a third of the per-user
quota, which leaves the rest to the pipeline and M17. M15's `measure` CLI runs
locally in its own process; the runbook says not to run it during a backfill.

**The pipeline's fetch** retries 429 and 5xx for at most 30 seconds in all, so
a message's run stays well inside `kill_timeout` (120 s). A rate limit
therefore rarely ends a message in FAILED. The 30 seconds bound the whole
call: the pacer's wait, every attempt -- each attempt's sockets get only the
time left -- and jittered pauses. Every retry is charged to the pacer.

### D4. The meeting pipeline's feed

**The rule.** Poll's candidates are `gmail_messages` rows that meet all of
these:
- `direction` `in`, `category` `primary`, not `to_self`. Strictly inbound: a forward to yourself would duplicate the original's proposal;
- not labelled `SPAM` or `TRASH`, and not gone;
- not bulk:
  - `precedence` is not `bulk` or `junk`;
  - `auto_submitted` is absent or `no`;
  - for a message with no category label, `has_list_unsubscribe` is false;
- `internal_at` at or after `feed_from` less an hour;
- no ledger row yet;
- a sync pass reached the end of history in the last 30 minutes (`caught_up_at`). Labels are only as current as the sync: hours into a failing sync, a message the owner trashed meanwhile would still look like Inbox. While the sync is behind, the feed offers nothing, and records nothing as too old.

They are taken oldest first, up to `POLL_BATCH_SIZE`. Everything after that is
as it is now: `claim`, `start`, park. The feed decides by time, not by how a
row arrived. Mail the backfill stored in the margin hour is fed, and mail
older than `feed_from` less an hour never is.

**Mailing lists in Primary stay in.** Team mail from Google Groups carries
`List-Unsubscribe` and `Precedence: list`, and meeting requests arrive that
way. Where Gmail has categorised the mailbox, promotional bulk mail has
already left Primary. Where it has not (no category label), `List-Unsubscribe`
is the bulk marker instead. M15's measurement keeps its own wider rule.

**Seven days, then recorded.** A candidate is claimed only while its
`internal_at` is within the last 7 days. Older ones are never processed, and
never silently dropped. Poll gives each one a ledger row, `SKIPPED` ("too old
when reached"), without a model call. The phrase joins the purge's fixed
reasons, so it is kept. That covers:
- mail found by a catch-up after a long outage;
- mail restored from Trash;
- mail held by M17's Pause or spending cap for over a week.

`/health` counts them, and `--status` lists the latest.

**A message gone before its turn.** The pipeline's fetch raises `MessageGone`
for a `404`, which its retry policy does not retry. Poll records the message
as `SKIPPED` ("no longer in the mailbox"), a fixed reason too. The graph's
edges do not change. The full message carries its labels at no extra cost,
so the same happens to a message now labelled `TRASH` or `SPAM`: the owner
binned it after the sync last saw it.

**The model's reasoning stays out of the ledger.** When classification finds
no meeting, the ledger now records the fixed phrase "not a meeting" instead of
the model's reasoning, which can quote the email. Until M18, that matters more
now that read mail is fed too.

**Until the first sync run,** poll keeps calling `list_unread`, as now.

**Claims stranded by a restart.** At boot, no run is in flight. For every
`claimed` row:
- a thread that parked is left to reconciliation, which records it (M16 D3);
- a thread that did not park, and is younger than the 7 days, is released: its ledger row and checkpoint are deleted, so the feed offers it again;
- an older one is marked FAILED, as now.

Before M20, a claim younger than an hour was left `claimed` for ever.

The sync and the poll stay separate jobs: a slow model call never delays
seeing mail, and a failing sync shows as its own failing job.

### D5. Recall, of the sync and of the feed

A daily job looks at the window from 26 hours ago to 2 hours ago, which keeps
it clear of the sync's own timing:
- **Sync recall.** The ids Gmail lists for the window, using D1's set, are checked against `gmail_messages`. Missing ones are fetched and stored, so a miss is repaired as well as reported. Repaired inbound mail is fed if the rule takes it.
- **Feed recall.** A stored row that has met the feed's rule for over an hour, with no ledger row, means the feed stalled. Hours when M17 was paused, or the spending cap had stopped work, are left out; the audit log records both. A `SKIPPED` "too old" record for mail under a day old means the age rule is wrong.
- **Category agreement, both ways.** Ids Gmail lists as not in the four other categories must be `primary` here. Ids it lists in Updates or Forums must not be.

Each result goes to `job_runs`. Any shortfall sends one alert a day ("Mail sync
missed messages", or "The mail feed has stalled").

### D6. Liveness

`/health` stays free of database reads, as M15 built it. The sync job records
in the process's liveness record when a pass last reached the end of history.
`/health` returns 503 when that is more than three intervals old, with the
clock starting one interval after boot, as poll's does. A run that only made
headway through a backlog does not count. Without this, a dead or lagging sync
with an empty feed would leave `/health` green while the agent saw nothing.

With the bearer secret, `/health` also shows these, kept in memory and
refreshed by the jobs that compute them:
- the cursor's age;
- the backfill's, the queue's and any catch-up's progress;
- unreadable and too-old counts;
- the table's row count;
- the last recall.

### D7. Retention

The purge deletes `gmail_messages` rows whose `internal_at` is over 180 days
old (the owner's decision), and gone rows a week after `gone_at`. Ledger rows
are untouched. A row is about 0.5 KB with its indexes. Primary, Updates and
Forums at 500 messages a day for 180 days come to about 45 MB, inside
Supabase Free's 500 MB; M15 measures the real volume, and `/health` shows the
count.

### D8. Records and tools

Migration `011_mail_sync.sql` (additive, re-runnable): `gmail_messages` with
its indexes (thread; `internal_at`; the feed's rule), `gmail_cursors` and
`gmail_fetch_queue`. `web_reader` gets nothing; the web app shows none of it
in M20.

`python -m app.mail.sync`, run on the instance through `fly ssh console`. Every
command takes the same advisory lock as the scheduled job.

| Option | What it does |
|---|---|
| `--once` | Runs one pass |
| `--status` | The cursor, `feed_from`, progress of the backfill, queue and any catch-up, counts by direction, category and `arrived_via`, the latest too-old records, and the last recalls |
| `--show <message id>` | One row's metadata, without content |
| `--catch-up` | Forces a catch-up, as if the cursor had expired |
| `--check-feed` | The switch-over and backfill checks in the exit criterion |

---

## Deliverables

- **`app/mail/`:** `sync`, `messages`, `feed`, `recall`, `quota`.
- **The Gmail client:** history pages, field-masked metadata, epoch-window listing, bounded retries, and the shared pacer.
- **The pipeline:**
  - poll reading the feed;
  - the switch-over;
  - the scheduler's `mail_sync` and `mail_recall` jobs;
  - `MessageGone`, and the too-old and gone records;
  - "not a meeting" in place of the model's reasoning;
  - stranded claims released at boot.
- **Health and retention:** `/health` fields and the 503; the purge extension.
- **Migration** `011_mail_sync.sql`.
- **Docs:** `docs/DEPLOY.md` (the first runs, the quota, the CLI) and the README.

## Commands

```powershell
.\tasks.ps1 check
uv run python -m app.mail.sync --once      # locally, against the dev mailbox
uv run --env-file .env.test pytest -m integration -o addopts="" -q -p no:cacheprovider
```

## Testing

- **Sync, against a fake Gmail service:**
  - the first run takes the old poller's stored id and time, and its replay is fed;
  - the switch-over listing runs once;
  - an incremental pass stores added messages, applies label changes without fetching, queues unstored messages a label change could bring in, and marks deletions;
  - a message added and deleted in one pass is never fetched; drafts, chats, spam, trash, Promotions and Social are never stored;
  - a fetch `404` marks the message gone; a per-message failure queues it and the pass goes on; an outage stops the pass and charges no strike; five strikes mark a queued message unreadable;
  - a history `404` reads the profile's id first, records the gap, moves the cursor, and works the gap off, resuming after a restart;
  - a catch-up re-fetches the last 7 days' rows, so a message trashed during the gap is never fed;
  - a catch-up marks nothing gone for not being listed;
  - a crash mid-pass costs at most one page;
  - the backfill resumes from `backfill_until`, stops at 90 days, never stores mail newer than `feed_from`, and spends only spare quota;
  - every window is in epoch seconds;
  - the shared pacer holds the sync to 2,000 units a minute, counted by method;
  - a CLI run and the scheduled job never interleave.
- **Classification:**
  - `SENT` gives `out`;
  - mail from the owner to the owner is `to_self`, and moving it to the Inbox does not feed it;
  - categories map, no category is `primary`, and a label change recomputes them.
- **Feed:**
  - only strictly inbound, non-bulk Primary mail reaches poll, Google Groups mail included;
  - uncategorised mail with `List-Unsubscribe` does not;
  - nothing older than `feed_from` less an hour does, whichever way it arrived;
  - a message read before the tick is still processed; one with a ledger row is skipped;
  - mail older than 7 days gets a `SKIPPED` record, a fixed reason the purge keeps, and no model call;
  - before the first sync run, poll uses `list_unread`;
  - a `MessageGone` gives `SKIPPED`, not FAILED;
  - a classification skip records "not a meeting";
  - at boot, a stranded claim is recorded, released or failed as D4 says.
- **Recall:**
  - a missing id is stored and fed, and gives one alert;
  - a stalled feed raises its alert, but not during a pause or a stopped cap;
  - a too-old record for day-old mail raises it;
  - category disagreement in either direction is caught.
- **Liveness:** a sync that has not reached the end of history for three intervals gives 503, a backlog included; a fresh boot does not.
- **Content:** a test seeds a subject, a snippet and a body, and checks none is stored. The request carries the field mask and exactly the six headers.
- **Integration (Neon):** 011 applies and can be re-run; writes are idempotent.
- **End-to-end, by the owner at the end:** the exit criterion.

## Boundaries

- **Always:**
  - read-only Gmail access (`gmail.readonly`, as now);
  - metadata only, through a field mask;
  - the cursor stored only after the rows it covers, and the gap recorded before the cursor moves;
  - every window in epoch seconds;
  - the sync within its share of the quota.
- **Ask first:**
  - storing subjects, snippets or bodies (M18 decides how);
  - a wider OAuth scope;
  - any schema beyond `011`;
  - another retention period.
- **Never:**
  - modifying the mailbox;
  - feeding mail older than `feed_from` less an hour, or older than the age limit, to the meeting pipeline.

## Exit criterion

On the deployed stack, at the owner's end tests:
1. A message the owner sends shows as `out` within one sync interval (`--show`).
2. A Primary message opened on the phone before the next tick is still processed by the pipeline. A Promotions message is not stored.
3. `--catch-up` records a gap, works it off, and a message trashed during it is never fed.
4. `--check-feed`:
   - no message older than `feed_from` less an hour has a ledger row created after the switch-over;
   - every Primary message from the switch-over hour was processed, or recorded with a reason.
5. Sync recall, feed recall and category agreement are clean for seven consecutive days.

## Open questions

- **FAILED messages are never retried.** The ledger calls FAILED retryable, but poll skips any message with a ledger row. M20 removes the commonest cause it adds (rate limits) and records the cases it creates (gone, too old, stranded). Retrying the rest belongs with M21.

## Review

Three adversarial rounds, each by a reviewer with fresh context, on 2026-10-01.
- **The first** found the switch-over gap, the routine fetch `404`s from drafts, the quota cost of `messages.get`, and the feed rule's loss of team mail.
- **The second** moved the first cursor to the old poller's stored id, recorded the catch-up's gap before moving the cursor, and took liveness out of the database.
- **The third** made the feed decide by time rather than by how a row arrived, stopped marking rows gone for not being listed, put every window in epoch seconds, moved label-change fetches to a queue, widened storage to Updates and Forums for M21, bounded the pipeline's retries, released stranded claims, and kept the model's reasoning out of the ledger.
