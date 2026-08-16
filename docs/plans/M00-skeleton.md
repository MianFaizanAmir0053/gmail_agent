# M00 · Repo & runtime skeleton

**Est.** 0.5 day · **Depends on** nothing · **Blocks** everything

## Goal

A repo where every later module can be built, tested, and containerised without any setup friction. Boring on purpose — this is the module that makes the other fourteen cheap.

## Deliverables

- Git repo, `.gitignore` (must cover `.env`, `token.json`, `data/raw_emails/`)
- Dependency management: `uv` (fast, recommended) or Poetry
- `ruff` (lint + format) and `mypy` configured in `pyproject.toml`
- `pytest` with one trivial passing test
- `Dockerfile` (multi-stage, non-root user) and `docker-compose.yml` with Postgres
- `app/config.py` using `pydantic-settings` — typed settings, fails loudly on missing required vars
- `app/contracts.py` — the shared data models (below)
- `.env.example` documenting every variable with a comment
- GitHub Actions: lint + typecheck + test on push, plus a `docker build` job
- `tasks.ps1` with `check`, `lint`, `fmt`, `typecheck`, `test`, `run`, `up`, `down`, `eval` (stub for now)

## The contracts file

This is the most important artifact in M00. Every module reads or writes these types; stable boundaries here are what make the modules independently replaceable.

```python
# app/contracts.py
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field

class EmailMessage(BaseModel):
    id: str                      # Gmail message ID — the idempotency key
    thread_id: str
    subject: str
    body_text: str
    sender: str
    recipients: list[str]
    received_at: datetime

class ExtractionResult(BaseModel):
    is_meeting: bool
    title: str | None = None
    start_utc: datetime | None = None
    end_utc: datetime | None = None
    timezone: str | None = None          # IANA, e.g. "Asia/Karachi"
    attendees: list[str] = Field(default_factory=list)
    location: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str

class ActionResult(BaseModel):
    status: Literal["created", "skipped_duplicate", "rejected", "failed"]
    event_id: str | None = None
    error: str | None = None
```

## Key decisions

- **`DRY_RUN: bool` goes in config from the very first commit.** Every write path checks it. This is what stops M03's test runs from writing garbage to a real calendar.
- Settings are typed and validated at import time. A missing `ANTHROPIC_API_KEY` should fail at startup, not three nodes into a graph.
- Don't add a database migration tool yet — plain SQL in `migrations/` is fine until M04. Add Alembic when the schema starts churning.

## Exit criterion

`docker build .` succeeds and CI is green on an empty test suite.

## Running notes

**`make` is not installed on this machine** — replaced the planned Makefile with `tasks.ps1`. Later module plans referencing `make eval` mean `.\tasks.ps1 eval`.

**Toolchain was empty at start:** no Python (the `python` on PATH is the Microsoft Store stub), no `uv`, Docker installed but daemon stopped. Chose `uv` over Poetry partly because it installs and pins Python itself, so it's one install rather than two.

**Postgres image is `pgvector/pgvector:pg16`, not plain `postgres`** — M10 needs the extension and swapping the image later would mean recreating the volume.

**`DRY_RUN` defaults to `true` and `ALLOWED_CHAT_IDS` defaults to empty.** Both chosen so that forgetting to configure them fails closed rather than open.

Settings load through `get_settings()` rather than a module-level instance, so importing `app.config` in a test doesn't require a populated environment — but a real boot still fails loudly on missing required vars.

**Ruff formats Python code fences inside Markdown.** It rewrote the plan docs' illustrative snippets (`{ ... }` elisions, grouped one-line fields). Added `extend-exclude = ["**/*.md"]` — those snippets are prose, not code to be gated.

**Defensive `# type: ignore` were counterproductive.** Added ten of them pre-emptively around `Settings()` and `Literal` args; the pydantic mypy plugin already handles both, so strict mode flagged every one as `unused-ignore`. Write the code first, add ignores only where mypy actually complains.

### Verified

```
ruff check          pass
ruff format         pass
mypy --strict       pass (app + tests)
pytest              10/10
docker build        pass
```

Container behaviour, checked directly rather than assumed:

- boots with config: `mailagent ok | env=dev dry_run=True`
- without config: exits 1 and names both missing vars — the fail-loudly design works
- `id` reports `uid=10001(appuser)` — non-root confirmed

CI unverified; nothing pushed to GitHub yet.
