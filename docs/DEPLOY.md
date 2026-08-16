# Deploying mailagent

One process: FastAPI serves the Telegram webhook and the health check, and
APScheduler runs the Gmail poll inside it. Long-polling plus a cron worker plus
an API would be three deployables and three failure modes.

Budget roughly **$5/month**. The free tiers named in older guides have changed:
Railway is trial credit now, and Fly's allowances shifted. Postgres on Neon or
Supabase free tier is genuinely fine at this volume.

---

## 1. Database

Any managed Postgres works. The app needs the `pgvector` extension from M10
onward, so pick a provider that offers it now rather than migrating later —
Neon and Supabase both do.

Take the connection string; it becomes `DATABASE_URL`.

---

## 2. Secrets

Nothing sensitive belongs in the image. `.dockerignore` excludes `.env`,
`secrets/`, and the token file, so they must arrive as environment variables.

| Variable | Notes |
|---|---|
| `DATABASE_URL` | From step 1 |
| `GEMINI_API_KEY` | Google AI Studio |
| `FERNET_KEY` | Must match the key that encrypted `token.enc` |
| `GOOGLE_CLIENT_SECRETS_PATH` | See step 3 |
| `GOOGLE_TOKEN_PATH` | See step 3 |
| `TEST_CALENDAR_ID` | Keep pointing at the throwaway calendar until confident |
| `TELEGRAM_BOT_TOKEN` | From `@BotFather` |
| `TELEGRAM_WEBHOOK_SECRET` | Any random string |
| `ALLOWED_CHAT_IDS` | JSON list, e.g. `[812345678]`. Empty rejects everyone |
| `PUBLIC_URL` | `https://<app>.fly.dev` |
| `OWNER_EMAIL` | Stripped from attendee lists |
| `USER_TIMEZONE` | e.g. `Asia/Karachi` |
| `DRY_RUN` | **Leave `true` for the first deploy** |

Fly:

```bash
fly secrets set DATABASE_URL="postgresql://..." GEMINI_API_KEY="AIza..." FERNET_KEY="..."
```

---

## 3. The OAuth token problem

This is the awkward part, and it is inherent rather than a shortcut.

The refresh token is obtained by an interactive browser consent flow, which
cannot run in a container. It must be minted locally and carried up.

Both files are small, so the practical route is to base64 them into env vars and
write them to disk at boot, or mount them as platform secret files. Fly supports
files directly:

```bash
fly secrets set --stage GOOGLE_CLIENT_SECRETS_B64="$(base64 -w0 secrets/client_secret.json)"
fly secrets set --stage GOOGLE_TOKEN_B64="$(base64 -w0 secrets/token.enc)"
```

…then decode them in an entrypoint before uvicorn starts. Keep `FERNET_KEY`
identical to the local one, or the token cannot be decrypted.

**The seven-day clock still applies.** While the OAuth app sits in "Testing"
publishing status, Google invalidates the refresh token weekly, so the deployed
instance stops processing mail every seven days until `reauth` is re-run locally
and the new `token.enc` re-uploaded. The scheduler warns two days ahead over
Telegram. A Google Workspace account and "Internal" publishing removes the limit
entirely — it is the single change that makes this genuinely unattended.

---

## 4. Deploy

```bash
fly deploy
```

`MIGRATE_ON_BOOT=true` applies pending migrations at startup. That is safe on
one instance and wrong on two — both would race. Scale past one machine and
migrations move to a release command.

---

## 5. Point Telegram at it

```bash
fly ssh console -C "python -m app.jobs.telegram_bot --set-webhook"
```

Registers `${PUBLIC_URL}/telegram/webhook` with the secret token. Telegram sends
that secret in a header on every call, and the webhook rejects anything else —
otherwise the URL is the only thing between the internet and a bot that writes
to a calendar.

Verify:

```bash
curl https://<app>.fly.dev/health
```

Expect `{"status": "ok", "dry_run": true, "token_days_remaining": 6.4}`.

---

## 6. Google redirect URI

Add `https://<app>.fly.dev` to the OAuth client's authorised redirect URIs in
the Cloud console. Only needed if consent is ever re-run from the deployed
instance; the local desktop flow does not use it.

---

## Turning off the safety catch

`DRY_RUN=true` means the agent reads mail, proposes events, and writes nothing.
Leave it that way until you have watched a full cycle: a card arriving, Confirm
reporting "Dry run — nothing written", and the ledger updating.

Then:

```bash
fly secrets set DRY_RUN=false
```

`TEST_CALENDAR_ID` still points at the throwaway calendar, so the first real
writes land somewhere harmless. Move it to your primary calendar only after
that looks right.

---

## Checklist

- [ ] Migrations applied (check the boot logs)
- [ ] `/health` returns `ok` with a plausible `token_days_remaining`
- [ ] Webhook registered; a button tap produces a log line
- [ ] Poller logging every `POLL_INTERVAL_MINUTES`
- [ ] A redeploy triggered mid-approval still resumes afterwards
- [ ] 48 hours unattended without manual intervention
