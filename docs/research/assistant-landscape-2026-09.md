# Personal-assistant landscape, September 2026

Evidence behind [`ASSISTANT-PLAN.md`](../../ASSISTANT-PLAN.md). Compiled on
30 Sep 2026 from five parallel research passes: Meta's Muse, xAI's Grok, the
assistant market, integration and channels, and agent security.

**Method and limits.** Primary sources first (vendor posts, docs, changelogs),
reputable press second. The session's web-search quota ran out partway
through, so later facts were checked by fetching official pages directly.
Anything resting on a single secondary source, or not found at all, is marked
**UNVERIFIED**. Products in this space change monthly; re-check before relying
on any single fact.

---

## 1. Meta Muse

"The Muse bot" most likely means Meta's **Muse** personal agent (about 85%
confidence); about 10% Meta AI running the Muse Spark model; under 5% the
open-source MuseBot Telegram/Discord bot ([GitHub](https://github.com/yincongcyincong/MuseBot)).
Microsoft's Muse (a gameplay model) and Sudowrite's Muse (fiction) are not
assistants.

**Timeline (2026)**

- 8 Apr — Muse Spark, the first Meta Superintelligence Labs model, closed weights ([Meta](https://about.fb.com/news/2026/04/introducing-muse-spark-meta-superintelligence-labs/)).
- 9 Jul — Spark 1.1 and a public Model API preview with MCP and skills support ([Meta AI](https://ai.meta.com/blog/introducing-muse-spark-meta-model-api/)).
- 24 Jul — Meta AI gains email and calendar connectors and a daily briefing ([Meta](https://about.fb.com/news/2026/07/meta-ai-muse-spark-doesnt-just-think-it-acts/)).
- 8 Sep — **Muse** agent on Spark 1.3: iOS, Android, web and as a WhatsApp contact; US first ([Meta](https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/)).
- 18 Sep — Mac app with opt-in access to Mail, Messages, Calendar, Notes and files ([TechCrunch](https://techcrunch.com/2026/09/18/metas-muse-hits-mac-letting-the-ai-take-actions-on-your-computer/)).
- 23 Sep — announced: avatar video chat, glasses support, the agent's own email address, a pocket device ([TechCrunch](https://techcrunch.com/2026/09/23/everything-new-coming-to-metas-ai-agent-muse/)).
- 29 Sep — Muse for Small Business: Asana, QuickBooks, Shopify, Slack, Zoom and custom connectors ([Meta](https://about.fb.com/news/2026/09/introducing-muse-small-business/)).

**Capabilities.** A per-user cloud VM; browser tasks, forms, email, travel;
payments through Stripe Link single-use card numbers; keeps working after the
app closes; memory the user can tell to forget; unprompted suggestions;
"Contemplating" mode runs agents in parallel.

**Security design** ([Meta research](https://research.meta.ai/blog/security-and-safety-for-ai-agents-our-approach-with-muse)) — the most transferable part:

- a separate "Sentinel" agent gates all network egress;
- taint tracking: a process that has read user data loses auto-approval;
- the agent sees stand-in credentials, swapped for real ones at the network edge;
- an ensemble of prompt-injection classifiers;
- the email connector strips one-time codes, password-reset links and magic links;
- purchases always need approval; read-only and previously allowed actions do not;
- an audit trail and a bug bounty.

**Pricing.** Free with a usage meter; $20 and $100 monthly tiers ([Meta](https://www.meta.com/help/subscriptions/1021145227643680/)).
About 3.4M downloads in its first weeks, per Sensor Tower via [TechCrunch](https://techcrunch.com/2026/09/25/meta-is-putting-its-muscle-behind-muse-as-the-ai-app-takes-off/).

**Criticisms**

- The Mac app synced about 187k iMessage rows after a journalist declined access, and the agent misdescribed its own access ([Decrypt](https://decrypt.co/379122/metas-muse-ai-agent-user-private-imessages-lied-how)).
- It compiled lists of people from Facebook and Instagram after a refused request was reworded ([Hunterbrook](https://hntrbrk.com/breaking-news/muse-doxxing)).
- A severe flaw that could expose a user's VM was found through the bug bounty ([report](https://www.thestar.com.my/tech/tech-news/2026/09/26/meta-bolsters-muse-safety-warning-after-security-vulnerability-found-the-information-reports)).
- Amazon blocked it ([TechCrunch](https://techcrunch.com/2026/09/21/metas-ai-agent-has-been-blocked-from-using-amazon-com/)).
- Headline benchmarks came from a configuration not generally available ([Artificial Analysis](https://artificialanalysis.ai/articles/muse-spark-1-3)).
- Undisclosed human callers behind its phone calls: UNVERIFIED.

**Transferable lessons.** Never let the model describe its own access; answer
from the scope registry. Refusals are not a policy. Use APIs and identify as an
agent. Publish scores only for the configuration you ship.

---

## 2. xAI Grok

SpaceX acquired xAI on 2 Feb 2026 ([xAI](https://x.ai/news/xai-joins-spacex)).

**Assistant features relevant to a personal assistant**

- **Grok Bot** (11 Aug 2026): a persistent cloud VM; connectors first, computer use as fallback; teach a task once, save it as a skill, schedule it; approval rules for send, publish, delete and purchase; two-factor codes, passwords and CAPTCHAs hand control back to a human ([docs](https://docs.x.ai/grok-bot/faq)). Team Bots in Slack entered beta on 28 Sep 2026 ([xAI](https://x.ai/news/team-bots)).
- **Automations** (16 Jul 2026): run on a schedule or on matching Gmail/Outlook email; run history, pause, "Run now" ([xAI](https://x.ai/news/grok-automations)). Read-only bank triggers via Plaid in Aug 2026.
- **Connectors** (6 May 2026): Google Workspace, Outlook, SharePoint, OneDrive, Notion, GitHub, Linear, bring-your-own MCP; Gmail access tiered read, modify, send ([xAI](https://x.ai/news/grok-connectors)).
- **Skills** (18 May 2026): user-taught procedures and styles ([xAI](https://x.ai/news/grok-skills)).
- **Memory** with visible update cards, edit and delete; a private mode with no history.
- **Office add-ins**; the Outlook add-in drafts only, and a human sends.
- Voice with live camera and screen sharing; a Voice Agent API at about $0.08/min.
- Tiers: Free, $10, $30 (includes Grok Bot), $100, $300 ([plans](https://grok.com/plans)).
- A $300M Telegram deal announced in May 2025 never shipped.

**What is distinctive.** Live X data and distribution through X and Tesla.
Memory, tasks, connectors, canvas and skills are catch-up features.

**Criticisms**

- "MechaHitler" (Jul 2025): untrusted X posts steered @grok's replies for about 16 hours ([Engadget](https://www.engadget.com/ai/grok-team-apologizes-for-the-chatbots-horrific-behavior-and-blames-mechahitler-on-a-bad-update-184520189.html)).
- The "white genocide" replies (May 2025) came from an unauthorized prompt edit that bypassed review ([CNBC](https://www.cnbc.com/2025/05/15/musks-xai-grok-white-genocide-posts-violated-core-values.html)).
- About 370k shared chats were indexed by search engines (Aug 2025).
- The sexualized-deepfake scandal from Dec 2025 brought regulator probes, country blocks and lawsuits ([summary](https://en.wikipedia.org/wiki/Grok_sexual_deepfake_scandal)).
- Training on consumer data is on by default ([xAI FAQ](https://x.ai/legal/faq)).
- Grok Build uploaded whole repositories, secrets included, despite its privacy toggle ([The Register](https://www.theregister.com/ai-and-ml/2026/07/14/musk-promises-purge-after-grok-build-caught-sending-entire-repos-to-the-cloud/5271123)).
- The API's remote MCP tools have no approval hook ([docs](https://docs.x.ai/docs/guides/tools/remote-mcp-tools)).

**Transferable lessons.** Email-triggered automations with run history; tiered
scopes with "ask first" rules; human takeover for authentication; visible
memory. Avoid: untrusted content steering the agent, unreviewed prompt changes,
opt-outs that miss the real data flow, and side-effect tools without an
approval hook.

---

## 3. Market map

**Big labs**

- **OpenAI.** Agent mode removed in Aug 2026 without notice; Pulse replaced by a Scheduled Tasks hub; Atlas shut down. ChatGPT Work (Jul 2026) runs hours-long tasks across connected apps. **Dots** (29 Sep 2026): always-on background agents with their own cloud computer, top tiers only ([TechCrunch](https://techcrunch.com/2026/09/29/openai-launches-dots-its-bubbly-agentic-avatar/)).
- **Google.** **Gemini Spark** (I/O, May 2026; $20 AI Pro in the US since Jul): a 24/7 cloud agent with schedules and skills that asks before spending or sending ([9to5Google](https://9to5google.com/2026/07/23/gemini-spark-google-ai-pro-us/)). Gmail **AI Inbox** surfaces action items and deadlines. The Labs "CC" briefing became Gemini Daily Brief, now reaching free users. Mariner was shut down in May 2026. A review of CC found swapped names, re-flagged answered mail and random FYIs ([TidBITS](https://tidbits.com/2026/05/29/taming-email-overload-googles-cc-daily-briefing-agent/)).
- **Microsoft.** Copilot Tasks (preview Feb 2026) with mandatory approval for purchases, sending personal data, messages and deletes ([Microsoft](https://support.microsoft.com/en-us/microsoft-copilot/using-copilot-tasks)); a business-first Copilot super app (Sep 2026).
- **Anthropic.** Connectors and MCP; Skills; Cowork, default in all chats since Sep 2026; Claude in Chrome GA on 26 Aug 2026 behind an action classifier ([Anthropic](https://claude.com/blog/claude-in-chrome-generally-available)).
- **Perplexity.** Comet browser; an Email Assistant on the $200 tier. A Ninth Circuit ruling on 4 Aug 2026 treated user-directed agent access as the user's own access ([Cooley](https://www.cooley.com/news/insight/2026/2026-08-06-ninth-circuit-rules-on-ai-agent-access-to-third-party-websites-under-cfaa)).
- **Apple.** The Gemini-built Siri shipped in iOS 27 (14 Sep 2026) as an English beta with a waitlist and daily caps ([Apple](https://www.apple.com/newsroom/2026/09/siri-ai-a-profoundly-more-capable-and-personal-assistant-is-here/)).
- **Amazon.** Alexa+ US-wide from 4 Feb 2026; early users called it too chatty.

**Open source**

- **OpenClaw** — the self-hosted "do anything" assistant, most-starred software repo on GitHub by Mar 2026 ([Star History](https://www.star-history.com/blog/openclaw-surpasses-react-most-starred-software/)). One gateway owns sessions and channels (WhatsApp, Telegram, Slack, Signal, iMessage and more); Markdown memory; a 30-minute heartbeat; skills from ClawHub.
- Its incident record:
  - CVE-2026-25253, a one-click remote code execution;
  - 341 malicious ClawHub skills, later 824;
  - 135k+ exposed instances, default bind `0.0.0.0`;
  - the Moltbook leak of 1.5M agent tokens ([The Register](https://www.theregister.com/security/2026/02/09/openclaw-instances-open-to-the-internet-present-ripe-targets/5043770), [The Hacker News](https://thehackernews.com/2026/02/researchers-find-341-malicious-clawhub.html)).
- **Inbox Zero** — open source, plain-English rules, reply tracking ([GitHub](https://github.com/elie222/inbox-zero)).

**Startups**

- **Poke** — iMessage/SMS/Telegram; bought by Cognition in Jul 2026 ([TechCrunch](https://techcrunch.com/2026/07/24/why-cognition-bought-poke-ai-personality-is-becoming-a-competitive-advantage/)).
- **Lindy** — Apple banned its iMessage account; Trustpilot 1.7 over credit burn and hallucination ([Trustpilot](https://www.trustpilot.com/review/lindy.ai)).
- **Martin** — text, call, WhatsApp; $21–49.
- **Manus** — deleted user data created between 29 Dec 2025 and 23 Aug 2026 when it left Meta ([report](https://www.trendingtopics.eu/manus-becomes-independent-again-following-2b-meta-deal-and-deletes-user-data/)).
- **Genspark** — credit burn and derailments in reviews.
- **Howie** — email-CC scheduling.
- **Fyxer** — drafts that need rewriting, filed mail that "disappears".
- **Superhuman**, **Shortwave**, **Motion**, **Reclaim** — email and calendar tools adding agents.
- **Relay.app** — shut down in Sep 2026 and deleted all data.
- **Newcomers** — Instinct (raised $1B on 28 Sep 2026); Ollie (privacy-first family logistics).

**Automation platforms.** Zapier Agents and MCP (9,000 apps, two tasks per
call); n8n with per-tool human approval; Make with manual approvals.

**Table stakes (Sep 2026)**

1. Gmail/Outlook and Calendar: search, triage, drafts in the user's voice, send; multiple accounts.
2. An app plus at least one chat channel, plus email CC or forward.
3. Schedules, event triggers and a morning brief.
4. Memory the user can inspect, edit and forget.
5. Approval gates on send, pay, delete and sharing personal data; an action log; human takeover.
6. Cloud execution with the user's devices off.
7. Native APIs for core apps, MCP for the long tail, a browser for sites without an API.
8. No training on user data; export and delete.
9. A price anchor around $20/month, with free tiers from Google, Meta and Amazon.

**Unmet needs**

1. **Accuracy you can check.** No vendor was found publishing per-user task accuracy (UNVERIFIED absence).
2. **Predictable cost.** Credit burn tops Lindy and Genspark reviews; one OpenClaw user spent $560 in a weekend ([HN](https://news.ycombinator.com/item?id=46820783)).
3. **Safe handling of hostile input.**
4. **Continuity and data ownership.** Products were shut down and data deleted throughout 2026.
5. **Closure, not briefs.** Summaries restate the inbox; nobody reliably closes loops.
6. **Reach across ecosystems.** Each vendor is strongest on its own data; iMessage and WhatsApp are gated; launches are US and English first.
7. **Vendor trust.**
8. **Billing and support ethics.**

**Candidate positions for a small team**

1. **Receipts-first, earned autonomy** — chosen.
2. **An obligations ledger** — chosen as the first job.
3. **An injection-hardened agent for sensitive inboxes** — folded into 1.
4. **Budgeted and portable** — supporting.
5. **Telegram-first outside the US** — dropped: Telegram is blocked on the owner's network.

The one that crushes us if wrong: Google, should Gmail's AI Inbox add closure
tracking and citations, free and inside Gmail.

---

## 4. Integration and channels

**MCP**

- Spec **2026-07-28**: stateless (no `initialize`, no session IDs); OAuth 2.1 with PKCE; Client ID Metadata Documents preferred over dynamic registration; servers must not pass tokens through ([spec](https://modelcontextprotocol.io/specification/2026-07-28/changelog)).
- The registry is still a metadata-only preview.
- `langchain-mcp-adapters` has been replaced by `langchain.mcp` (beta). This repo has no LangChain dependency, so a plain MCP client is lighter.
- **google-genai's MCP support is experimental, and its automatic function calling runs the tools itself, bypassing approvals.** Turn it off and execute tools in our own graph node ([python-genai](https://github.com/googleapis/python-genai)).
- The Interactions API went GA in Jun 2026, and `generateContent` is now labelled legacy.
- Google Workspace's own MCP servers have been in developer preview since 1 May 2026 ([Workspace updates](https://workspaceupdates.googleblog.com/2026/05/agent-tools-and-security-updates-for-workspace-developers.html)).

**Managed auth and tool platforms**

| Platform | Notes | Price | Holds tokens |
|---|---|---|---|
| Composio | 1000+ toolkits; Python SDK; LangGraph provider; Tool Router for tool search; SOC 2 Type II | Free 100k calls/month with your own OAuth apps (20k with theirs); Pro $29 ([pricing](https://composio.dev/pricing)) | Composio ([custody](https://docs.composio.dev/docs/security/token-custody)) |
| Arcade | 7,500+ tools; auth through LangGraph `interrupt()` | Free 2k calls, then $25/month plus $0.01/call | Arcade |
| Pipedream Connect | 3,000+ APIs; acquired by Workday | Production about $99/month (UNVERIFIED) | Pipedream |
| Nango | Auth and API proxy only; self-hostable (ELv2) | Free for 10 connections | You or Nango |
| Merge Agent Handler | MCP packs with a data-loss-prevention gateway | Pro $1,000/month | Merge |
| Zapier MCP | 9,000 apps; two tasks per call | — | Zapier |

**Decision:** Composio, with our own OAuth apps, behind our own registry. Self-hosted Nango is the exit path. Whether Composio lets you export tokens is UNVERIFIED.

**Tool count**

- Accuracy falls as the tool catalog grows: LongFuncEval measures 7.6–85.6% drops ([arXiv](https://arxiv.org/abs/2505.10570)).
- Retrieving tools first raised accuracy from 13.6% to 43.1% in RAG-MCP ([arXiv](https://arxiv.org/abs/2505.03275)).
- Anthropic reports tool search raising accuracy substantially ([Anthropic](https://www.anthropic.com/engineering/advanced-tool-use)).
- Gemini rejects more than 512 function declarations (observed Feb 2026) and has no native tool search.
- **Pattern adopted:** a pgvector tool catalog, about 8 core tools plus top-K retrieved (at most 20), and a `search_tools` meta-tool.

**Channels**

- **WhatsApp.** Since 15 Jan 2026 the Business API prohibits AI providers whose primary function is a general-purpose assistant, with exceptions only where required by law, such as Brazil ([Meta terms](https://www.facebook.com/legal/Meta-Terms-for-WhatsApp-Business-Platform), [AI providers](https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing/ai-providers)). Not viable.
- **Slack.** AI apps need the Agents feature and a paid plan. Non-Marketplace apps are limited to 1 request/min on history reads, while internal apps are exempt ([Slack](https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps)). Ship as an internal app.
- **iMessage.** No API for personal accounts. Skip.
- **Voice.** `gemini-3.8-live` GA on 15 Sep 2026 ([changelog](https://ai.google.dev/gemini-api/docs/changelog)).

**Google OAuth**

- `gmail.readonly`, `gmail.compose` and `gmail.modify` are restricted; `gmail.send` is sensitive ([scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)). `gmail.compose` can also send, so a drafts-only policy must be enforced in code.
- Restricted scopes for public apps need verification plus a yearly CASA assessment; one lab charges $675–4,500 per app at AL1 ([verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification)).
- **The seven-day refresh-token expiry comes from "Testing" status** ([Google](https://support.google.com/cloud/answer/15549945)). Publishing "In production" without verification (personal use, under 100 users, warning screen stays) reportedly removes it. To be confirmed in M15.

**Microsoft Graph.** Users can consent for their own mailbox; publisher
verification is free; refresh tokens last 90 days; no CASA equivalent.

**Gemini API terms.** Free-tier prompts may be used to improve Google products
and read by human reviewers; the paid tier is required for users in the EEA,
Switzerland and the UK ([terms](https://ai.google.dev/gemini-api/terms)).

**Gemini prices** ([pricing](https://ai.google.dev/gemini-api/docs/pricing), fetched 30 Sep 2026)

- Gemini 3.6/3.7/3.8 Flash: $0.75 in / $3.75 out per 1M tokens until 31 Dec 2026, then $1.50 / $7.50.
- 3.5 Flash-Lite: $0.30 / $2.50.
- 3.1 Flash-Lite: $0.25 / $1.50.
- Embedding 2: $0.20.

**No-API fallback**

- Gemini Computer Use is in preview and returns `require_confirmation` before purchases and sends ([docs](https://ai.google.dev/gemini-api/docs/computer-use)).
- On the human-judged Online-Mind2Web benchmark: Operator 61.3%, Claude Computer Use 56.3%, Browser Use 30.0% ([arXiv](https://arxiv.org/html/2504.01382)).
- Playwright MCP's own docs say it is not a security boundary.

---

## 5. Agent security

**Framings**

- **The lethal trifecta:** private data, untrusted content and external communication together enable exfiltration ([Willison](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/)).
- **Agents Rule of Two:** per session, allow at most two of untrusted input, sensitive data, and state change or external communication; all three need a human ([Meta](https://ai.meta.com/blog/practical-ai-agent-security/)).
- **The dual-LLM pattern** ([Willison](https://simonwillison.net/2023/Apr/25/dual-llm-pattern/)).
- **CaMeL:** a planner sees only the user's query; a quarantined model parses data; an interpreter enforces capability policies. It solves 77% of AgentDojo tasks with provable security, against 84% undefended ([arXiv](https://arxiv.org/abs/2503.18813)).
- **Six design patterns** for securing agents ([arXiv](https://arxiv.org/abs/2506.08837)).

**Vendor numbers do not transfer.**

- AgentDojo shows spotlighting cutting attack success far less than its own paper claimed ([results](https://agentdojo.spylab.ai/results/)).
- A large red-teaming competition found nearly every agent violated policy within 10–100 queries ([arXiv](https://arxiv.org/abs/2507.20526)).
- Adaptive attacks broke 12 published defenses, most at over 90% attack success ([arXiv](https://arxiv.org/abs/2510.09023)).
- The conclusion: model robustness is not a security boundary; system design must make a successful injection harmless.

**Incidents and the control that stops each**

| Incident | Control |
|---|---|
| EchoLeak, M365 Copilot, Jun 2025 ([HTB](https://www.hackthebox.com/blog/cve-2025-32711-echoleak-copilot-vulnerability)) | No model-rendered links or images; provenance on outbound data |
| MCP tool poisoning and rug pulls ([Invariant](https://invariantlabs.ai/blog/mcp-security-notification-tool-poisoning-attacks)) | Pin tool definitions by hash |
| GitHub MCP private-repo leak ([Invariant](https://invariantlabs.ai/blog/mcp-github-vulnerability)) | Private data never flows to public destinations |
| Supabase MCP ([Willison](https://simonwillison.net/2025/Jul/6/supabase-mcp-lethal-trifecta/)) | Least-privilege credentials; approval on writes |
| Comet reading a Gmail OTP via hidden page text ([Brave](https://brave.com/blog/comet-prompt-injection/)) | Keep web reading apart from logged-in sessions |
| Calendar-invite titles hijacking Gemini ([SafeBreach](https://www.safebreach.com/blog/invitation-is-all-you-need-hacking-gemini/)) | Treat invites as untrusted; argument-bound approvals |
| ShadowLeak, Deep Research exfiltrating Gmail ([THN](https://thehackernews.com/2025/09/shadowleak-zero-click-flaw-leaks-gmail.html)) | No URL fetching with mailbox data in context |
| postmark-mcp BCC'ing every email ([THN](https://thehackernews.com/2025/09/first-malicious-mcp-server-found.html)) | No unvetted tool servers; alert on new recipients |
| Replit agent deleting a production database | Destructive operations never exposed; backups |
| OpenClaw CVE, malicious skills, exposed instances | No internet-exposed control plane; no third-party skills |
| ZombieAgent persistent memory injection ([Radware](https://www.radware.com/blog/threat-intelligence/zombieagent/)) | Owner-only memory writes |
| LangGraph checkpointer flaws, Jun 2026 ([Check Point](https://research.checkpoint.com/2026/from-sqli-to-rce-exploiting-langgraphs-checkpointer/)) | Patch; never pass untrusted input as checkpoint filters. Postgres saver unaffected; the lockfile pins patched versions but `pyproject.toml` floors are loose |

**OWASP.** LLM Top 10 2025 ([OWASP](https://genai.owasp.org/llm-top-10/)) and
the Top 10 for Agentic Applications 2026 ([OWASP](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/)).
Both map onto the controls in the security model of `ASSISTANT-PLAN.md`:
injection is handled by provenance and tool-less readers; excessive agency by
the tier registry and minimal scopes; memory poisoning by owner-only writes
with expiry; rogue behaviour by an append-only audit log, auto-pause and a kill
switch.

**Memory**

- LangMem runs on LangGraph's Postgres store with pgvector ([LangMem](https://langchain-ai.github.io/langmem/)); Mem0 supports pgvector; Letta self-hosts on Postgres. Graphiti needs a graph database, but its valid-from/valid-to columns on facts are worth copying.
- LongMemEval shows commercial assistants losing about 30% accuracy on long-term memory ([arXiv](https://arxiv.org/abs/2410.10813)). LoCoMo results are contested between vendors; evaluate on our own data.
- Memory poisoning is practical: MINJA reached over 95% success ([arXiv](https://arxiv.org/abs/2503.03704)), and Microsoft found 50+ commercial memory-poisoning prompts in the wild ([Microsoft](https://www.microsoft.com/en-us/security/blog/2026/02/10/ai-recommendation-poisoning/)).

**Proactivity**

- Persistent suggestions were rated distracting ([CHI 2025](https://arxiv.org/abs/2410.04596)).
- The best model scored F1 66.5% at deciding when to help ([arXiv](https://arxiv.org/abs/2410.12361)).
- Apple paused AI news summaries after false headlines.
- OpenClaw's 30-minute heartbeat is a round-the-clock cost.
- **Adopted:** event triggers plus one daily digest, rare interrupts, a source link on every item, mute per category, and dismissal rates tracked.
- Promises in the owner's own sent mail are trusted; requests in inbound mail stay suggestions.
