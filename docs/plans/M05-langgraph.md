# M05 · LangGraph orchestration & durable state

**Est.** 3 days · **Depends on** M04 · **Blocks** M06

## Goal

Rewrite the linear flow as a state graph whose execution survives a process restart mid-approval.

**This is the biggest technical trap in the whole plan.** Get the checkpointer wrong and M06's Telegram approval silently has nothing to resume into.

## Graph

```
fetch ──► classify ──► extract ──► [review] ──► await_approval ──► act ──► log
   │          │           │                          │              │
   └──────────┴───────────┴──────► error ◄───────────┴──────────────┘
```

`review` is a no-op passthrough until M13 fills it in. Wire the node now so adding the reviewer later is a one-node change rather than a graph rewrite.

## Deliverables

- `app/graph/state.py` — the `TypedDict` state
- `app/graph/nodes/*.py` — one file per node
- `app/graph/build.py` — graph construction + compile
- Postgres checkpointer wired up
- Retry-with-backoff on Gmail and LLM calls
- Explicit error node + dead-letter status

## Durability — the part that matters

```python
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import interrupt, Command

graph = builder.compile(checkpointer=PostgresSaver(conn))

# in the approval node:
decision = interrupt({"proposed_event": state["extraction"].model_dump()})

# from the Telegram callback, hours and one deploy later:
graph.invoke(Command(resume={"action": "confirm"}), config={"configurable": {"thread_id": msg_id}})
```

Three things must all be true:

1. **`PostgresSaver`, not `MemorySaver`.** In-memory state dies with the process — and the approval may be answered hours later, after a redeploy.
2. **`thread_id` = Gmail message ID.** Stable, unique, and already your idempotency key. Now the same identifier threads through the ledger, the checkpointer, and the Telegram callback payload.
3. **The interrupt payload must carry everything the approval card needs to render**, because the Telegram handler shouldn't have to re-query the graph to draw a message.

## Retries and failure

- Exponential backoff on Gmail API and Anthropic calls — both rate-limit
- The Anthropic SDK already retries 429/5xx twice by default; tune `max_retries` rather than hand-rolling a loop
- Track `retry_count` in state — M08 will chart it
- Terminal failures write `status='failed'` plus the error to the ledger. A dead-lettered message must be re-runnable by hand.

## Key decisions

- Nodes stay thin. Business logic lives in `app/extraction/`, `app/tools/`, `app/store/` and is unit-testable without a graph.
- The state object holds `EmailMessage`, `ExtractionResult`, `ActionResult`, plus `retry_count` and `error`. Nothing else — resist stuffing it.

## Exit criterion

Start an approval, **kill the process**, restart it, resume the thread — the event is still created correctly. Do this by hand once, then automate it as a test.

## Running notes

**The serializer was a time bomb, and it announced itself as a warning.** The first live run printed:

```
Deserializing unregistered type app.contracts.EmailMessage from checkpoint.
This will be blocked in a future version.
```

Graph state is entirely Pydantic models, so *every* checkpoint depended on a path scheduled to close — a routine dependency bump would have broken resume for every parked approval at once. `app/graph/checkpointer.py` now passes an explicit `allowed_msgpack_modules` allowlist, and the durability test sets `LANGGRAPH_STRICT_MSGPACK=true` so a model added to the state but not to `CHECKPOINT_TYPES` fails the build instead of rotting until the upgrade.

It is also a security boundary: the serializer can construct arbitrary objects from checkpoint rows, so write access to those tables would otherwise mean code execution on deserialize.

**`from_conn_string` can't take a serializer**, so the checkpointer is constructed directly — which requires `row_factory=dict_row`. mypy caught that; `PostgresSaver` reads its rows by column name and a default tuple-row connection fails inside the saver.

**The checkpointer gets its own connection.** It issues queries around node boundaries, and interleaving those with application transactions on one connection invites failures that only appear under load.

**`act` deliberately has no retry policy.** Every other network node gets `RetryPolicy(max_attempts=3)`, but creating a calendar event is not idempotent: a retry after an ambiguous timeout risks a double booking. That path is guarded by the ledger instead.

**The revision cap lives in graph state, not the prompt.** "Only revise twice" in a prompt is a suggestion; a counter the router reads is a guarantee.

**Transient failures are not caught inside nodes.** Swallowing a rate limit into `state["error"]` would turn a recoverable blip into a dead letter. Nodes raise, the retry policy handles it, and only a failure that survives retries reaches the poller's handler and is marked `FAILED` — which is non-terminal, so it can be re-run once the cause is fixed.

**One `nodes.py`, not the planned `nodes/*.py`.** Seven functions of a few lines each; splitting them would mean seven imports to follow one flow.

**Extraction split into two graph nodes.** `ExtractionPipeline` grew `classify()` and `extract()` as separate methods, with `__call__` composing them so the eval harness is unaffected. The graph calls them as distinct nodes, which is what gives M08 per-stage timings and costs.

### Verified

```
ruff / format / mypy --strict   pass
pytest                          355 tests, 0 skipped
```

**Exit criterion — `test_approval_survives_losing_the_process`:** two graph instances, two independent checkpointer connections, nothing shared but Postgres, strict msgpack enforced. Proposal is parked with nothing written; the first connection closes and the graph is discarded; a fresh instance finds the interrupt, resumes on `confirm`, and the event is created with its ID recorded in the ledger.

Live run against the real inbox and Gemini, three messages, all correctly classified as non-meetings and skipped:

```
1a00a9d35c937b2f  skipped
1a00a6a626e03fe3  skipped
1a00a616d1ad290e  skipped
```

**Not yet exercised live: the approval path.** The inbox contains no meeting email, so nothing has parked at `await_approval` outside the test suite. `python -m app.jobs.approve --list` exists as a separate-process front-end for exactly that, and M06 replaces it with Telegram. Send yourself a meeting invite to see it end to end.
