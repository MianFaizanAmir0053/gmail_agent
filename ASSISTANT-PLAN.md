# Universal Personal Assistant: Plan (v2)

`MASTER-PLAN.md` describes v1: a single-user Gmail-to-Calendar agent built as a
portfolio project (modules M00–M14). This document plans v2: growing that agent
into a universal personal assistant that connects to the owner's apps, keeps
track of every loose end, and acts on the owner's behalf.

It holds the decisions, the capability map, and the project-wide rules. Each
module gets its own spec in `docs/plans/M##-*.md`; tasks for the module being
built live in `tasks/todo.md`. Evidence behind the decisions is in
[`docs/research/assistant-landscape-2026-09.md`](docs/research/assistant-landscape-2026-09.md).

---

## Decisions

Settled in the planning session of 30 Sep 2026.

| Topic | Decision |
|---|---|
| Goal | **Me first, product later.** Owner-only until it measurably saves the owner time. Multi-user is out of scope for v2. |
| Direction | **Universal agent core**: a chat planner, a tool registry, a connector gateway. |
| First proactive job | **Loose ends**: replies owed, promises made in sent mail, promises owed to the owner, deadlines, money. Tracked to closure. |
| Channel | **Own web app with push**, built on the existing Next.js dashboard. Telegram is blocked on the owner's network (see M06 notes) and the WhatsApp Business API prohibits general-purpose AI assistants. Telegram code stays as an optional adapter. |
| Apps wanted | Gmail, Calendar, Drive/Docs/Sheets/Contacts, chat (Slack; WhatsApp and Telegram are not readable, see Not doing), work tools (Notion, ClickUp, Jira, GitHub), business (invoices, Stripe, CRM), Outlook / Microsoft 365, social (LinkedIn, X, Instagram), freelance (Upwork, Fiverr). |
| Autonomy | **Earned per action type**, reversible internal actions only. Anything that leaves the owner's account (send, invite, share) always asks. |
| Payments | **Never.** The assistant tracks money and reminds; payment happens in the bank's or Stripe's own app. |
| Budget | **$20–50/month** all-in. Gemini Flash prices double on 1 Jan 2027, so budgets are computed at 2027 rates. |
| Models | Gemini paid tier for every call. Free-tier prompts may be used to improve Google's products, including human review, so real mail never goes through it. The planner model changes only if the tool-routing eval says so. |
| Token custody | **Composio with the owner's own OAuth apps** for non-Google apps, behind our own tool registry. Self-hosted Nango is the exit path. Gmail, Calendar and Drive stay on our own Google code. |
| Build order | **Thin core first**, loose ends as the first job (see the capability map). |
| Pace | 20+ hours per week. |
| Docs | Specs in `docs/plans/`, the active task list in `tasks/todo.md`. |

---

## One-pager

### Problem statement

How might we give one busy person a single assistant that sees their apps,
tracks every loose end, and acts on their behalf — earning autonomy with
measured evidence instead of asking for blind trust?

### Direction

Meta Muse, Google Gemini Spark, OpenAI Dots and xAI's Grok Bot already sell a
"do anything" agent for $0–30 a month. v2 does not try to out-feature them. For
the owner, the value is control, privacy and a system whose behaviour is
measured. If it ever becomes a product, the differentiators are the same four
things the market lacks:

1. **Receipts.** Every item links to its sources; accuracy per category is measured and shown.
2. **Autonomy earned by statistics.** An action type runs unattended only after its measured error bound is low enough, and it loses that right on the first error.
3. **Hardened against hostile input by construction.** An injected email cannot cause an outbound action without an approval, and the attack success rate is measured, not claimed.
4. **Budget-capped, exportable, self-hosted.** Nobody can shut it down or delete the owner's data. In 2026 alone, OpenAI removed Agent mode, Pulse and Atlas, Google shut Mariner and CC, Relay.app shut down and deleted its users' data, and Manus deleted eight months of user data.

### Key assumptions to validate

- [ ] **Loose ends cost the owner real time.** M15 measures threads awaiting the owner's reply for more than 48 hours, over two weeks.
- [ ] **Closure is detectable from threads.** M21's golden set sets the target: at least 90% precision on "still open".
- [ ] **The budget holds at 2027 prices.** M15 measures real volume and cost; a hard cap enforces it from M17.
- [ ] **Gemini Flash routes tools reliably.** M19's tool-routing eval must pass before the planner ships.
- [ ] **Enough approvals accrue to unlock autonomy** — roughly 60 clean approvals per action type for a 5% error bound. Counted from M16 onward.
- [ ] **No injected content produces an unapproved outbound action.** Enforced in code (M17) and tested by M18's injection suite.

### MVP scope

M15–M21: go live, the web channel, the action policy, untrusted-input hygiene,
a planner with a handful of Google tools, full mailbox sync, and the loose-ends
ledger with a daily digest. Everything else waits until the MVP has been used
daily.

### Not doing (and why)

- **Reading WhatsApp or iMessage.** There is no compliant API for personal accounts, and the WhatsApp Business API bans general-purpose AI assistants outside a few regions.
- **Reading Telegram chats.** Bots cannot read private chats, and Telegram is blocked on the owner's network.
- **Executing payments.** Money moves in the bank's own app; the assistant only reminds.
- **Model-accessible permanent deletes, forwarding or filter rules, permission, sharing or OAuth changes.** These are the actions attackers want; they are never registered as tools.
- **Third-party skills, marketplaces, or unpinned MCP servers.** The largest single source of agent incidents in 2026 (malicious ClawHub skills, the postmark-mcp BCC package).
- **A cloud VM with computer use as the core.** Real-task success rates are 30–60%. A sandboxed browser fallback comes last (M27).
- **Personas and companions.** Safety and brand risk with no productivity value.
- **Multiple users.** A hosted multi-user Gmail product needs an annual CASA assessment and multi-tenancy.
- **Voice.** Later, if at all.

---

## Capability map

Module ids are stable; specs and tasks refer to them.

| Id | Responsibility | Depends on | Est. |
|---|---|---|---|
| [M15-go-live](docs/plans/M15-go-live.md) | Harden the deploy (reproducible image, liveness, visible retries, checkpoint retention); observe mode on the paid tier; OAuth "In production" proven by evidence; measure volume, loose ends and cost | — | 5d |
| [M16-web-channel](docs/plans/M16-web-channel.md) | Owner-only web app on Vercel: timeline, approval cards, web push, Google sign-in; one recorded decision queue applied by a single worker; Telegram as an optional adapter | M15 | 8d |
| M17-action-policy | Tool registry with risk tiers enforced in code; `DRY_RUN` in the registry; outbound-action table with single-use nonces bound to an argument hash and to the `DRY_RUN` state a proposal was made under; recipient rule; a deterministic calendar event id, so an interrupted `act` can be re-driven without double-booking; fail-closed budget cap; `/pause`; audit log. `DRY_RUN` stays on until this lands | M15 | 4d |
| M18-untrusted-input | Strip one-time codes and reset links; structurally separate owner input from content; forwards untrusted; tool-less readers; injection suite gating CI; re-index and re-run evals | M17 | 4d |
| M19-planner | Chat planner, plan-then-execute, about six Google tools; tool-routing eval first; tools executed by our own node | M16, M17, M18 | 5d |
| M20-mail-sync | Full mailbox sync through `history.list` including SENT; cursor resync; per-source item tables; recall check | M15 | 3d |
| M21-loose-ends | Golden set with closure labels, then extraction, closure tracking, ledger view and daily digest | M16, M18, M20 | 7d |
| M22-connector-gateway | Composio (own OAuth apps) and pinned MCP servers as registry providers; connections table; tool catalog in pgvector with top-K retrieval | M17, M19 | 4d |
| M23-connectors-1 | Drive/Docs/Sheets/Contacts; Outlook / Microsoft 365; the owner's Slack (internal app), Notion, GitHub, ClickUp/Jira. Each approve-only, with routing evals, feeding loose ends | M21, M22 | 5d |
| M24-earned-autonomy | Reversible actions only; unlock by error bound; random audits; reset on model or prompt change; grant/revoke view | M17, M21 | 3d |
| M25-memory | Owner-only writes; provenance and expiry on every memory; view, edit, forget; retrieval into the planner | M19 | 4d |
| M26-money-readonly | Invoices, Stripe and CRM, read-only, feeding loose ends; reminders only | M21, M22 | 3d |
| M27-browser-fallback | Sandboxed browser for apps without an API (candidates: social and freelance platforms); no logged-in sensitive sessions; step and cost caps | M17, M22 | 5d |

### Build order

1. M15
2. M16, M17, M20 — any order
3. M18
4. M19
5. M21 — **MVP complete** (about six weeks at 20+ hours per week)
6. M22
7. M23
8. M24, M25
9. M26
10. M27

```
M15 ─┬─► M16 ─────────────────┬─► M19 ─┬─► M22 ─┬─► M23
     ├─► M17 ─► M18 ──────────┤        │        ├─► M26
     │                        │        └─► M25  └─► M27
     └─► M20 ─────────────────┴─► M21 ─┬─► M23
                                       └─► M24
```

Each module is specified, reviewed and approved just before it is built, not
all up front: what M15–M21 teach will change the later specs.

---

## Security model

The current graph already has all three legs of the "lethal trifecta": it
reads attacker-writable email, it can search the owner's private mailbox, and it
proposes calendar invites whose guests the model chooses. Today the Telegram
approval is the only barrier. v2 makes the barrier structural.

**Risk tiers.** Every tool is registered with a tier, and the executor — not the
prompt — enforces it.

| Tier | Examples | Rule |
|---|---|---|
| T0 read | search mail, read a thread, list events, free/busy | Automatic |
| T1 reversible, internal | label, archive, draft, calendar hold without guests, task in the owner's own tool | One tap; the only tier eligible for earned autonomy |
| T2 external or recoverable-destructive | send, reply, invite (an event with guests), share, post, move to trash | Approval bound to the exact arguments, every time; never autonomous |
| T3 forbidden | payment, permanent delete, forwarding/filter/auto-reply rules, permission, sharing or OAuth changes | Never registered as a tool |

**Rules that hold in every module.**

- Approvals carry a single-use nonce bound to a hash of the exact arguments, checked again at execution. A free-text "yes" is never an approval.
- T2 recipients must already be participants in the source thread, or contacts the owner has confirmed.
- Arguments derived from untrusted or retrieved content always need approval and show where they came from.
- Content from email, documents, the web or forwarded messages is data. It is never placed in the same channel as the owner's instructions.
- Nothing with private data in context may fetch a URL or render a link or image.
- Only the owner can write memory.
- Tools from external providers are pinned by a hash of their definition; a changed definition disables the tool until reviewed.
- `DRY_RUN` is enforced in the registry, so it covers every provider, not only the Calendar client.
- A monthly spend cap fails closed, including for unpriced models.

---

## What the adversarial review changed

The recommended direction was reviewed by a fresh-context agent instructed to
find faults. All eighteen findings were judged valid. The ones that reshaped
the plan:

- **The approval loop has never run in production, and its channel is blocked.** Hence M15 and M16 before any new capability.
- **The budget was unmeasured.** The price table used placeholder rates, and Flash doubles in January 2027. Hence the measurement in M15 and the fail-closed cap in M17.
- **"All apps" is not reachable compliantly.** WhatsApp, iMessage and Telegram chats cannot be read. A ledger the owner has to double-check saves nothing, so every digest states what it does not cover.
- **Invites are external sends.** Scheduling can never become autonomous; autonomy is limited to T1.
- **"N clean approvals" is weak evidence.** With zero errors in N approvals, the 95% upper bound on the error rate is still about 3/N. Autonomy thresholds are bound-based, edits and cancellations count as failures, and unlocked action types keep random audits.
- **An exfiltration path exists today.** The extractor holds `search_context` while reading raw mail, and owner corrections are appended to the email body in the same string (`app/extraction/pipeline.py`), so any sender can forge one. M17 and M18 close both.
- **The poller does not see every message.** It reads the newest unread messages only and never sees sent mail. Hence M20.
- **The Gmail-keyed schema cannot hold other sources.** New sources get their own tables; parked graph threads are drained before any topology change.
- **Evaluation must precede implementation.** The obligations golden set comes before the extractor, as M02 did in v1.
- **The planner is the largest cost and injection surface.** It is built after the policy and input-hygiene layers, with plan-then-execute.

---

## Project-wide spec

### Tech stack

Existing, and kept: Python 3.12 with uv, FastAPI, LangGraph (1.2.x locked) with
the Postgres checkpointer, google-genai, Postgres with pgvector, APScheduler, the
Next.js dashboard, Fly.io or Railway.

Candidate additions, each approved in its own module spec: a Web Push library
(M16), the Composio Python SDK and an MCP client (M22), a browser-automation
library (M27).

### Commands

The existing task runner stays the entry point.

```powershell
.\tasks.ps1 check          # ruff lint + format check + mypy --strict + pytest
.\tasks.ps1 eval           # golden-set evaluation
.\tasks.ps1 serve          # FastAPI: webhook + scheduler
.\tasks.ps1 migrate        # apply schema
.\tasks.ps1 retrieval-eval --by-kind
```

Each module spec lists the commands it adds.

### Project structure

The current layout (see `README.md`) stays. New packages are decided in each
module's spec; the tentative shape is one package per capability — for
example `app/policy/` for M17, `app/planner/` for M19 and `app/obligations/`
for M21 — with graph nodes staying thin and delegating to them, as today.

### Code style

Follow the existing code. Its defining habit: rules that matter are enforced in
code, and the comment says why.

```python
def _after_review(state: GraphState) -> Literal["extract", "conflicts", "skip", "reject"]:
    """Where the reviewer's verdict sends the graph.

    The cap lives here rather than in the reviewer's prompt. "Only revise twice"
    is a request; a counter compared in the router is the reason the loop
    terminates.
    """
```

Also: Pydantic contracts with `extra="forbid"` at every model boundary; safe
defaults (off, dry-run, empty allowlist); `mypy --strict`; ruff with a
100-character line length; docstrings that explain decisions rather than restate
code.

### Testing strategy

- **Unit tests** with pytest, in `tests/`, using injected fakes as the existing `Deps` pattern does.
- **Integration tests** marked `integration`; they skip automatically without a reachable Postgres.
- **Golden sets before model features.** Every model-driven capability gets its labelled set and a scored baseline before the implementation exists: tool routing (M19), obligations and closure (M21), per-connector routing (M23).
- **An injection regression suite** (M18) gates CI: hidden text, calendar-invite titles, attendee injection, OTP bait, and paraphrased retries.
- **Runtime verification** on the deployed instance for anything that touches scheduling, OAuth or channels.

### Boundaries

- **Always:** build the eval set before the model feature; run `.\tasks.ps1 check` before every commit; route every side effect through the registry and `DRY_RUN`; publish only measured numbers, labelling estimates as estimates; keep migrations additive.
- **Ask first:** new dependencies; schema changes; new external services or providers; wider OAuth scopes; any new T2 action type; changing a safety default.
- **Never:** expose T3 actions to the model; execute payments; install third-party skills or unpinned MCP servers; commit real email or secrets; send real mail through a free-tier model key.

---

## Open questions

- **Social and freelance platforms.** Official APIs for personal messaging on LinkedIn, X, Instagram, Upwork and Fiverr look restricted or partner-only. To be verified before M23; the likely outcome is read-only access where an API exists and the M27 browser fallback or nothing where it does not.
- **Work tools.** Are the Slack, Notion, ClickUp and Jira workspaces owned by the owner, or by an employer whose data policy applies?
- **Retention.** How long ingested content is kept, and when it is purged.
- **Cross-model review.** The Gemini and Codex CLIs are not installed; a manual cross-model review of any spec is available on request.
