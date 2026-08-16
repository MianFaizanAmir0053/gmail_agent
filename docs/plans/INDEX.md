# Build Plan Index

Master plan (rationale, what was wrong with v1, stack decisions): `../../MASTER-PLAN.md`

Each module below is independently shippable with a binary exit criterion. Build in dependency order; each one leaves the system in a working state.

## Dependency graph

```
M00 ─┬─► M01 ─┬─► M03 ─► M04 ─► M05 ─► M06 ─► M07 ──► M08 ─┬─► M09
     │        │                                             │
     └─► M02 ─┘                                             └─► M10 ─► M11 ─► M12 ─► M13 ─► M14
```

M01 and M02 are parallel — when the OAuth consent screen makes you want to quit, go label emails instead.

## Modules

| # | Module | Est. | Status |
|---|---|---|---|
| [M00](M00-skeleton.md) | Repo & runtime skeleton | 0.5d | ☑ done |
| [M01](M01-google-auth.md) | Google auth & API clients | 1d | ☑ done |
| [M02](M02-eval-harness.md) | Eval harness & golden dataset | 1d | ☑ done |
| [M03](M03-extraction.md) | Extraction & classification | 2–3d | ☑ done — baseline 92.9% |
| [M04](M04-action-idempotency.md) | Action layer & idempotency | 1d | ☑ done |
| [M05](M05-langgraph.md) | LangGraph orchestration & durable state | 3d | ☑ done |
| [M06](M06-telegram-hitl.md) | Telegram interface & human-in-the-loop | 2d | ◐ code done, needs a bot token |
| [M07](M07-deploy.md) | Deploy | 1d | ◐ deployable, not deployed |
| [M08](M08-observability.md) | Observability | 2–3d | ☑ done |
| [M09](M09-dashboard.md) | Next.js dashboard | 3d | ☑ done |
| [M10](M10-rag-ingestion.md) | RAG ingestion pipeline | 3d | ☑ done |
| [M11](M11-hybrid-retrieval.md) | Hybrid retrieval & `search_context` | 2d | ☐ |
| [M12](M12-retrieval-eval.md) | Retrieval evaluation | 1d | ☐ |
| [M13](M13-reviewer-agent.md) | Reviewer agent | 2–3d | ☐ |
| [M14](M14-pipeline-packaging.md) | Scheduled pipeline & portfolio packaging | 2d | ☐ |

**Ship publicly after M07.** M08 is the highest value-per-hour module in the plan.

**Cut list, in order:** M12 → M13 → M09. Never cut M02 or M08 — they are the differentiator.

## Shared contracts

All modules speak `app/contracts.py`. Get it right early — see [M00](M00-skeleton.md).
