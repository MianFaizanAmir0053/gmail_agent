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
- **`slots.first_free(busy_ref, minutes)`.** It looks within the range of the free/busy step that `busy_ref` names. Working hours come from `WORKING_HOURS`, Monday to Friday, 09:00 to 18:00 by default.
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

### Task 19.2, the plan's schema and its validation (2026-10-04)

- **`app/planner/plan.py`:** the model writes a `PlanPayload`, and `to_plan` checks it and gives it types (`Plan`), in the way extraction's payload becomes an `ExtractionResult`. A refusal raises `InvalidPlanError`, whose message names the step, the field and the problem, never a value.
- **The schema:** each step field is optional in it (`SkipJsonSchema[None]`) and required by code per operation (`FIELDS`). The project's cleaner (`response_json_schema`) now writes each nested model and enum out where it is used and drops `default`, so the plan's schema has no union, no `$defs`, no `$ref` and no `additionalProperties`. The cleaner walks schemas, not names, so a field called `title` or `default` stays. Extraction's two schemas come out as before (tested), so the frozen baseline's request is unchanged.
- **Details the spec left open:**
  - `$paste` names the owner's pasted text for `mail.read`. A plan that names it when nothing was pasted is refused.
  - One `timezone` for the whole plan. A search's `after` and `before` are dates, taken as midnight in that zone.
  - Guests are `guests` (addresses) or `guest_refs` (`contacts.find` steps), so no field takes two kinds of value.
  - The templates are D7's five. The decline codes are `cannot_send`, `cannot_change`, `email_says` and `out_of_scope`.
  - An answer runs at least one step, and a clarify or a decline runs none.
  - A template must have a step whose result it shows, and a plan that proposes answers with the `proposal` template.
  - Limits: three reads (D6), ten results a search, 200 characters of text, a 31-day range, a 24-hour slot.
- **One change to D3:** `slots.first_free` takes no window of its own. It looks within its free/busy step's range. A window wider than that range would count time never read as free.
- **Not yet:** whether the API accepts the schema is recorded once a development key exists (19.16).
- **Order:** 19.3's harness scores a plan by running it through the executor, so it is built after 19.6.
- 38 deliberate breaks, one per rule, each caught. 2462 tests pass, with lint and mypy clean.

### Task 19.4, two calendars (2026-10-04)

- **`CalendarClient`** has a read calendar, `primary` by default, beside the write calendar it had. Free/busy and the new `list_events` read the primary. Inserts, deletes and the approval's binding keep the write calendar. Only the graph and the smoke tool build a client, and neither names the read calendar.
- **Mail's cards change with this:** their conflicts now come from the owner's real calendar, not the test calendar. DEPLOY.md §14 says so (19.17).
- **Free/busy that cannot be read is never free.** Google reports an unreadable calendar as errors beside an empty busy list, and the client used to take that as free. It now raises `CalendarUnavailableError`, as it does when the calendar is missing from the answer.
- **`list_events`** reads one page of 50, earliest first, each repeat of a recurring event on its own, and says whether there were more. Each event has its title, its start and end, whether it lasts all day, its organiser, and whether Google says the organiser is the owner. It leaves out cancelled events and those the owner declined. All-day events run from midnight to midnight in the calendar's own zone.
- **Scopes:** `events.list` on the primary is covered by `calendar.events`, which the agent already holds.
- 12 deliberate breaks, each caught. 2476 tests pass, with lint and mypy clean.

### Task 19.5, the read operations and helpers (2026-10-04)

- **`app/planner/ops.py`:** `search`, `first_free` and `find_contact`. The calendar's reads are the client's own (19.4). Nothing here gives a value its origin; the executor does (19.6).
- **`mail.search`:**
  - Code builds the query. Each value the planner wrote goes inside quote marks with its own quote marks removed, so Gmail reads none of it as an operator. Dates become epoch seconds (`epoch_window`), since Gmail reads a bare date as Pacific midnight.
  - Each message found is read as metadata through the field mask: `From`, `Subject` and `Authentication-Results` only (`SEARCH_HEADERS`). It comes back with its sender's address and never the display name, Gmail's DMARC verdict (`participants.dmarc_passed`), whether the owner sent it (`SENT`), Gmail's own date, and the subject through `redact_secrets`.
  - A message that left the mailbox or went to the bin between the listing and the read is left out.
- **`slots.first_free`** offers the earliest free time inside working hours in the owner's zone. It starts on a quarter hour, never before now, and never outside its free/busy step's range. `WORKING_HOURS` (default `Mon-Fri 09:00-18:00`) is read at boot, so a typo fails there.
- **`contacts.find`, a detail beyond D3's "ignoring case and dots":** a name names a confirmed contact when each of its words is a word of the address's local part, or its letters are all of them. "Sara" finds sara.khan@, "Bilal Ahmed" finds bilalahmed@, and "Sar" finds nothing. A Gmail contact is stored without dots (`guest_key`), so "Sara" alone does not find sarakhan@gmail.com, and the owner types the address. Several matches or none end the turn with a fixed question (`Unclear`).
- **The fake mailbox** now reads quoted terms, `from:` and `to:`, and still refuses an unquoted word, which would be a bug in whatever built the query.
- 25 deliberate breaks, each caught. 2523 tests pass, with lint and mypy clean.

### Task 19.6, the executor, origins, and typed against pasted (2026-10-04)

- **`app/planner/origins.py`:** the four origins in D4's order, and `computed`, which gives a computed value the least trusted origin among its inputs. Typed addresses are found with `scrub.ADDRESS` and compared by `guest_key`, less the owner's own and their aliases. Pasted addresses are listed for an Allow, less the owner's and those the owner typed.
- **`app/planner/execute.py`:** `execute` runs a checked plan one step at a time and returns what ran, each result with its origin, what was refused and why, a question if a step needed the owner, and the pasted addresses.
- **D4's rules, checked before each step.** A read takes only a message a search found, or the paste. Free time is looked for only in the calendar's own free/busy. A slot is one the owner typed, or one the calendar showed free. A guest is an address the owner typed, or a confirmed contact, whether the planner wrote it or `contacts.find` found it. Each rule has a test on a plan built by hand, one `to_plan` would refuse, so the executor's check is shown to hold without the schema's.
- **A search's fields and a proposal's title** can only be what the planner wrote: no reference can fill them. The plan's types hold that rule, and a test holds the types.
- **When a step cannot go on:**
  - A step that breaks a rule is refused with a fixed reason, and so is every step that needs its result. The others still run.
  - A Gmail or calendar failure refuses its step; the log names only the exception's type.
  - A name that matched no confirmed contact, or several, ends the turn with its question.
- **Proposals are handed to `park`**, which 19.13 wires to the queue; nothing here writes to a calendar. The owner is never their own guest, so an invite of only the owner parks with no guests. A slot already past is refused, and so is a proposal whose free-slot step found nothing.
- **The reader** is a protocol here; 19.7 builds it.
- 23 deliberate breaks, each caught; one was missed at first, and a test was added for it. 2556 tests pass, with lint and mypy clean.

### Task 19.3, the routing golden set and its harness (2026-10-04)

- **Built after 19.6,** since a plan is scored by running it through the executor.
- **`data/planner/`:**
  - `world.json` is one world every case shares: 13 messages, 12 events, five confirmed contacts, and the owner asking on Wednesday 7 October 2026 at 10:00 in Karachi.
  - 64 cases in D10's seven categories: 21 single operations, 8 look-alikes, 8 scheduling in several steps, 5 irrelevant requests, 7 with missing details, 8 unsupported actions and 7 about what an email says. 26 must ask or decline.
  - Each case carries its ideal plan, which the harness's tests score. The README says how to write one, and that no case may be a worked example in the prompt.
- **`app/eval/planner.py`** scores each sample by what its run did:
  - **Operations:** the plan's operations, as a multiset, are one of the case's right sets.
  - **Outcome:** the facts the case names hold: the time a free/busy read covers, the free slot, the events listed, the messages found, the message read and what was asked of it, and each card's slot and its guests by address and source. Nothing else is refused or parked. Free text is never compared.
  - **Kind:** an answer, an ask (a clarify plan, or a step's question) or a decline, as the case says.
  - **The bar** is 95%, 90% and 100% for asks and declines. A must-ask case counts only when it also parked nothing. Wilson intervals are recorded beside each rate.
  - **The baseline** needs every case at five samples, no errors, and the bar met. Its hash covers the planner's call (the schema and check, the prompt, `llm.py`, the schema cleaner, the metered client), the world, the cases and the model. `.\tasks.ps1 planner-eval` runs it.
- **Built for it:**
  - `app/eval/mailbox.py` is the query reader the mail sync's fake and the eval share. A quoted word now also matches `From` and `To`, as Gmail's do.
  - The search takes any mail reader, so the eval runs the real query builder.
  - The planner's call (`make_plan`) and a draft prompt (`app/planner/prompts.py`), finished in 19.8.
  - `PLANNER_MODEL`, which `models.in_use()` now counts.
- **The run itself** waits for a development key and for 19.8.
- 26 deliberate breaks, each caught. Two were missed at first, and their tests were fixed. 2595 tests pass, with lint and mypy clean.

### Task 19.7, the quarantined reader (2026-10-04)

- **`app/planner/reader.py`:** a `Reader` serves one turn and reads at most three times. The fourth call raises `StepRefusedError`, now public so a tool can refuse a step, and the executor gives the turn its fixed reason.
- **What it reads:**
  - A message is prepared as the fetch prepares it (`scrub.prepare`). The reader is given only its own words (`split_quoted`), between markers made for the call (`email_block`), with the body cut to 16,000 characters.
  - The owner's paste is read whole, since the owner chose it, between the same markers.
  - Credential mail, and a paste that carries a code, never reach the model; the reading says why it was not read.
- **The question** rides in the system instruction (`read_system`), a channel no message writes to. The user turn is the message alone.
- **What it returns:**
  - Points and names are `Quoted`, labelled by code with the sender the search found: never by the fetched message's own `From`, and never by the model.
  - What the model wrote is cleaned as a card's text is (`shown`), on one line. It keeps at most five points of 300 characters and ten people.
  - An address must look like one. It is checked with `named()` against the message: in its own words, only in a quoted section, or not in it at all. For a paste, it is checked against the pasted text.
- **`origins.py`** gains `Quoted` and its two labels, and the extraction prompt's marker wrapper (`between`) is public, for the paste.
- **The structural test** now names `planner/plan.py` and `planner/reader.py` among the modules that call a model.
- 16 deliberate breaks, each caught. One was missed at first, and its test was extended. 2615 tests pass, with lint and mypy clean.

### Task 19.8, answers rendered by code, and the planner's prompt (2026-10-04)

- **`app/planner/render.py`** turns a plan and its outcome into plain text, with no model involved:
  - The reply's template shows its steps' results. A clarify asks its question; a decline gives its code's fixed sentence; a step's question (no contact, or several) is asked as it stands.
  - Each step that did not run gets a line saying why. The paste's addresses are listed, each for the owner's Allow, and `Answer.allow` carries them for the page's buttons (19.11).
- **Labels:**
  - Each message in a list, and each quote, says who sent it by address, Gmail's verdict on their domain, and when. The owner's own mail says "sent by you".
  - A name the reader gives is shown only as "written as", after the address. A forged display name never shows as the sender, since the search keeps the address alone.
  - A stranger's event shows its title in quotation marks, as an invitation from its organiser.
  - Calendar answers name the owner's primary calendar (decision 11).
- **Form:** text from mail or an event goes through `shown`, so it is one line with no link or code, and sits in quotation marks. Times are the owner's local times. Code writes no Markdown.
- **The prompt (`prompts.py`) is final:** the catalogue, which template goes with which steps, the conventions for days, weeks and "after 2pm", reading only the owner's calendar, and seven worked examples on a day of their own. Tests check that each example is a plan `to_plan` accepts, that the examples cover every kind, and that no example is a golden case. The baseline is taken against this prompt (19.16).
- **The phase's checkpoint:** a test runs golden cases' ideal plans on the eval's world (`play`, now shared with the eval) through to the rendered answer.
- 21 deliberate breaks, each caught. Two were missed at first, and tests were added. 2651 tests pass, with lint and mypy clean.

### Task 19.9, records, API and spending (2026-10-04)

- **`migrations/013_chat.sql`:**
  - `chat_turns` keeps each turn's key, its typed request and its paste apart, when it was asked, its status, the checked plan, the rendered answer and a fixed reason. Checks tie `started_at` to leaving the queue and `finished_at` to an end.
  - `processed_messages.source`, and `proposals.source` with `chat_turn_id`, tied by a check.
  - `web_reader`'s column grant. It is revoked first, so running the file again leaves exactly this grant. The grant tests apply the file itself before checking, since the database ran it only once.
- **The answer is stored as JSON**, holding its text, the paste's addresses and the cards. The page's Allow buttons come from the granted answer, so the grant is not widened beyond decision 12.
- **One change to D1:** the page sends its text as parts, each marked typed or pasted, rather than as character ranges. That spares index arithmetic across JavaScript's UTF-16 and Python's code points.
- **`POST /api/chat`:**
  - It answers 202 with the turn's id, and a resend with the same key gets the same turn.
  - Another zone is refused with 409 before anything is stored.
  - The typed request must not be blank and holds at most 2,000 characters; the paste holds at most 16,000 in all. A refusal names fields, never the text.
- **The chat check (`budget.ChatCheck`)** checks, in order: the pause, new work being allowed (the month's cap and every model priced), the owner's day of chat against `CHAT_DAILY_CAP_USD`, and the month leaving `MAIL_RESERVE_USD` for mail. Each stop has a fixed sentence. Like every gate's, the month's total may be a minute old; a turn's ceiling bounds what that minute could add.
- **The turn's own gate (`models.chat_gate`)** holds `CHAT_TURN_CEILING_USD`, and its calls are metered under `chat-<turn>`. The new settings are in `.env.example`.
- **Not yet:** the job that runs a turn, wakes on a new one and stops it by the check (19.10).
- 16 deliberate breaks, each caught. One was missed at first, and a test was added. 2684 tests pass, with lint and mypy clean, and the chat and grant tests pass on Neon (Postgres 18).

### Task 19.10, the chat job (2026-10-04)

- **`app/planner/chat.py`** runs a turn's life:
  - It claims the oldest queued turn, skipping any another worker has locked.
  - It plans from the typed request, runs the plan, renders the answer, and ends the turn: answered, stopped or failed, with the checked plan, the answer and a fixed reason.
- **The deadline and the chat check** run before the plan, before each read and park, and before each step: the executor gained a `before_step` hook. Past 90 seconds the turn stops, saying so.
- **What stops a turn, not a step:** the executor's new `TurnStoppedError`, and the spend gate refusing a reader's call. A refusal of a single step still leaves the others running.
- **How a turn ends:**
  - A plan the check refuses, or a model answer that is unusable, fails the turn as "could not plan this".
  - Anything else unforeseen fails it with a fixed sentence. Its trace keeps the exception's type.
- **Writes are conditional:**
  - A turn's end is written only while the turn is `running`.
  - A park runs inside a transaction that holds the turn `running` (`while_running`).
  - A turn the sweep has failed writes nothing more. A test sweeps one mid-turn to show it.
- **Stranded turns:** the job's sweep fails a turn left `running` past its deadline. The job's first tick runs at boot, so that sweep is boot's.
- **Metering and traces:** each turn's calls are metered under `chat-<turn>` (`CURRENT_MESSAGE`). It is one run in `runs`, with a span for the plan and one for each read.
- **No cards yet:** until the queue knows chat's proposals (19.12, 19.13), a proposal step is refused with a fixed sentence.
- **The scheduler's `chat` job** runs every 30 seconds, one tick at a time, and the API wakes it for a new turn (not for a resend). A tick with nothing queued opens no session, so it loads no credentials. Failures are recorded at most every five minutes.
- 17 deliberate breaks, each caught; two tests the list showed were missing were added first. 2705 tests pass, with lint and mypy clean, and the job's and API's tests pass on Neon.

### Review of tasks 19.1 to 19.8 (2026-10-04)

A fresh-context review of the planner, read against D2 to D7 and D10, found ten problems: one high, four medium and five low. It checked each with a script. All ten were accepted and fixed, and each fix has a test that a deliberate break showed fails without it.

- **High: a sender could forge an answer's label.** A subject could close its own quotation mark, write a fake "from ... (domain verified by Gmail)" label, and push the real label out of sight with blank-looking padding. The renderer now:
  - puts the label before what it labels, in a list of messages and on a stranger's event;
  - turns every quotation mark inside quoted text into an apostrophe;
  - removes characters that show nothing or turn text around;
  - cuts each quote to 300 characters.
- **Medium:**
  - A sender whose `From` is not a plain address, such as a quoted local part that spells another address, is now "an unknown sender", unverified. An organiser that is not an address is "an unknown organiser".
  - A plan that reads mail must answer with the `quote` template, as a plan that proposes must with `proposal`. Before, a read's answer could be dropped, and the eval scored the plan right.
  - Dates carry the year, so a message from last October is not yesterday's.
  - In the eval, a case that must ask or decline counts only when it also ran just the steps it lists. Reading the mail and then asking is acting on it.
- **Low:**
  - Search leaves out drafts and chats, as the mail sync's queries do.
  - Free/busy across several days says each busy time's day, and an all-day event says its day.
  - A hold's card says "with no guests" rather than naming the primary calendar, since writes go to the write calendar (D3).
  - A card parked before a step's question is still shown with the question.
  - `call-sara-if-free` now accepts only the plan that checks before it books.
- 16 deliberate breaks, each caught. One was missed at first, and a test was added. 2718 tests pass, with lint and mypy clean.

### Task 19.11, the chat page (2026-10-04)

- **`/chat`, "Ask" in the web app's menu,** lists the last 20 turns, oldest first. For each it shows the typed request, its status while open, its answer or its reason, an Allow for each address from a paste, and a link to the timeline when a card was made. Turns are read through `web_reader`'s grant, which excludes the paste and the plan.
- **What was pasted (`src/lib/chat.ts`):** the box keeps a mark beside each character. A change is found by what the old and new text share at each end, and the characters it put in are marked pasted when the change was a paste or a drop. Characters around the change keep their marks. The request goes as runs of typed and pasted text (19.9).
- **The key** is made in the browser at the first send and kept until Fly has the turn, so a send that timed out and is sent again finds the same turn. The box is locked while a turn is open. The page re-reads every two seconds, then every ten after thirty.
- **Plain text:**
  - Answers are React text. A test scans the page and its components for any way to make markup from text.
  - A content policy on `/chat` lets the page load images from the app alone.
- **Allow from a paste** posts the address with no proposal named. Fly makes it a confirmed contact, which the owner can then name.
- **`OWNER_TIMEZONE`** is sent with each request, and Fly refuses any other zone. The web app's `.env.example` and README say so.
- **The phase's checkpoint:** a test posts a question to the API on Postgres, runs the job on fakes, and reads back the rendered answer.
- 13 deliberate breaks on the web side, each caught; one was missed at first, and its test was fixed. 173 web tests pass, and typecheck, lint and build are clean. 2719 Python tests pass. The browser check waits for the owner's end tests.
