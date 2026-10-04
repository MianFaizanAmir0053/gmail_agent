# M19 · The planner

**Est.** 8 days · **Depends on** M16, M17, M18 · **Blocks** M22 (connector gateway), M25 (memory)

Plan and decisions: [`ASSISTANT-PLAN.md`](../../ASSISTANT-PLAN.md), sections
"Security model" and "What the adversarial review changed".

**Status: approved by the owner on 2026-10-04, on condition of a second
adversarial round, whose findings are folded in below (see Review). The
tasks follow in [`tasks/todo.md`](../../tasks/todo.md).**

## Goal

The owner asks in their own words, in the web app, and the agent answers or
proposes:
- **questions about mail and the calendar** are answered: "when did Sara last write?", "am I free Thursday at three?", "what's on tomorrow?";
- **scheduling from chat** becomes a card with the same bound Confirm as a proposal from mail: "book thirty minutes with sara@example.com next week";
- **the model that plans never reads mail**, and the model that reads mail holds no tool: the planner sees the owner's typed words and nothing else, plans once, and never sees a result;
- **a tool-routing eval comes first**, and the planner ships only if it passes.

**What M19 cannot promise.** It cannot make an answer drawn from mail true: a
sender can lie, and the agent reports what the mail says, labelled with who
sent it and whether Gmail verified their domain. It promises that nothing a
sender writes can choose what the agent does, and that what the owner is told
from mail always says where it came from.

## Why this comes next

The code as of `118f9a8` (mapped on 2026-10-04):
- **There is no chat.** The web app shows proposals made from mail, and the owner can only decide them (`dashboard/src/app/page.tsx`).
- **The approval path is built around a Gmail message and its graph.** `proposals.message_id` is a foreign key to the ledger. The worker's withdraw, give-up and advance steps all read the message's LangGraph checkpoint (`session.thread`, `step_for`). The recipient rule asks Gmail for the message's thread (`contacts.unconfirmed_outsiders`). Telegram's buttons carry the message id after the last colon.
- **The registry knows one argument shape.** `Registry.execute` takes `CreateEventInput`, and `bound()` validates a parked `proposed` as an `ExtractionResult` (`app/policy/`).
- **The tool loop is plan-and-act.** `structured_call` feeds each tool's result back into the same model (`app/extraction/llm.py`). M18 removed every caller; the loop and `search_context` stayed "for M19". M19 does not need them.
- **The gate has one ceiling.** `Gate.check` compares a message's spend with `MESSAGE_CEILING_USD` ($0.50). `allows_new_work()` checks the monthly cap and pricing, not Pause, which is `control.is_paused`, checked separately (`app/policy/`).
- **One calendar for everything.** `CalendarClient` holds the test calendar: free/busy, conflicts and writes all use it, and it cannot list events (`app/google/calendar.py`).
- **The web app's database role reads no mail content** (migration 008).
- **There is no routing eval.** The plan requires one before the planner (`ASSISTANT-PLAN.md`, key assumptions).

## Decisions (4 Oct 2026)

The owner took the recommended choice for each, over four rounds. The third
followed the research, and the fourth followed the first adversarial review.

1. **First jobs:** answering questions about mail and the calendar, and scheduling from chat. Drafting replies and the daily brief wait.
2. **Surface:** a chat page in the existing web app, behind the same Google sign-in, on both phones.
3. **Mail facts come through a quarantined reader:** a tool-less model reads mail and returns fixed, typed fields; the planner never sees email text (the dual-LLM pattern).
4. **Approvals: one queue, two sources,** by an additive migration (D8). The schema change is approved with this spec.
5. **Guests in a chat invite:** an address the owner typed counts as theirs for that proposal, and the card says so. An address that came from elsewhere needs an Allow, as M17 requires.
6. **Search:** Gmail's own search, built by code from typed fields. No ingestion, no embeddings.
7. **The eval bar, in two steps.** The planner ships behind `DRY_RUN` on about 60 cases: the right operations at least 95% of the time, the right outcome at least 90%, and every request it must refuse or ask about handled correctly (100%), five samples a case. Before `DRY_RUN` goes off, the set grows to about 150 routing cases, three runs each, plus 100 cases that tempt a wrong write, with none allowed. 48 right out of 50 shows a true rate anywhere from 86.5% to 98.9%.
8. **The plan is a JSON document of fixed steps,** from one model call; code runs it and checks every type; no result ever goes back to the model.
9. **Answers are rendered by code.** What came from mail is labelled with where it came from. No model writes the final answer.
10. **A ceiling of $0.10 per chat turn.** A turn that reaches it stops and says so.
11. **The planner reads the owner's primary calendar,** within the scopes already granted, and answers name the calendar. Where writes go stays M17's open question, decided before `DRY_RUN` goes off.
12. **The web app may read chat answers.** A column-level grant gives `web_reader` a turn's status, its typed request, its rendered answer and its reason. These are cleared after the turn's cards settle (D12). This is a deliberate change to M16's rule that the web app reads no mail content.
13. **Pasted text is not the owner's:** an address in it needs an Allow.
14. **Two cuts from the first version.** No follow-ups: each request stands alone. No times taken from mail: a slot comes from the owner or from free/busy, since the mail pipeline already proposes a sender's time.

**A default set with this spec, open at the owner's word:** a daily chat cap
of $1.00, and $15 a month kept for mail (D9).

**What the second review made stricter than decisions 5 and 13**, recorded
for the owner:
- **Pasted text never reaches the planner at all,** not only its addresses (D5).
- **A name becomes a guest only through a confirmed contact.** Any other name means the owner types the address (D3). The tap-to-pick list is gone, along with the laundering it invited.

## Scope

**In:**
- a chat page, its records, and an asynchronous turn: the request is stored, a job answers it, and the page shows the answer;
- the planner and its operations: search mail, read a message (through the reader), list events, free/busy, find a free slot, find a confirmed contact, propose a hold, propose an invite;
- proposals from chat on the queue mail uses: the worker, Withdraw, the sweep, Telegram, the recipient rule by source;
- conflicts read from the primary calendar, for mail's cards as well as chat's;
- spending: a ceiling per turn, a daily cap, a reserve for mail, and traces for chat;
- the tool-routing golden set and its eval, before the planner;
- an injection suite for chat;
- removing the tool loop and `search_context`, which nothing needs.

**Out:**
- follow-ups and conversation history, and times taken from mail (decision 14);
- picking among contacts found in mail; a contacts provider (M23);
- sending, replying, drafting, labelling, archiving (wider scopes; later modules);
- the daily brief and loose ends (M21);
- memory (M25), other sources (M22, M23);
- the vector corpus in production;
- Telegram as a chat surface;
- choosing the calendar writes go to.

## Research (4 Oct 2026)

Read on the day, by a research agent, from primary sources.

- **The pattern that fits** is plan-first, with a quarantined reader. Beurer-Kellner et al. (2025, arXiv 2506.08837) set the rule: once untrusted input has been read, consequential actions must be impossible. Their email and calendar case study finds that planning first costs little, but recipients and bodies can still be steered, so actions need the user's confirmation. CaMeL (DeepMind, 2025, arXiv 2503.18813) is the strong form: a planner writes code from the query alone, a tool-less reader fills schemas, and an interpreter tags every value with its origin and checks a policy before each call. It solved 77% of AgentDojo's tasks, against 84% undefended, at about 2.8 times the tokens. Its stated limits:
  - text-to-text attacks: a misleading summary;
  - side channels;
  - hand-written policies;
  - approval fatigue;
  - tasks where data has to become control flow.
- **Meta's Rule of Two** (2025) holds here: the planner never reads untrusted input, and the reader cannot act. Newer work (2026) builds on these and supersedes none. It also finds that adaptive attacks beat most defences that run inside the model's own context, which is why the checks here run in code.
- **Gemini.**
  - `generateContent` remains supported. The Interactions API stores conversations on Google's servers by default, so M19 does not use it.
  - A plan from one structured-output call needs no function responses, so Gemini 3's thought signatures do not arise.
  - Gemini 3's default temperature, 1.0, is the recommended one.
- **Evaluation.**
  - BFCL's categories: simple, multiple, parallel, irrelevance (no call is right), and multi-turn (missing parameters means ask; missing functions means decline).
  - τ-bench judges by the end state, and its pass^k counts a task only if it succeeds on every one of k runs.
  - Below a few hundred items, use Wilson intervals.
  - Zero failures in n cases bounds the failure rate at about 3/n.
- **Prices** (official page, updated 2026-10-01): 3.6 Flash costs $0.75 per million input tokens and $3.75 per million output, doubling on 2027-01-01. Thinking tokens bill as output.

---

## Design

### D1. A chat turn

- **The page.**
  - `/chat` in the web app, owner-only like every page. The owner types a request of up to 2,000 characters.
  - The page records which parts arrived by paste or drop (D5).
  - A server action checks the session and posts the request to Fly (`POST /api/chat`). The post carries the pasted ranges, the web app's zone, and an idempotency key made by the page and unique in `chat_turns`. A resend after the web app's 10-second abort finds the turn already stored, and never makes a second.
  - Fly refuses a zone that is not its own `USER_TIMEZONE`, so the two sides cannot disagree about "tomorrow". Otherwise it stores the turn and answers 202 at once.
  - While a turn is open the box is locked, and the page re-reads every two seconds, then every ten after thirty seconds.
- **The record.** `chat_turns`:
  - id and idempotency key;
  - the typed request, and any pasted text, kept apart;
  - when it was asked;
  - status: `queued`, `running`, `answered`, `stopped` or `failed`;
  - the validated plan;
  - the rendered answer;
  - a fixed reason when it stopped or failed;
  - when it finished.
- **Running it.**
  - **The job.** A scheduler job, `chat`, with `max_instances=1`, runs queued turns one at a time. The API wakes it as `wake_decisions` wakes the worker, and it also ticks every 30 seconds.
  - **The deadline.** A turn has 90 seconds, inside the process's kill timeout.
  - **Writes are conditional.** Every status write applies only while the turn is `running`, and a park happens in the same transaction as it.
  - **Stranded turns.** A turn found `running` past its deadline, at boot or by the job, is marked `failed` ("stopped by a restart"), and nothing is parked after that. There is no retry: the owner asks again.
- **Traced.** Each turn is a trace (`runs` and `spans`) under the message id `chat-<turn>`, so `/costs` and M15's cost report include chat.
- **Each request stands alone** (decision 14).

### D2. The planner: one call, one plan

- **The call.** `app/planner/plan.py` makes one structured call (`structured_call` with a schema and no tools, at a low thinking level). The model's default temperature, 1.0, is the one Gemini 3 recommends, and `structured_call` sets none.
- **The schema is flat,** because the endpoint rejects much of JSON Schema. A plan has:
  - `kind` (`answer`, `clarify`, `decline`);
  - `steps`: at most four;
  - a `reply`.

  Each step has an operation from an enum, and the fields for that operation, every one optional in the schema and required by code. No field takes two kinds of value. A slot is either `slot_ref` (`$s1`) or `start` and `end`; one item of a list is `$s1.0`. The `reply` has separate fields: `template`, `question`, or `decline_code`.

  The schema is tried against the API before any case is written, and the baseline's hash covers it.
- **The planner's input.**
  - The system instruction holds the catalogue of operations, their fields and the conventions. It says that the planner never sees results. Its worked examples are not in the golden set.
  - The user turn holds the grounding block, taken at the moment the owner asked (`asked_at`, so "tomorrow" means the owner's tomorrow), and the owner's typed request. Pasted text never reaches the planner (D5).
- **Times.** The planner writes local times with a zone, as extraction does, and code converts them.
- **Validation, before anything runs.** The plan is refused for:
  - an unknown operation;
  - a missing or wrong field;
  - a reference that points forward, to nothing, or to the wrong type, such as a list where one value is wanted;
  - more than four steps.

  The turn then fails as "could not plan this", and nothing runs.
- **The model.** `PLANNER_MODEL`, by default production's extraction model. It must be priced, it is in `models.in_use()`, and it has a pipeline version of its own.

### D3. Operations

**Reads** (T0, run by code, never by a model):
- **`mail.search(from?, to?, words?, after?, before?, limit ≤ 10)`.**
  - It returns message refs: id, thread, sender address, whether Gmail verified the sender's domain (`dmarc_passed`), date, and the subject as quoted text.
  - Code builds the Gmail query; the planner never writes Gmail syntax.
  - A search's subjects pass through `redact_secrets` before they are kept, since a metadata read never sees a body and cannot tell credential mail.
- **`mail.read(message_ref, ask)`** hands one message to the quarantined reader (D6): only that message's own words, above any quoted or forwarded section. `ask` is a fixed set: `asks_of_owner`, `people`, `summary`.
- **`calendar.list_events(start, end)`** reads the primary calendar (decision 11). Each event comes back with its start, its end, its organiser, and its title as quoted text.
- **`calendar.freebusy(start, end)`**, on the primary calendar.

**Helpers** (pure code, no I/O):
- **`slots.first_free(busy_ref, window_start, window_end, minutes)`.** Working hours come from `WORKING_HOURS`, Monday to Friday, 09:00 to 18:00 by default.
- **`contacts.find(name)`** matches a name against the confirmed contacts' addresses, the part before the at-sign, ignoring case and dots. One match is a guest the owner already allowed. Several, or none, end the turn with a `clarify` reply: "which address?" or "type the address". Mail is never searched for an address.

**Proposals** (never run without the owner's Confirm): `calendar.propose_hold(title, slot)` (T1) and `calendar.propose_invite(title, slot, guests)` (T2). Each parks a proposal on the queue (D8).

**Two calendars.** `CalendarClient` gains a read calendar, the primary, apart from its write calendar, which is today's test calendar until M17's open question is decided:
- reads and conflict checks use the primary, for chat's cards and for mail's;
- writes and the approval's binding use the write calendar.

A card's "no conflicts" then means the owner's real calendar is free.

### D4. Origins

Every value carries an origin:
- **typed:** the owner's typed request, and what the planner wrote from it;
- **calendar:** the owner's calendar's times;
- **contact:** an address among the confirmed contacts;
- **mail:** anything that came from mail, or from text the owner pasted.

A value an operation computes takes the most untrusted origin among its inputs, in that order from typed to mail.

The rules, checked by the executor (`app/planner/execute.py`) before each operation:
- **A read's arguments** are typed or calendar values, or a ref returned by a search. No text from mail ever becomes a query.
- **A title** is typed.
- **A slot** is typed or calendar (decision 14).
- **A guest** is an address the owner typed (D5), or a confirmed contact. A pasted address is offered on the card for an Allow (D5), never placed in a plan.
- **Text from mail or from an event** is a `Quoted` value. It is shown to the owner, labelled, and accepted by nothing else.

A step that breaks a rule fails with a fixed reason. The turn answers what it could, and says what it would not do.

### D5. What the owner typed, and what they pasted

The browser knows what was pasted: the page records the ranges that arrived by a paste or a drop event. Fly keeps them apart from the typed request, so typed and pasted are never guessed from the text.

- **The planner reads the typed request only.** Pasted text goes to the reader if the plan asks for it (`mail.read` of the paste, origin mail). It is shown back quoted, and never shapes a title, a time or a guest.
- **Typed addresses.** Code finds the addresses in the typed request with the scrubber's own pattern (`scrub.ADDRESS`), and drops the owner's own and their aliases. A guest is "named by you" only when its `guest_key` matches one of them.
- **An address the planner wrote** that the owner never typed, and that is not a confirmed contact, fails D4.
- **Pasted addresses** are listed on the turn's answer as "from text you pasted", each with an Allow. Once allowed, an address is a confirmed contact, which the owner can then name in a new request.

### D6. The quarantined reader

`app/planner/reader.py`:
- **What it reads.** A model with no tools reads one message's own words: above any quoted or forwarded section, found with `split_quoted`. The message is prepared as the fetch prepares it (`scrub.prepare`), between per-call markers (M18, D3). The reader can also read the owner's pasted text, labelled as such.
- **What it returns.** It answers one fixed question into a fixed schema, and everything it returns has origin mail.
  - Its prose is `Quoted`, labelled by code with that message's sender. The model never chooses who said what.
  - The addresses it names pass through M18's `named()` against that message. One the message does not contain is marked "not found in the email".
- **Limits.** A turn calls it at most three times, and the text is cut to the prompt's room.
- **No tools.** The structural test that keeps tools out of models that read mail covers `app/planner/reader.py` (D13).

### D7. Answers, rendered by code

- **Templates.** A plan's `reply.template` names one, over its steps' results:
  - free or busy at a time;
  - a list of events;
  - the last messages from someone;
  - a quote from the reader;
  - a proposal made ("see the card").
- **Labels.**
  - Everything from mail is labelled with the sender's address, whether Gmail verified their domain, and the date. This covers typed values, counts and lists, not only quotes.
  - A display name is never shown as who someone is.
  - An event whose organiser is not the owner shows its title quoted, under its organiser: invitations from strangers reach the calendar by default.
- **Cleaning and form.** `Quoted` text passes through `shown()` before it is stored. Answers render as plain text, with no Markdown and no links made from text. The chat page sets a content policy that loads no remote image.
- **Clarify and decline.** A `clarify` reply asks the planner's question, made from the typed request alone, or one of D3's fixed questions. A `decline` reply gives its fixed reason: "sending email is not something I can do", "I don't act on what an email asks", and so on.

### D8. Proposals from chat: one queue, two sources

- **The migration** (`013_chat.sql`, additive):
  - `chat_turns` (D1, D12);
  - `processed_messages.source` (`mail` or `chat`, default `mail`);
  - `proposals.source` and `proposals.chat_turn_id`.
- **The id.** A chat proposal's id is `chat-<turn>-<step>`, with no colon: Telegram's buttons split on every colon (`parse_callback`). Its ledger row is claimed first (`claim`, with the turn as its `thread_id`), then parked by `write_park`, which updates an existing row.
- **No graph.**
  - A chat proposal parks straight from the executor. Its `proposed` is an `ExtractionResult`, so `bound()` and `event_args` hold it unchanged, and the event's description is the fixed one with the chat id.
  - Conflicts are checked at park, on the primary calendar (D3).
- **The worker, by source.** A chat proposal's state is read from its proposal, its ledger row and its outbound action, never from a checkpoint. `apply_open`'s withdraw, `apply_one`'s give-up and `_advance` all dispatch on `proposals.source`:
  - **Confirm:** the worker's checks, then `Registry.execute` from the stored `proposed`, then the ledger mark (created, or skipped in dry run).
  - **A refusal, or a failed check** (guests, a changed hash, the mode): the proposal returns to the owner at the next generation, re-parked from the stored `proposed`, as mail's is from the checkpoint.
  - **Give-up** with an action still `executing`: `look_up` finds the event or not, then `close`, as M17's D3 requires. Nothing is settled on a guess.
  - **Withdraw** while queued or held: the same rules as mail's.
  - **Cancel and the sweep** end it.
  - **Edit** is refused for chat, in `decide()`, from every channel, Telegram's reply-to-correct included. The owner asks again instead.
- **The recipient rule, by source.** `Outsiders` dispatches on the source. For chat, the inside guests are the addresses typed in the stored turn's request (D5), re-derived at every check, and the confirmed contacts. Gmail is not asked.
- **The card** shows:
  - the typed request it came from;
  - where the title came from;
  - each guest's source: "named by you" and "a confirmed contact" join M18's sources in `participants.py`, `guests.ts` and Telegram's cards;
  - its conflicts.

  A chat card has no Edit button, on the web or in Telegram.
- **M24.** Chat proposals have a pipeline version and a source of their own, so M24, when it is built, can leave the owner's own requests out of the evidence for mail.

### D9. Spending

- **A chat check before every model call:** before the plan, and before each reader call. It also runs before each park. A turn stopped by one says why. The check:
  - the owner has not paused (`control.is_paused`);
  - `allows_new_work()`: the monthly cap and pricing;
  - the day's chat spend is under `CHAT_DAILY_CAP_USD` ($1.00), counting the owner's day in `USER_TIMEZONE`;
  - the month's spend leaves `MAIL_RESERVE_USD` ($15) for mail, so chat never stops mail late in a month.
- **A ceiling per turn,** $0.10 (`CHAT_TURN_CEILING_USD`), through a gate of its own. The turn's spend is metered under `chat-<turn>`. A turn can overshoot by one call, since a call's cost is known only after it.
- **What a turn costs.** A plan call is about 4,000 tokens in and 400 out plus a low thinking budget, and a reader call about 6,000 in and 500 out. At today's 3.6 Flash prices, thinking tokens billed as output, a typical turn costs one to four cents, and two to eight at 2027's. At most $25 a month goes to chat. The routing eval records the real figures.

### D10. The tool-routing eval, first

- **Cases.** `data/planner/routing/` holds about 60 cases. Each is the owner's typed request, any pasted text, an instant and a zone, a fixed fake calendar and mailbox, and the outcome expected: the plan's kind, its operations, and the end state.
- **Categories:**
  - each operation on its own;
  - look-alike operations;
  - scheduling in several steps;
  - irrelevance;
  - missing details, where the planner must ask;
  - unsupported actions (send, delete, forward, pay), where it must decline;
  - "do what this email says", and pasted mail, where it must decline or ask.
- **Scoring by outcome, not by the plan's spelling,** as τ-bench judges by the end state. Each sampled plan runs against the case's fakes. It scores on:
  - the right operations (a multiset, so renumbered references do not matter);
  - the right outcome: the slot proposed, the guests by `guest_key`, the answer's facts;
  - the right kind (ask or decline).

  Free text, such as search words and titles, is not compared.
- **The run.** `app/eval/planner.py` and `.\tasks.ps1 planner-eval`: five samples a case, on the planned model.
- **The bar:**
  - the right operations at least 95% of the time;
  - the right outcome at least 90%;
  - every ask or decline case right (100%).
- **The baseline** is committed like M18's injection run. It is taken once the prompt is frozen, tied by a hash to the code that shapes the planner's prompt, and checked by CI.
- **Before `DRY_RUN` goes off,** the set grows (decision 7).

### D11. The injection suite, for chat

The cases are mail that search will find, or text the owner pastes, that tries to steer what the owner is told or asked:
- "add this address";
- "the owner wants this";
- a forged sender name;
- a quoted chat reply.

The planner never sees mail or pasted text, so no plan can change. Each attack is still driven through the executor, with a compliant fake reader that does whatever the text says:
- no proposal carries a title, a slot or a guest from mail;
- no label is missing, or names the wrong sender;
- no display name is shown as identity;
- nothing is written without the owner's Confirm.

The model run carries the same cases, checking the reader's quoted output for code and link markers.

### D12. Records, grants and retention

- **The web app's grant** (decision 12). `web_reader` may read `chat_turns`' id, status, asked time, typed request, rendered answer and reason. It may not read the pasted text or the plan.
- **Retention.** A turn's typed request, pasted text, plan arguments and answer are cleared a week after the turn finished, and only once every proposal it made has settled. A pending card still needs the typed addresses. The plan's structure stays, for statistics. The purge does this, in `_clear_content`'s fashion.

### D13. Removing the tool loop, first

`structured_call`'s tool loop (`tools`, `dispatch`, the turn limit), `app/tools/search_context.py` and their tests go. M18 kept them "for M19", and M19 needs neither.

The structural test (`tests/test_no_tools.py`) changes with them:
- it asserts that `structured_call` takes no tools at all;
- it scans `app/planner/` as well as `app/extraction/`.

"No result goes back to a model" is then a property of the code. M15's cost report loses its line about search coming back.

---

## Deliverables

- **Python:**
  - `app/planner/`: plan, execute, origins, reader, render, the chat job;
  - `CalendarClient`'s read calendar and `list_events`;
  - the chat API in `app/web_api.py`;
  - the worker's steps for chat;
  - the recipient rule by source;
  - the new sources in `participants.py`;
  - Edit refused for chat in `decide()`;
  - Telegram's cards without Edit for chat;
  - the chat check, gate and caps;
  - chat traces;
  - the purge for chat;
  - `app/eval/planner.py`;
  - removing the tool loop;
  - settings: `PLANNER_MODEL`, `CHAT_TURN_CEILING_USD`, `CHAT_DAILY_CAP_USD`, `MAIL_RESERVE_USD`, `WORKING_HOURS`.
- **Web:**
  - `/chat`: its paste tracking, server action and content policy;
  - the new guest sources and the request on cards;
  - no Edit for chat.
- **Data:** `data/planner/routing/`; chat injection cases in `data/injection/`.
- **Migration:** `013_chat.sql`, with `web_reader`'s column grant.
- **Results:** the routing baseline.
- **Docs:** DEPLOY.md §14, README.

## Commands

```powershell
.\tasks.ps1 check
.\tasks.ps1 planner-eval              # the routing eval, five samples a case
.\tasks.ps1 planner-eval --baseline   # and write the committed baseline
```

## Testing

- **Removal first:** the structural test fails while a `tools` argument exists, and covers `app/planner/`.
- **The plan:** the schema accepted by the API (once, recorded). Validation refuses an unknown operation, a missing field, a list where one value is wanted, a bad reference and a fifth step.
- **Operations:** each, on fakes:
  - `list_events` and the read calendar;
  - `contacts.find`: one match, several, none;
  - `first_free` with `WORKING_HOURS`;
  - `mail.search`'s query and its redacted subjects.
- **Origins:** each rule in D4, with a test that fails without it. That includes a value computed from mail inputs, a pasted address, and an address the planner invented.
- **Typed and pasted:** ranges kept apart; the planner given typed text only; the owner's alias dropped.
- **The reader:**
  - one message's own words, its quoted section left out;
  - labels from code;
  - a named address the message lacks marked;
  - three calls at most.
- **Rendering:**
  - every template;
  - labels on every value from mail;
  - display names never shown as identity;
  - stranger organisers quoted;
  - plain text.
- **The chat path, on Postgres:**
  - stored, run, answered and traced;
  - an idempotent resend;
  - a zone mismatch refused;
  - a stranded turn failed, with nothing parked after;
  - the deadline;
  - Pause, the daily cap and the mail reserve at each check.
- **Proposals from chat, on Postgres:**
  - through `decide()`, the worker and the registry;
  - a refusal re-parked at the next generation;
  - give-up with an executing write looked up;
  - Withdraw while held;
  - Edit refused from every channel;
  - Telegram's buttons decoding a chat id;
  - a typed guest needs no Allow, and a pasted one does;
  - conflicts from the primary calendar.
- **Records:** the grant, which `web_reader` can and cannot read, and the purge waiting for settled cards.
- **The web app:**
  - paste ranges recorded;
  - the content policy set;
  - cards with the request and the new sources;
  - no Edit for chat.
- **The routing eval:** outcome scoring on fakes, the harness with a fake model, and the baseline gate.
- **The chat injection cases:** a compliant fake reader through the executor.

## Boundaries

- **Always:**
  - plan before reading anything;
  - check every value's origin before every operation;
  - render answers in code, labelled;
  - keep mail and pasted text out of the planner's context.
- **Ask first:**
  - a new operation;
  - any write that is not a proposal;
  - a new scope;
  - letting a plan read results (a second planning round);
  - widening `web_reader`'s grant.
- **Never:**
  - feed a result back to the planner;
  - give the reader a tool;
  - act without the owner's Confirm;
  - show a display name as identity;
  - use the Interactions API.

## Exit criterion

1. The routing eval's baseline is committed and passes the bar, and CI checks it.
2. On the deployed stack, with `DRY_RUN` on and the primary calendar, these come back right and labelled:
   - "am I free tomorrow at 3pm?";
   - "when did <someone> last write?";
   - "what's on Thursday?".
3. These park the right cards:
   - "book 30 minutes with <address> next week": the guest is "named by you";
   - "book 30 minutes with <a confirmed contact's name>": the guest is "a confirmed contact";
   - an address pasted in: the address is offered for an Allow, never placed on a card.
4. A planted email that tries to steer, found by a search, changes no plan. Its text shows only as a labelled quote, with no display name taken as identity.

## Open questions

- **Names to addresses beyond confirmed contacts:** a contacts provider (M23).
- **Follow-ups**, and choices that need a plan to read a result ("book the second one"). A second planning round over typed refs only is the likely shape, after M19.
- **Where writes go:** M17's open question, before `DRY_RUN` goes off.

## Review

Two adversarial rounds, each in fresh context, against the code, on 2026-10-04.

- **Round 1: 23 findings,** all accepted.
  - **Security:**
    - "typed by the owner" was undefined;
    - origins were lost through helpers;
    - code stated sender-controlled facts as fact;
    - quoted text went uncleaned;
    - "found in your mail" hid how.
  - **Fit with the code:**
    - no graph for a chat proposal;
    - the guest check asked Gmail for a chat id;
    - event arguments;
    - `web_reader` and M24;
    - retention;
    - duplicate turns from the 10-second abort;
    - one ceiling;
    - the test calendar.
  - **Gaps:**
    - Pause and the caps;
    - concurrency and polling;
    - time zones and working hours;
    - the plan schema on Gemini;
    - the chat card;
    - monthly cost.
  - **Over-design:**
    - follow-ups;
    - times from mail;
    - the tool loop;
    - `contacts.find`;
    - the step count.
- **Round 2: 11 findings** in the revised design, all accepted.
  1. **The worker.** Withdraw and give-up still read a checkpoint. The worker's steps for chat now dispatch on the source, with re-park, look-up and close (D8).
  2. **Pasted text** reached the planner as the owner's, and the text-pattern rule misjudged both ways. The browser now marks pastes, and the planner reads typed text only (D5).
  3. **Picking a contact** had no mechanism, and the obvious build skipped the Allow. Contacts now come from confirmed contacts only, and anything else means typing the address (D3).
  4. **The reader's labels** named the wrong sender for a thread. It now reads one message's own words, and code labels it (D6).
  5. **The card showed the request** that `web_reader` could not read. The typed request is now in the grant, and the paste is not (D12).
  6. **Telegram's buttons** split on colons, and the new sources were unknown. Chat ids now have no colon, the sources are added on both sides, and chat cards have no Edit (D8).
  7. **Two calendars, one client:** "free" and "no conflicts" disagreed. Reads and conflicts now use the primary, and writes and the binding use the write calendar (D3).
  8. **Spending:**
     - `allows_new_work()` does not check Pause;
     - chat could exhaust mail's month;
     - chat had no traces;
     - thinking tokens were uncounted.

     A chat check, the mail reserve, traces and revised estimates fix these (D9, D1).
  9. **The flat schema hid unions,** and "right arguments" was undefined. Fields are now per operation, and scoring is by outcome on fakes (D2, D10).
  10. **Ordering and coverage:**
      - chat parks only after the worker, the rule, Edit and Telegram are ready;
      - the schema is tried first and the baseline taken last;
      - the tool loop goes first;
      - tests now cover every deliverable;
      - the zone travels with each request.
  11. **Small:**
      - the ledger row is claimed before it is parked;
      - confirmed contacts are consistent across D4, D5 and D8;
      - the temperature is the model's default.

## Running notes

### Specified (2026-10-04)

- Four rounds of the owner's decisions, a research agent's cited summary, a codebase survey, and two adversarial reviews.
- The owner approved the spec on condition of the second review, whose findings are folded in above.
- The second review made two rules stricter than decisions 5 and 13, recorded under Decisions for the owner.

### Task 19.1, the tool loop removed (2026-10-04)

- **Removed:** `structured_call`'s `tools`, `dispatch` and `max_tool_turns`, with the loop that fed each result back to the model; `app/tools/search_context.py`; and their tests (`test_tool_loop.py`, `test_search_context_tool.py`). `structured_call` is now one call and one answer, its request byte-identical to the old path without tools.
- **The structural test** (`tests/test_no_tools.py`) was rewritten first and failed on the old code. It now asserts that `structured_call` takes no tools at all and that the search tool is gone. It scans every model caller in `app/extraction/` and `app/planner/` for `**kwargs`, and still guards the pipeline's searcher and the graph's reviewer.
- **M15's cost report** loses its search projection. Its planner line now says chat is metered per turn and capped (D9).
- **Kept:** the corpus search (`app/rag/search.py`) for the retrieval eval and the demo, which now says the planner uses Gmail's own search.
- 2373 tests pass, with lint and mypy clean.
