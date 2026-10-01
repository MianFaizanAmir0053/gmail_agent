# M20 · Mail sync

**Est.** 4 days · **Depends on** M15 · **Blocks** M21 (loose ends)

Plan and decisions: [`ASSISTANT-PLAN.md`](../../ASSISTANT-PLAN.md).

## Goal

The agent sees every relevant message in the mailbox, inbound and sent, read
or unread, as it arrives. It reads Gmail's history from a stored cursor,
catches up on its own when the cursor expires, and checks its own recall,
both of the sync and of the feed. The meeting pipeline reads new Primary mail
from this feed instead of the newest page of unread mail. What is stored is
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

## Decisions (1 Oct 2026)

- **The meeting pipeline reads new Primary mail, read or not.** Every new inbound message in Primary, even one the owner opened first. Promotions, Social, Updates and Forums are skipped, and so are bulk senders.
- **Metadata is kept for 180 days** (D7).
- **The age limit is 7 days** (D4): mail first reached later than that is recorded as skipped, not processed.
- **Testing happens at the end**, with the owner's other end-to-end tests.

The owner approved this spec on 2026-10-01, after two adversarial review
rounds, with a third running.

## Scope

**In:**
- a Gmail message table, filled from metadata;
- a cursor; incremental sync through `history.list`; catching up when the cursor expires;
- a 90-day backfill, run in the background;
- the meeting pipeline's feed, and the switch from the old poller;
- daily recall checks, of the sync and of the feed;
- sync liveness in `/health`;
- retention for the new records.

**Out:**
- bodies, subjects and snippets: they can hold one-time codes, so they wait for M18;
- Promotions, Social, Updates and Forums mail: not stored;
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
| `direction` | `out` when labelled `SENT`; otherwise `in` |
| `to_self` | Sent by the owner to the owner (any of `OWNER_EMAIL` and `OWNER_ALIASES` in `To`) |
| `category` | `primary` for `CATEGORY_PERSONAL`, or for no category label |
| `from_addr`, `to_addrs`, `cc_addrs` | Lower-cased addresses, as written |
| `has_list_unsubscribe`, `precedence`, `auto_submitted` | The bulk signals: a flag, not the header's URL |
| `arrived_via` | `history`, `switch_over`, `catch_up`, `recall` or `backfill`: how the row first arrived |
| `first_seen_at`, `updated_at` | Timing |
| `gone_at` | When the message left the mailbox: history reported it deleted, a fetch answered `404`, or a catch-up no longer listed it |

**What is stored.** Messages labelled `SENT`, and messages in Primary or with
no category label. Drafts, chats, spam, trash and the other categories are
not stored. A label change can bring a message in, for example one moved from
Promotions to Primary within the last 90 days: it is fetched then.

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

### D2. The cursor

`gmail_cursors`, one row per account:
- `history_id`: the cursor;
- `synced_at`: the last successful pass;
- `backfill_until`: how far back the backfill has reached;
- `gap_from`, `gap_until`, `gap_progress`: an unfinished catch-up (D3), kept until it completes;
- `catch_ups`: how many there have been.

Every write to the cursor is conditional on the value it replaces. Every sync
run holds a Postgres advisory lock, so a CLI run and the scheduled job never
interleave.

### D3. Sync

`app/mail/sync.py`, run by the scheduler every 2 minutes (`max_instances=1`).
Each run is bounded to 60 seconds, then leaves the rest to the next tick.

**The first run starts where the old poller stopped.** The old poller stored
the profile's history id after each full pass (`sync_state.last_history_id`).
The first run uses it as the cursor. Its first incremental pass then replays
everything since that pass, and those rows arrive as `history` and are fed. If
that id is null (a fresh database) the first run takes the profile's current
id. If it has expired, that is a catch-up from `sync_state.updated_at`.

**Then, once:** the switch-over listing. Unread Primary mail from the last 7
days (`is:unread category:primary newer_than:7d -in:chats`) is fetched and
stored as `switch_over`. That is what the old poller would still have
reached, had its page of ten not overflowed.

**Incremental passes.** `history.list` from the cursor, with types
`messageAdded`, `messageDeleted`, `labelAdded` and `labelRemoved`, page by
page. Gmail returns records after the cursor, not including it:
- **added:** a message added and deleted within the same pass is never fetched, since drafts are replaced constantly. Every other one is fetched and stored if D1 keeps it. A fetch answering `404` means the message is already gone: it is marked gone if stored, never treated as an error;
- **labels changed:** a stored row's labels take the record's additions and removals, with no fetch, and `direction` and `category` are recomputed. A message not stored is fetched only when the change could bring it in (out of `SPAM` or `TRASH`, or into Primary) and it is within the last 90 days;
- **deleted:** `gone_at` is set.

After each page commits, the cursor moves to the last record handled; after
the last page, to the response's `historyId`. A crash or a deploy costs at most
one page.

**Errors.**
- A fetch `404` is the only error a pass passes over.
- Any other error stops the pass where it is. The cursor stays at the last record handled, and the next tick retries.
- A message that has stopped five passes in a row is marked unreadable, counted, and passed over. `/health` shows the count.

**Catching up.** `history.list` answers `404` when Gmail no longer keeps the
cursor, typically after about a week. Only that `404` starts a catch-up, which
runs in one transaction:
1. It records the gap: `gap_from` is the last successful pass less an hour, and `gap_until` is now.
2. It takes the profile's current id as the new cursor, so live mail flows again at the next pass.

The gap is then worked off in the background, one day at a time, newest
first, recording `gap_progress`. A restart resumes it. For each day:
- every listed message is fetched (`catch_up`), refreshing the labels of rows already stored;
- stored rows that day which Gmail no longer lists are marked gone. That covers mail trashed or deleted during the outage.

`gap_*` is cleared only when the whole gap is done.

**The backfill** runs after the incremental pass and any catch-up, and only
spends quota they leave. It lists one day at a time, newest first, back to 90
days (`in:sent OR in:inbox`, less the four other categories, `-in:chats
-in:drafts`), stores what D1 keeps as `backfill`, and records
`backfill_until`, so a restart resumes rather than starting over. A busy
mailbox takes hours, which is fine: nothing waits for it.

**Quota.** One pacer, shared by every Gmail call in the process (the sync, the
pipeline's fetches, and M17's thread reads), counts units by method. The sync
spends at most 2,000 units a minute: a third of the per-user quota, which
leaves the rest to the pipeline and M17. The Gmail client retries 429 and 5xx
for up to two minutes. A rate limit therefore no longer ends a message in
FAILED, which the pipeline's three-second retry made likely. M15's `measure`
CLI runs locally in its own process; the runbook says not to run it during a
backfill.

### D4. The meeting pipeline's feed

**The rule.** Poll's candidates are `gmail_messages` rows that meet all of
these:
- `direction` `in`, `category` `primary`, not `to_self`. Strictly inbound: a forward to yourself would duplicate the original's proposal;
- not labelled `SPAM` or `TRASH`, and not gone;
- not bulk:
  - `precedence` is not `bulk` or `junk`;
  - `auto_submitted` is absent or `no`;
  - for a message with no category label, `has_list_unsubscribe` is false;
- `arrived_via` is not `backfill`;
- no ledger row yet.

They are taken oldest first, up to `POLL_BATCH_SIZE`. Everything after that is
as it is now: `claim`, `start`, park.

**Mailing lists in Primary stay in.** Team mail from Google Groups carries
`List-Unsubscribe` and `Precedence: list`, and meeting requests arrive that
way. Where Gmail has categorised the mailbox, promotional bulk mail has
already left Primary. Where it has not (no category label), `List-Unsubscribe`
is the bulk marker instead. M15's measurement keeps its own wider rule.

**Seven days, then recorded.** A candidate is claimed only while its
`internal_at` is within the last 7 days. Older candidates are never processed,
and never silently dropped. Poll gives each one a ledger row, `SKIPPED`
("too old when reached"), without a model call. That covers:
- mail found by a catch-up after a long outage;
- mail restored from Trash;
- mail held by M17's Pause or spending cap for over a week.

They show in the failures view, and `/health` counts them.

**A message gone before its turn.** The pipeline's fetch raises `MessageGone`
for a `404`, which its retry policy does not retry. Poll records the message
as `SKIPPED` ("no longer in the mailbox"). The phrase joins the purge's fixed
reasons. The graph's edges do not change.

**Until the first sync run,** poll keeps calling `list_unread`, as now.

The sync and the poll stay separate jobs: a slow model call never delays
seeing mail, and a failing sync shows as its own failing job.

### D5. Recall, of the sync and of the feed

A daily job looks at the window from 26 hours ago to 2 hours ago, which keeps
it clear of the sync's own timing:
- **Sync recall:** the ids Gmail lists for the window (`in:sent OR category:primary`, `-in:chats -in:drafts`) are checked against `gmail_messages`. Missing ones are fetched and stored as `recall`, so a miss is repaired as well as reported. Repaired inbound mail is fed: it is within the 7 days.
- **Feed recall:** every stored row in the window that meets the feed's rule must have a ledger row. A row that meets the rule with no ledger row two poll intervals after it arrived means the feed stalled.
- **Category agreement:** every id Gmail lists for `category:primary` in the window must map to `primary` here. A disagreement means D1's mapping is wrong.

Each result goes to `job_runs`. Any shortfall sends one alert a day ("Mail sync
missed messages", or "The mail feed has stalled").

### D6. Liveness

`/health` stays free of database reads, as M15 built it. The sync job records
its last success in the process's liveness record. `/health` returns 503 when
that is more than three intervals old, with the clock starting one interval
after boot, as poll's does. Without this, a dead sync with an empty feed would
leave `/health` green while the agent saw nothing.

With the bearer secret, `/health` also shows these, kept in memory and
refreshed by the jobs that compute them:
- the cursor's age;
- the backfill's and any catch-up's progress;
- unreadable and too-old counts;
- the last recall.

### D7. Retention

The purge deletes `gmail_messages` rows whose `internal_at` is over 180 days
old (the owner's decision), and gone rows a week after `gone_at`.
Ledger rows are untouched. A row is about 0.5 KB with its indexes, so 200
relevant messages a day for 180 days come to about 20 MB. That is well inside
Supabase Free's 500 MB, against which M15 measures real volume.

### D8. Records and tools

Migration `011_mail_sync.sql` (additive, re-runnable): `gmail_messages` with
its indexes (thread; `internal_at`; the feed's rule) and `gmail_cursors`.
`web_reader` gets nothing; the web app shows none of it in M20.

`python -m app.mail.sync`, run on the instance through `fly ssh console`. Every
command takes the same advisory lock as the scheduled job.

| Option | What it does |
|---|---|
| `--once` | Runs one pass |
| `--status` | The cursor, the backfill and catch-up progress, counts by direction, category and `arrived_via`, and the last recalls |
| `--show <message id>` | One row's metadata, without content |
| `--catch-up` | Forces a catch-up, as if the cursor had expired |
| `--check-feed` | The switch-over and backfill checks in the exit criterion |

---

## Deliverables

- **`app/mail/`:** `sync`, `messages`, `feed`, `recall`, `quota`.
- **The Gmail client:** history pages, field-masked metadata, day-window listing, retries, and the shared pacer.
- **The pipeline:**
  - poll reading the feed;
  - the switch-over;
  - the scheduler's `mail_sync` and `mail_recall` jobs;
  - `MessageGone`, and the too-old and gone records.
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
  - the first run takes the old poller's stored id, and its replay is fed;
  - the switch-over listing runs once;
  - an incremental pass stores added messages, applies label changes without fetching, and marks deletions;
  - a message added and deleted in one pass is never fetched; drafts, chats, spam, trash and other categories are never stored;
  - a fetch `404` marks the message gone; any other error stops the pass at its last record; a message failing five passes is passed over and counted;
  - a history `404` records the gap, moves the cursor, and works the gap off, resuming after a restart; rows gone during the gap are marked;
  - a crash mid-pass costs at most one page;
  - the backfill resumes from `backfill_until`, stops at 90 days, and spends only spare quota;
  - the shared pacer holds the sync to 2,000 units a minute, counted by method;
  - a CLI run waits for the scheduled run's lock.
- **Classification:**
  - `SENT` gives `out`;
  - mail from the owner to the owner is `to_self`, and moving it to the Inbox does not feed it;
  - categories map, and a label change recomputes them.
- **Feed:**
  - only strictly inbound, non-bulk Primary mail reaches poll, including Google Groups mail;
  - uncategorised mail with `List-Unsubscribe` does not;
  - `backfill` rows never do;
  - a message read before the tick is still processed; one with a ledger row is skipped;
  - mail older than 7 days gets a `SKIPPED` record, and no model call;
  - before the first sync run, poll uses `list_unread`;
  - a `MessageGone` gives `SKIPPED`, not FAILED.
- **Recall:**
  - a missing id is stored and fed, and gives one alert;
  - a stalled feed raises its alert;
  - a category mismatch is caught.
- **Liveness:** a sync silent for three intervals gives 503; a fresh boot does not.
- **Content:** a test seeds a subject, a snippet and a body, and checks none is stored. The request carries the field mask and exactly the six headers.
- **Integration (Neon):** 011 applies and can be re-run; writes are idempotent.
- **End-to-end, by the owner at the end:** the exit criterion.

## Boundaries

- **Always:**
  - read-only Gmail access (`gmail.readonly`, as now);
  - metadata only, through a field mask;
  - the cursor stored only after the rows it covers, and the gap recorded before the cursor moves;
  - the sync within its share of the quota.
- **Ask first:**
  - storing subjects, snippets or bodies (M18 decides how);
  - a wider OAuth scope;
  - any schema beyond `011`;
  - another retention period.
- **Never:**
  - modifying the mailbox;
  - sending backfilled mail, or mail older than the age limit, through the meeting pipeline.

## Exit criterion

On the deployed stack, at the owner's end tests:
1. A message the owner sends shows as `out` within one sync interval (`--show`).
2. A Primary message opened on the phone before the next tick is still processed by the pipeline. A Promotions message is not stored.
3. `--catch-up` records a gap, works it off, and marks a message trashed during it as gone.
4. `--check-feed`:
   - no `backfill` row has a ledger row created after the switch-over;
   - every Primary message from the switch-over hour was processed, or recorded with a reason.
5. Sync recall, feed recall and category agreement are clean for seven consecutive days.

## Open questions

- **FAILED messages are never retried.** The ledger calls FAILED retryable, but poll skips any message with a ledger row. M20 removes the commonest cause it adds (rate limits) and records the cases it creates (gone, too old). Retrying the rest belongs with M21.
