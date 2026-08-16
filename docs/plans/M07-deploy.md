# M07 · Deploy

**Est.** 1 day · **Depends on** M06 · **Blocks** M08

## Goal

Runs unattended on the internet. **This is the ship point** — a deployed, imperfect agent beats a perfect local one.

## Deliverables

- Docker image built in CI
- Deployed to Railway or Fly.io
- Managed Postgres (Neon or Supabase free tier — genuinely fine at this scale)
- Secrets via platform env vars
- APScheduler running the Gmail poll inside the FastAPI process
- `/health` endpoint returning DB connectivity + token expiry
- Telegram webhook URL and Google OAuth redirect URI pointed at the deployed host
- Token-expiry alert wired to Telegram

## One process, not three

```
FastAPI app
├── POST /telegram/webhook   → resumes graphs
├── GET  /health             → DB + token status
└── APScheduler (in-process) → polls Gmail every N minutes
```

Telegram long-polling plus a separate cron worker plus the API is three processes and three failure modes. Webhook + in-process scheduler is one.

If the scheduler ever needs to outgrow this, that's the moment to split it — not before.

## Budget reality

The free tiers named in the original plan have changed: Railway is now trial-credit rather than a standing free tier, and Fly.io's allowances shifted. Expect **~$5/month** for hosting. Postgres on Neon/Supabase free tier is fine.

## The 7-day token check

Personal-Gmail refresh tokens expire after 7 days in "Testing" publishing status (see M01). Schedule a daily job:

```python
if token_health().days_remaining <= 2:
    await bot.send_message(admin_chat_id, "⚠️ Google token expires in 2 days. Run: make reauth")
```

Being alerted before it breaks — rather than noticing a week of silence — is the difference between a known limitation and an outage.

## Deploy checklist

- [ ] Migrations run on boot or as a release command
- [ ] `DRY_RUN=false` in production, `true` everywhere else
- [ ] Fernet key set and **not** the dev one
- [ ] OAuth redirect URI updated in the GCP console to the deployed host
- [ ] `set_webhook` called with the production URL + secret token
- [ ] Poll interval sane (5–15 min; Gmail rate limits are generous but you're paying per LLM call)
- [ ] Logs going somewhere you can read

## Exit criterion

Runs unattended for **48 hours**, processes real mail, and survives a redeploy triggered mid-approval (the pending approval still resumes correctly afterwards).

## After this module

Record the 90-second demo video **now**, while the system is small enough to explain in one breath. Nobody can log into your app — unverified restricted scopes mean only OAuth test users can authenticate — so the video is how most reviewers will ever see it run.

## Running notes

**Credentials are files, but a container gets environment variables.** The OAuth client secret and the encrypted refresh token both live on disk, and `.dockerignore` deliberately keeps them out of the image — baking a refresh token into a layer that lands in a registry is how credentials leak. `app/bootstrap.py` decodes `*_B64` env vars to disk before settings are read, and a malformed value is fatal: booting without credentials would produce a process that looks healthy and silently processes no mail.

**The scheduler is off by default.** `RUN_SCHEDULER=false` locally, so `tasks.ps1 serve` and the test suite cannot quietly start processing real mail. The deployed environment turns it on.

**Poll jobs use `max_instances=1, coalesce=True`.** A slow run must not stack behind itself. `claim()` makes overlapping polls *safe*, but they are still duplicated work and duplicated LLM spend against a per-day quota.

**`auto_stop_machines = false` in `fly.toml`.** Fly's default scale-to-zero is right for a pure request/response service and wrong here: a stopped machine polls no mail and fires no token-expiry warning, which looks exactly like the agent having silently died.

**Shell-form `CMD` so `$PORT` expands.** Most platforms assign the port at runtime; a hardcoded one passes locally and fails the platform's health check.

**Scheduler shutdown is in the lifespan `finally`.** Without it a redeploy can leave a poll mid-flight holding a claimed message with no process behind it.

**A failing scheduled job logs and returns.** APScheduler swallows tracebacks from job functions, so an unhandled exception would be invisible; the next tick retries.

**Health checks degrade, they do not raise.** `/health` reports `degraded` with the reason when the token is unreadable rather than returning a 500, because a 500 tells a load balancer nothing about *what* is wrong.

### Verified locally

```
ruff / format / mypy --strict   pass
pytest                          298 tests
docker build                    pass
container                       app constructs, uvicorn CMD resolves
```

### Not verified: nothing has been deployed

The exit criterion — 48 hours unattended, surviving a redeploy mid-approval — requires a hosting account and a managed Postgres that do not exist yet. `docs/DEPLOY.md` is the runbook.

Two things will surface on first deploy and are worth expecting:

1. **The token upload dance.** Consent cannot run in a container, so `token.enc` is minted locally and carried up base64-encoded, and `FERNET_KEY` must match exactly or it cannot be decrypted.
2. **The seven-day clock does not go away by deploying.** The instance stops processing mail weekly until `reauth` is re-run locally and the new token re-uploaded. The scheduler warns two days ahead over Telegram. A Workspace account and "Internal" publishing is the one change that makes this genuinely unattended.

**Telegram is also the reason to deploy sooner rather than later**: it is blocked on the development network (see M06), and the deployed instance is the first environment where the approval path can actually be exercised.
