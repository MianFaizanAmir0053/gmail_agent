# mailagent

Reads Gmail, extracts meeting details, proposes a Calendar event, and waits for a human tap in Telegram before writing anything.

> **Status:** M00 (skeleton). See [`docs/plans/INDEX.md`](docs/plans/INDEX.md) for the build plan and [`MASTER-PLAN.md`](MASTER-PLAN.md) for the reasoning behind it.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (manages Python too — no separate Python install needed)
- Docker Desktop, running

Install uv on Windows:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## Setup

```powershell
.\tasks.ps1 setup
```

Then fill in `.env`. Only `DATABASE_URL` and `ANTHROPIC_API_KEY` are required to boot; the Google and Telegram values arrive in M01 and M06.

## Tasks

`make` is not used — `tasks.ps1` replaces it.

```powershell
.\tasks.ps1 check      # lint + format + typecheck + test
.\tasks.ps1 test
.\tasks.ps1 fmt        # autofix
.\tasks.ps1 up         # start Postgres (pgvector)
.\tasks.ps1 run        # validate config and exit
```

## Safety defaults

- `DRY_RUN` defaults to **true**. Every Calendar write path checks it. Forgetting to set it is safe, not destructive.
- `ALLOWED_CHAT_IDS` defaults to **empty** — nobody can talk to the bot. This bot can read your email; the allowlist is not optional.
- Real email content lives in `data/raw_emails/` and is gitignored. Only anonymised fixtures are committed.

## Layout

```
app/
  config.py      typed settings, validated at startup
  contracts.py   shared data models -- the module boundaries
  main.py        entrypoint stub (becomes FastAPI in M06)
tests/
docs/plans/      per-module plans, M00 through M14
```

## Known limitations

- **OAuth tokens expire every 7 days.** Personal Gmail plus restricted scopes means the app stays in "Testing" publishing status, where Google invalidates refresh tokens weekly. Handled with a `reauth` command and a pre-expiry alert (M01, M07) rather than the CASA verification process, which costs hundreds of dollars and months. A Google Workspace account would remove the limit entirely via "Internal" publishing.
- Single user by design. No multi-tenancy, no user system.
