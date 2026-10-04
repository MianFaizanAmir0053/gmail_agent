# The planner's routing golden set (M19, D10)

`app/eval/planner.py` runs these. Each case is a request the owner might type;
the eval asks the planner for a plan, runs it with the real executor against
fakes of one shared world, and scores what happened.

**Never use a case as a worked example in the planner's prompt.** The set
measures the prompt, and a case it was shown measures nothing.

## The world: `world.json`

One mailbox, one calendar and one list of confirmed contacts, shared by every
case, and the moment the owner asked: Wednesday 7 October 2026, 10:00 in
Asia/Karachi.

- `events`: wall-clock times in the world's zone. An event with no
  `organizer` is the owner's own.
- `messages`: `at` is wall-clock in the world's zone, and becomes Gmail's
  internal date. `dmarc: true` adds Gmail's own `Authentication-Results`
  passing DMARC for the sender's domain. `labels` default to `INBOX`.
- Search in the fake reads quoted words in the `Subject`, `From` and `To`
  headers only (`app/eval/mailbox.py`), so write subjects that make a case's
  answer clear without a body.

## A case: `routing/<id>.json`

- `id`, the file's name; `category`, one of D10's: `single`, `lookalike`,
  `multistep`, `irrelevance`, `missing`, `unsupported`, `email`.
- `typed`, the owner's request; `pasted`, text the owner pasted, if any. The
  planner never sees `pasted`, only that there is some.
- `expect`:
  - `kind`: `answer`, `ask` or `decline`, or a list of those that are right.
    `ask` is a clarify plan, or a step that ends the turn with a question.
  - `ops`: every multiset of operations that is right, as lists. A case that
    must ask or decline runs none unless it says so.
  - What the run must find, each optional: `busy_at` (a free/busy read of at
    most a day covers this time), `free_slot` (`[start, end]`), `events`
    (titles listed), `messages` (ids found, newest first), `newest` (the first
    id found), `reads` (`[id or "$paste", ask]`), `proposals` (each card's
    `start`, `end` and `guests` as `[address, "typed" or "contact"]`). Nothing
    beyond these may be refused or parked.
- `ideal`: the plan a perfect planner writes. The harness's own tests score
  it, renumbered and broken, so a case that cannot be met fails CI.

Free text -- search words, titles, a clarify question -- is never scored.
