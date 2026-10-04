# M19 · The planner

**Est.** 7 days · **Depends on** M16, M17, M18 · **Blocks** M22 (connector gateway), M25 (memory)

Plan and decisions: [`ASSISTANT-PLAN.md`](../../ASSISTANT-PLAN.md), sections
"Security model" and "What the adversarial review changed".

**Status: draft, revised after one adversarial review round (see Review) and
the owner's fourteen decisions. Waiting for the owner's approval; nothing is
built before it.**

## Goal

The owner asks in their own words, in the web app, and the agent answers or
proposes:
- **questions about mail and the calendar** are answered: "when did Sara last write?", "am I free Thursday at three?", "what's on tomorrow?";
- **scheduling from chat** becomes a card with the same bound Confirm as a proposal from mail: "book thirty minutes with sara@example.com next week";
- **the model that plans never reads mail**, and the model that reads mail holds no tool: the planner sees the owner's words and nothing else, plans once, and never sees a result;
- **a tool-routing eval comes first**, and the planner ships only if it passes.

**What M19 cannot promise.** It cannot make an answer drawn from mail true: a
sender can lie, and the agent reports what the mail says, labelled with who
sent it and whether Gmail verified their domain. It promises that nothing a
sender writes can choose what the agent does, and that what the owner is told
from mail always says where it came from.

## Why this comes next

The code as of `118f9a8` (mapped on 2026-10-04):
- **There is no chat.** The web app shows proposals made from mail, and the owner can only decide them (`dashboard/src/app/page.tsx`).
- **The approval path is built around a Gmail message and its graph.** `proposals.message_id` is a foreign key to the ledger; the worker's step table reads the message's LangGraph checkpoint (`step_for`); the recipient rule asks Gmail for the message's thread (`contacts.unconfirmed_outsiders`).
- **The registry knows one argument shape.** `Registry.execute` takes `CreateEventInput`, and `bound()` validates a parked `proposed` as an `ExtractionResult` (`app/policy/`).
- **The tool loop is plan-and-act.** `structured_call` feeds each tool's result back into the same model (`app/extraction/llm.py`). M18 removed every caller; the loop and `search_context` stayed "for M19". M19 does not need them.
- **The gate has one ceiling.** `Gate.check` compares a message's spend with `MESSAGE_CEILING_USD` ($0.50); outside a graph run only the monthly cap applies (`app/policy/budget.py`).
- **The calendar client points at the test calendar,** and can only answer free/busy: there is no way to list events (`app/google/calendar.py`).
- **The web app's database role reads no mail content** (migration 008).
- **There is no routing eval.** The plan requires one before the planner (`ASSISTANT-PLAN.md`, key assumptions).

## Decisions (4 Oct 2026)

The owner took the recommended choice for each, over four rounds. The third
followed the research, and the fourth followed the adversarial review.

1. **First jobs:** answering questions about mail and the calendar, and scheduling from chat. Drafting replies and the daily brief wait.
2. **Surface:** a chat page in the existing web app, behind the same Google sign-in, on both phones.
3. **Mail facts come through a quarantined reader:** a tool-less model reads a thread and returns fixed, typed fields; the planner never sees email text (the dual-LLM pattern).
4. **Approvals: one queue, two sources,** by an additive migration (D8). The schema change is approved with this spec.
5. **Guests in a chat invite:** an address the owner typed counts as theirs for that proposal, and the card says so. An address found in mail needs an Allow, as M17 requires.
6. **Search:** Gmail's own search, built by code from typed fields. No ingestion, no embeddings.
7. **The eval bar, in two steps.** The planner ships behind `DRY_RUN` on about 60 cases: the right operations at least 95% of the time, the right arguments at least 90%, and every request it must refuse or ask about handled correctly (100%), five samples a case. Before `DRY_RUN` goes off, the set grows to about 150 routing cases, three runs each, plus 100 cases that tempt a wrong write, with none allowed. 48 right out of 50 shows a true rate anywhere from 86.5% to 98.9%.
8. **The plan is a JSON document of fixed steps,** from one model call; code runs it and checks every type; no result ever goes back to the model.
9. **Answers are rendered by code.** What came from mail is labelled with where it came from. No model writes the final answer.
10. **A ceiling of $0.10 per chat turn.** A turn that reaches it stops and says so.
11. **The planner reads the owner's primary calendar.** It does this within the scopes already granted, and answers name the calendar. Where writes go stays M17's open question, decided before `DRY_RUN` goes off.
12. **The web app may read chat answers.** A column-level grant gives `web_reader` a turn's status, its rendered answer and its reason. Answers are scrubbed and labelled, and cleared after their cards settle (D12). This is a deliberate change to M16's rule that the web app reads no mail content.
13. **Pasted text is not the owner's.** An address counts as typed only if code finds it in a part of the request that does not look pasted (D5). One from pasted text is labelled "from text you pasted" and needs an Allow.
14. **Two cuts from the first version.**
    - **No follow-ups:** each request stands alone.
    - **No times taken from mail:** a slot comes from the owner or from free/busy. The mail pipeline already proposes a sender's time.

A default set with this spec, open at approval: **a daily chat cap of $1.00**
(D9). Twenty turns a day at the ceiling would otherwise cost $60 a month,
above the $40 monthly cap.

## Scope

**In:**
- a chat page, its records, and an asynchronous turn: the request is stored, a job answers it, and the page shows the answer;
- the planner and its operations: search mail, read a thread (through the reader), list events, free/busy, find a free slot, find a contact, propose a hold, propose an invite;
- proposals from chat on the queue mail uses, with the owner-typed guest rule;
- spending: a ceiling per turn, a daily cap;
- the tool-routing golden set and its eval, before the planner;
- an injection suite for chat;
- removing the tool loop and `search_context`, which nothing needs.

**Out:**
- follow-ups and conversation history; times taken from mail (decision 14);
- sending, replying, drafting, labelling, archiving (wider scopes; later modules);
- the daily brief and loose ends (M21);
- memory (M25), other sources (M22, M23), a contacts provider (M23);
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
  - Gemini 3 is meant to run at temperature 1.0.
- **Evaluation.**
  - BFCL's categories: simple, multiple, parallel, irrelevance (no call is right), and multi-turn (missing parameters means ask; missing functions means decline).
  - τ-bench's pass^k: a task counts only if it succeeds on every one of k runs.
  - Below a few hundred items, use Wilson intervals.
  - Zero failures in n cases bounds the failure rate at about 3/n.
- **Prices** (official page, updated 2026-10-01): 3.6 Flash costs $0.75 per million input tokens and $3.75 per million output, doubling on 2027-01-01.

---

## Design

### D1. A chat turn

- **The page.**
  - `/chat` in the web app, owner-only like every page. The owner types a request of up to 2,000 characters.
  - A server action checks the session and posts it to Fly (`POST /api/chat`) with an idempotency key made by the page, unique in `chat_turns`. A resend after the web app's 10-second abort finds the turn already stored, and never makes a second.
  - Fly stores the turn and answers 202 at once.
  - While a turn is open the box is locked, and the page re-reads every two seconds, then every ten after thirty seconds.
- **The record.** `chat_turns`:
  - id and idempotency key;
  - the owner's text, and when it was asked;
  - status: `queued`, `running`, `answered`, `stopped` or `failed`;
  - the validated plan;
  - the rendered answer;
  - a fixed reason when it stopped or failed;
  - when it finished.
- **Running it.**
  - **The job.** A scheduler job, `chat`, with `max_instances=1`, runs queued turns one at a time. The API wakes it as `wake_decisions` wakes the worker, and it also ticks every 30 seconds.
  - **The deadline.** A turn has 90 seconds, inside the process's kill timeout.
  - **Writes are conditional.** Every status write applies only while the turn is `running`, and a park happens in the same transaction as it.
  - **Stranded turns.** A turn found `running` past its deadline, at boot or by the job, is marked `failed` ("stopped by a restart"). Nothing it would have parked is parked afterwards. There is no retry: the owner asks again.
- **Each request stands alone** (decision 14): the planner sees no earlier turn.

### D2. The planner: one call, one plan

- **The call.** `app/planner/plan.py` makes one structured call: `structured_call` with a schema and no tools, at temperature 1.0. It returns a `Plan`.
- **The schema is flat,** because the endpoint rejects much of JSON Schema. A plan has:
  - `kind` (`answer`, `clarify`, `decline`);
  - `steps`: at most four, each an operation from an enum, with optional typed fields; a reference is a string of the form `$s1` or `$s1.slot`;
  - `reply`: a template name, a question, or a decline code.

  The schema is tried against the API before anything is built on it, and the baseline's hash covers it.
- **The planner's input.**
  - The system instruction holds the catalogue of operations, their types and the conventions. It says that the planner never sees results. Its worked examples are not in the golden set.
  - The user turn holds the grounding block, taken at the moment the owner asked (`asked_at`, so "tomorrow" means the owner's tomorrow), and the owner's words. It holds nothing else.
- **Times.** The planner writes local times with a zone, as extraction does, and code converts them. One zone setting serves the planner and the web app: `USER_TIMEZONE`, which the web app's `OWNER_TIMEZONE` must equal (checked at boot).
- **Validation, before anything runs.** The plan is refused for an unknown operation, a wrong type, a reference that points forward or to nothing, or more than four steps. The turn then fails as "could not plan this", and nothing runs.
- **The model.** `PLANNER_MODEL`, by default production's extraction model. It must be priced, it is in `models.in_use()`, and it has a pipeline version of its own, as extraction does.

### D3. Operations

**Reads** (T0, run by code, never by a model):
- **`mail.search(from?, to?, words?, after?, before?, limit ≤ 10)`.**
  - It returns message refs: id, thread, sender address, whether Gmail verified the sender's domain (`dmarc_passed`), date, and the subject as quoted text.
  - Code builds the Gmail query; the planner never writes Gmail syntax.
  - A search's subjects pass through `redact_secrets` before they are kept, since a metadata read never sees a body and cannot tell credential mail.
- **`mail.read(ref, ask)`** hands one thread to the quarantined reader (D6). `ask` is a fixed set: `asks_of_owner`, `people`, `summary`.
- **`calendar.list_events(start, end)`** reads the primary calendar (decision 11). Each event comes back with its start, its end, its organiser, and its title as quoted text. This is a new `CalendarClient` method, within the `calendar.events` scope.
- **`calendar.freebusy(start, end)`**, on the primary calendar.

**Helpers** (pure code, no I/O):
- **`slots.first_free(busy, window, minutes)`.** Working hours come from a setting, `WORKING_HOURS`, Monday to Friday, 09:00 to 18:00 by default.
- **`contacts.find(name)`** looks only in confirmed contacts and among senders whose domain Gmail verified. It returns choices, never a guest: the planner cannot bind a list to a guest field. With one choice the card offers it; with several, the answer lists them for the owner to tap (D7). The address chosen keeps the origin mail.

**Proposals** (never run without the owner's Confirm): `calendar.propose_hold(title, slot)` (T1) and `calendar.propose_invite(title, slot, guests)` (T2). Each parks a proposal on the queue (D8).

### D4. Origins

Every value carries an origin:
- **owner:** the owner's words, and what the planner wrote from them;
- **calendar:** the owner's calendar's times;
- **mail:** anything that came from mail.

A value an operation computes takes the most untrusted origin among its inputs, where mail is below calendar, and calendar is below owner. A `first_free` over a window from mail returns a slot from mail.

The rules, checked by the executor (`app/planner/execute.py`) before each operation:
- **A read's arguments** are owner or calendar values, or a ref returned by a search. No text from mail ever becomes a query.
- **A title** comes from the owner.
- **A slot** comes from the owner or the calendar (decision 14).
- **A guest** is either:
  - an address the owner typed (D5);
  - a confirmed contact;
  - one address the owner picked from a `contacts.find` list, which keeps the origin mail and needs an Allow.
- **Text from mail or from an event** is a `Quoted` value. It is shown to the owner, labelled, and accepted by nothing else.

A step that breaks a rule fails with a fixed reason. The turn answers what it could, and says what it would not do.

### D5. What the owner typed

Code decides, not the planner:
- **Addresses.** It finds the addresses in this turn's raw text with the scrubber's own pattern (`scrub.ADDRESS`), and drops the owner's own and their aliases.
- **A guest is "named by you"** only when its `guest_key` matches one of those addresses. An address the planner wrote that the owner never typed is neither typed nor allowed: it fails D4.
- **Pasted text** (decision 13). A request that has header lines (From:, To:, Subject:, Date:), lines quoted with `>`, or more than three addresses, has its addresses treated as pasted. They are labelled "from text you pasted" and need an Allow.

### D6. The quarantined reader

`app/planner/reader.py`:
- **What it reads.** A model with no tools reads one thread, prepared as the fetch prepares it (`scrub.prepare`), between per-call markers (M18, D3). It answers one fixed question into a fixed schema.
- **What it returns** has origin mail. Its prose is `Quoted`. The addresses it names pass through M18's `named()` against that thread: one the thread does not contain is marked "not found in the email", with M18's warning beside Allow.
- **Limits.** A turn calls it at most three times, and a thread is cut to the prompt's room.
- **No tools.** The structural test that keeps tools out of `app/extraction/` (M18, D4) covers it.

### D7. Answers, rendered by code

- **Templates.** A plan's `reply` names a template over its steps' results:
  - free or busy at a time;
  - a list of events;
  - the last messages from someone;
  - a quote from the reader;
  - a choice of contacts to tap;
  - a proposal made ("see the card").
- **Labels.**
  - Everything from mail is labelled with the sender's address, whether Gmail verified their domain, and the date. This includes typed values, counts and lists, not only quotes.
  - A display name is never shown as who someone is.
  - An event whose organiser is not the owner shows its title quoted, under its organiser: invitations from strangers reach the calendar by default.
- **Cleaning.** `Quoted` text passes through `shown()` before it is stored.
- **Plain text only.** Answers render as plain text, with no Markdown and no links made from text. The chat page sets a content policy that loads no remote image.
- **Clarify and decline.** A `clarify` reply asks the planner's question, made from the owner's words alone. A `decline` reply gives its fixed reason: "sending email is not something I can do", "I don't act on what an email asks", and so on.

### D8. Proposals from chat: one queue, two sources

- **The migration** (`013_chat.sql`, additive):
  - `chat_turns` (D1, D12);
  - `processed_messages.source` (`mail` or `chat`, default `mail`). A chat proposal has a ledger row under the id `chat:<turn>:<step>`.
  - `proposals.source` and `proposals.chat_turn_id`, which `web_reader` can read.
- **No graph.**
  - A chat proposal parks straight from the executor: its `proposed` is an `ExtractionResult`, so `bound()` and `event_args` hold it unchanged. The event's description is the fixed one, with the `chat:` id.
  - Conflicts are checked at park, as `detect_conflicts` does for mail.
  - The worker gains a step for source `chat`: the Confirm's checks, then `Registry.execute` from the stored proposal, then the ledger mark. A Cancel or a sweep ends the proposal.
  - **Edit is refused** for source `chat`, in `decide()`, from every channel, Telegram's reply-to-correct included. The owner asks again instead.
- **The recipient rule, by source.** `Outsiders` dispatches on the source. For chat, the guests that count as inside are:
  - the addresses typed in the stored turn's text (D5), re-derived at every check;
  - confirmed contacts.

  Gmail is not asked.
- **The card** shows:
  - the request it came from;
  - where the title came from;
  - each guest's source: "named by you" and "from text you pasted" join M18's;
  - its conflicts.
- **M24's evidence** leaves chat proposals out. The owner's own requests, approved almost always, must not count toward autonomy for mail.

### D9. Spending

- **A chat gate with its own ceiling,** $0.10 a turn (`CHAT_TURN_CEILING_USD`). During a turn `CURRENT_MESSAGE` is `chat:<turn id>`. A turn can overshoot by one call, since a call's cost is known only after it.
- **A daily chat cap,** $1.00 by default (`CHAT_DAILY_CAP_USD`). The monthly cap still applies.
- **Checks.** `allows_new_work()`, which covers Pause and the caps, is checked before the plan call, before each reader call, and before each park. A turn stopped by one says why.
- **What a turn costs.** A plan call is about 4,000 tokens in and 400 out, and a reader call about 6,000 in and 500 out. At today's 3.6 Flash prices a typical turn costs one to three cents, two to six cents at 2027's. The routing eval records the real figures.

### D10. The tool-routing eval, first

- **Cases.** `data/planner/routing/` holds about 60 cases. Each is the owner's words, an instant and a zone, and the plan expected: its kind, its operations, and its key arguments.
- **Categories:**
  - each operation on its own;
  - look-alike operations;
  - scheduling in several steps;
  - irrelevance;
  - missing details, where the planner must ask;
  - unsupported actions (send, delete, forward, pay), where it must decline;
  - "do what this email says", where it must decline;
  - pasted text.
- **The run.** `app/eval/planner.py` and `.\tasks.ps1 planner-eval`: five samples a case, on the planned model.
- **The bar:**
  - the right operations at least 95% of the time;
  - the right arguments at least 90%;
  - every case where it must ask or decline handled correctly (100%).
- **Committed like M18's injection run:** a baseline tied by a hash to the code that shapes the planner's prompt, which CI checks.
- **Before `DRY_RUN` goes off,** the set grows (decision 7).

### D11. The injection suite, for chat

The cases are mail that search will find and that tries to steer what the owner is told or asked:
- "add this address";
- "the owner wants this";
- a forged sender name;
- a quoted chat reply.

The planner never sees mail text, so no plan can change. Each attack is still driven through the executor, with a compliant fake reader that does whatever the mail says:
- no proposal carries a title, a slot or an unmarked guest from mail;
- no label is missing;
- no display name is shown as identity;
- nothing is written without the owner's Confirm.

The model run carries the same cases, checking the reader's quoted output for code and link markers.

### D12. Records, grants and retention

- **The web app's grant** (decision 12). `web_reader` may read `chat_turns`' id, status, asked time, rendered answer and reason. It may not read the owner's text or the plan.
- **Retention.** A turn's text, plan arguments and answer are cleared a week after the turn finished, and only once every proposal it made has settled. A pending card still needs the typed addresses. The plan's structure stays, for statistics.

### D13. Removing the tool loop

`structured_call`'s tool loop (`tools`, `dispatch`, the turn limit), `app/tools/search_context.py` and its tests go. M18 kept them "for M19", and M19 needs neither. Once they are gone, "no result goes back to a model" is a property of the code, and the structural test asserts it.

---

## Deliverables

- **Python:**
  - `app/planner/`: plan, execute, origins, reader, render, the chat job;
  - `CalendarClient.list_events`, and reads from the primary calendar;
  - the chat API in `app/web_api.py`;
  - the worker's step for chat;
  - the recipient rule by source;
  - Edit refused for chat in `decide()`;
  - the chat gate and daily cap;
  - `app/eval/planner.py`;
  - removing the tool loop.
- **Web:** `/chat`, its server action and content policy; the request and the new guest sources on cards.
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

- **The plan:** the schema accepted by the API (once, recorded); validation refuses an unknown operation, a wrong type, a bad reference and a fifth step.
- **Origins:** each rule in D4, with a test that fails without it. That includes a value computed from mail inputs, and a list bound to a guest.
- **What the owner typed:** typed, invented, pasted, and the owner's alias.
- **The reader:** no tools; its prose quoted; an address it names that the thread lacks is marked; three calls at most.
- **Rendering:**
  - every template;
  - labels on every value from mail;
  - display names never shown as identity;
  - stranger organisers quoted;
  - plain text.
- **The chat path, on Postgres:**
  - stored, run and answered;
  - an idempotent resend;
  - a stranded turn failed, with nothing parked after;
  - the deadline;
  - Pause and the caps at each check.
- **Proposals from chat, on Postgres:**
  - through `decide()`, the worker and the registry;
  - a typed guest needs no Allow, and a found or pasted one does;
  - Edit refused from every channel;
  - M24 leaves them out.
- **The routing eval:** the harness with a fake model, and the baseline gate.
- **The chat injection cases:** a compliant fake reader through the executor.
- **Removal:** the structural test that no `tools` argument exists.

## Boundaries

- **Always:**
  - plan before reading anything;
  - check every value's origin before every operation;
  - render answers in code, labelled;
  - keep mail text out of the planner's context.
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
3. "Book 30 minutes with <address> next week" parks a card whose guest is "named by you". Asked with a name, it offers the contact to tap, and the guest then needs an Allow.
4. A planted email that tries to steer, found by a search, changes no plan. Its text shows only as a labelled quote, with no display name taken as identity.

## Open questions

- **Names to addresses.** `contacts.find` is limited to confirmed contacts and verified senders. A contacts provider (M23) would answer better.
- **Follow-ups**, and choices that need a plan to read a result ("book the second one"). A second planning round over typed refs only is the likely shape, after M19.
- **Where writes go:** M17's open question, before `DRY_RUN` goes off.

## Review

One adversarial round, in fresh context, on 2026-10-04, against the code. It
made 23 findings, all accepted; the design above carries their fixes.

- **Security, five:**
  - "Typed by the owner" was undefined, and a paste got round it (D5, decision 13).
  - Origins were lost through helpers and references (D4).
  - Code stated sender-controlled facts as fact (D7's labels; display names).
  - Quoted text was never cleaned before it reached the screen (D7).
  - "Found in your mail" hid how it was found (D6's `named()`).
- **Fit with the code, eight:**
  - The worker could not apply a chat decision without a graph (D8: a step of its own).
  - The guest check asked Gmail for a chat id (D8: by source).
  - Event arguments and the description (D8: unchanged).
  - `web_reader` could not read the source, and M24 would have counted chat (D8, D12).
  - Retention would have cleared what a pending card needs (D12).
  - The 10-second abort could have duplicated turns (D1: an idempotency key).
  - The gate had one ceiling (D9).
  - Reads would have used the test calendar (decision 11).
- **Gaps, six:**
  - where Pause and the caps are checked, and the deadline (D1, D9);
  - concurrency and polling (D1);
  - time zones and working hours (D2, D3);
  - the plan schema on Gemini (D2: flat, tried first);
  - what a chat card shows, and Edit refused (D8);
  - the monthly cost (D9's daily cap).
- **Over-design, four:**
  - follow-ups cut;
  - times from mail cut;
  - the tool loop deleted (D13);
  - `contacts.find` limited, and plans capped at four steps.

## Running notes

### Specified (2026-10-04)

- Four rounds of the owner's decisions, a research agent's cited summary, a codebase survey, and one adversarial review.
- Approval comes next, then the tasks.
