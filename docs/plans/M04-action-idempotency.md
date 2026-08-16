# M04 · Action layer & idempotency

**Est.** 1 day · **Depends on** M03 · **Blocks** M05

## Goal

Let the model *call* the calendar tool, and make sure running the poller twice never creates the same event twice.

## Why idempotency lives here, not in the reviewer agent

The original plan listed "duplicate bookings" as a Phase 5 reviewer-agent responsibility. That's the wrong layer. A scheduled poller with no processed-message ledger will re-read the same email on every restart and every overlapping run, and no amount of LLM reasoning fixes that. It's a database constraint.

## Deliverables

- `app/tools/calendar_tool.py` — `create_calendar_event` as a model-callable tool
- `migrations/002_processed_messages.sql`
- `app/store/ledger.py` — claim/complete/fail transitions
- Gmail `historyId` cursor persistence for incremental fetch
- Free/busy conflict check before proposing an event

## Schema

```sql
CREATE TABLE processed_messages (
    gmail_message_id TEXT PRIMARY KEY,       -- the idempotency key
    thread_id        TEXT NOT NULL,
    status           TEXT NOT NULL,          -- claimed|extracted|awaiting_approval|created|skipped|failed
    calendar_event_id TEXT,
    error            TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE sync_state (
    id              INT PRIMARY KEY DEFAULT 1,
    last_history_id TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT single_row CHECK (id = 1)
);
```

Claim with `INSERT ... ON CONFLICT (gmail_message_id) DO NOTHING RETURNING *`. If nothing comes back, another run already owns it — skip. This is what makes concurrent/overlapping poller runs safe.

## Tool definition

Mark it **strict**:

```python
{
    "name": "create_calendar_event",
    "description": "...",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": { ... },
        "required": [ ... ],          # list every property
        "additionalProperties": False, # required for strict
    },
}
```

Strict mode guarantees `tool_use.input` validates against the schema exactly, which removes a whole class of "the model invented a field" bugs.

## The tool-calling vs function-calling distinction

Worth being precise about, because it comes up in interviews and the original plan's framing was a bit of a strawman.

- **Extraction** is best done with **structured outputs** — you want a schema-validated object back, not an action.
- **The calendar write** is best done as a **model-callable tool** — the model decides *whether* to call it and with *what arguments*.

Both are used, for different jobs. The meaningful distinction isn't "you never call the function yourself" — it's that the model owns the decision and the arguments, while your harness owns execution, validation, and the approval gate.

## Key decisions

- Still behind `DRY_RUN`. Real calendar writes start at M06.
- Free/busy check runs *before* the approval card is sent, so the card can say "⚠️ conflicts with Standup".
- Record `calendar_event_id` on success — M13's duplicate detection and any future undo depend on it.

## Exit criterion

Run the poller twice over the same inbox: **zero duplicate calendar events and zero duplicate LLM calls.** Assert it in a test, don't just eyeball it.

## Running notes

**mypy caught a bug that would have fired on every dry run.** `execute_create_event` returned `ActionResult(status="skipped")`, but `"skipped"` was never in the contract's `Literal` — a pydantic `ValidationError` waiting for the first dry-run write. Added `"dry_run"` as its own status rather than folding it into `skipped_duplicate`: they mean different things ("the flag was on" vs "we already booked this"), and a dashboard that conflates them hides a deployment that has been writing nothing all week.

**`FAILED` is deliberately not terminal.** Failures here are usually transient — rate limit, timeout, a five-second Google blip. Making them terminal would mean hand-editing rows to retry.

**The `CHECK` constraint duplicates the Python guard on purpose.** `(status = 'created') = (calendar_event_id IS NOT NULL)` is enforced in the database as well as in `mark()`, so a future code path that writes SQL directly still cannot record a created event with no event ID. A test asserts the database rejects it independently.

**`unseen()` is a pre-filter, never the authority.** It exists to avoid fetching message bodies we already know about, but another run can insert between that query and the claim. `claim()`'s `INSERT ... ON CONFLICT DO NOTHING RETURNING` is what actually decides ownership, and a test proves it across two live connections.

**Integration tests need real Postgres, not SQLite.** `ON CONFLICT`, `CHECK`, and `ANY(array)` are the behaviour under test; a SQLite stand-in would test a different database and prove nothing about the one in production. They skip cleanly when no server is reachable, and CI now runs a `pgvector/pgvector:pg16` service so the guarantee stays verified rather than silently skipped.

**The test fixture clears tables before yielding, not just after.** A test asserting "the sync cursor starts empty" failed the moment `app.jobs.poll` ran against the same development database. Rolling back protects tests from each other; clearing upfront protects tests from real data — and because it is inside the rolled-back transaction, the development rows survive.

**Added `app/jobs/poll.py` ahead of the plan.** The exit criterion talks about running "the poller", which the module's own deliverable list never included. It claims and marks a placeholder status, does no extraction, and carries a `--reset` to clear its own rows — M05 replaces `_process` with the graph invocation and keeps the surrounding claim/mark structure.

### Verified

Against the live inbox, twice in a row:

```
RUN 1   Saw 10 unread, claimed 10 new.
RUN 2   Saw 10 unread, claimed 0 new.     <- idempotency holding
```

Placeholder rows cleared afterwards with `--reset`, so those ten messages are still available for M05 to process properly.

```
ruff / format / mypy --strict   pass
pytest                          240 tests, 0 skipped (Postgres up)
```
