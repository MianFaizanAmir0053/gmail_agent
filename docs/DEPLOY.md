# Deploying mailagent (M15: observe mode)

This deploys the existing pipeline to run **unattended, writing nothing**. It
polls Gmail, classifies, and parks meeting proposals. It never touches a
calendar, because `DRY_RUN` stays `true` until M17 binds approvals to the
setting they were made under. The spec is
[`plans/M15-go-live.md`](plans/M15-go-live.md).

The stack, chosen in M15 task 1 (prices and sources are in that module's
running notes):

| Part | Choice | About |
|---|---|---|
| App | Fly.io, `sin` (Singapore), one shared-cpu-1x 512 MB machine with swap | $4 a month |
| Database | Supabase Free, Singapore, **direct** connection | $0 |
| Uptime | Better Stack Free, HTTP check on `/health` | $0 |
| Models | Gemini API on the **paid** tier | measured in M15 |

One process: FastAPI serves `/health`, and APScheduler runs the poll, the
hourly purge and the token check inside it.

---

## 1. Before the first deploy (owner)

Do these in order. A message that fails on day one stays `FAILED`, because the
ledger never offers it to the poller again. So billing comes first.

1. **Paid tier.** Enable billing on the Google Cloud project behind your
   Gemini API key, and confirm in AI Studio that it shows a paid tier. Free-tier
   prompts may be used to improve Google's products, including by human
   reviewers; real mail does not go there.

2. **Budget alert.** In Cloud Billing, set a budget alert at **$50 minus the
   hosting and database list prices**, about $45 with this stack. It sees
   Google Cloud spend only.

3. **Database.** Create a Supabase Free project in Singapore.
   - Enable the `vector` extension (Dashboard → Database → Extensions).
   - Copy the **direct** connection string: `db.<project>.supabase.co:5432`. It is IPv6-only on the free plan, and Fly machines reach it over IPv6. The session pooler on port 5432 also works.
   - **Never use port 6543**, the transaction pooler: the app refuses to start on it, because it breaks LangGraph's checkpointer.

4. **Publish the OAuth app.** In Cloud console, open *OAuth consent screen →
   Audience* and press **Publish app**. Keep the user type External and do
   not submit for verification: this is personal use by fewer than 100 users.
   This is the change M15 exists to test (see §7).

5. **A production-only encryption key.** Never reuse your dev key:

   ```powershell
   .\tasks.ps1 fernet
   ```

6. **Mint the primary token** with that key, into its own file. Google shows
   an "unverified app" warning; continue via *Advanced*.

   ```powershell
   $env:FERNET_KEY = "<production key>"
   $env:GOOGLE_TOKEN_PATH = "secrets/token-prod.enc"
   .\tasks.ps1 reauth --minted-under production
   ```

   `--minted-under` is required. Say what the console shows *right now*: the
   status at the moment of consent decides whether the token lapses.

7. **Uptime monitor.** In Better Stack, create an HTTP monitor on
   `https://<app>.fly.dev/health` every 3 minutes, with email alerts. Any
   non-2XX response counts as down. `/health` returns 503 when polling stalls
   or no Google token is usable.

---

## 2. Secrets

Nothing sensitive goes in the image. `.dockerignore` excludes `.env`,
`.env.test`, `secrets/` and `*.enc`. Paths are **absolute**, so jobs run
through `fly ssh console` resolve them the same way the server does.

| Variable | Value |
|---|---|
| `DATABASE_URL` | Supabase direct connection string |
| `GEMINI_API_KEY` | The paid project's key |
| `FERNET_KEY` | The production key from §1.5 |
| `GOOGLE_CLIENT_SECRETS_B64` | base64 of `secrets/client_secret.json` |
| `GOOGLE_CLIENT_SECRETS_PATH` | `/app/secrets/client_secret.json` |
| `GOOGLE_TOKEN_B64` | base64 of `secrets/token-prod.enc` |
| `GOOGLE_TOKEN_PATH` | `/app/secrets/token.enc` |
| `GOOGLE_TOKEN_STANDBY_PATH` | `/app/secrets/token-standby.enc` (the token itself comes on day 4, §5) |
| `TEST_CALENDAR_ID` | The throwaway calendar. `graph_session` refuses to start without it |
| `OWNER_EMAIL` | Your address |
| `USER_TIMEZONE` | e.g. `Asia/Karachi` |
| `DRY_RUN` | `true`: **not** turned off in M15 |
| `INGEST_ENABLED` | `false`, until M18 strips one-time codes |
| `SEARCH_CONTEXT_ENABLED` | `false`: the production corpus is empty |
| `REVIEWER_ENABLED` | `false` |
| `ALLOWED_CHAT_IDS` | `[]` |
| `TELEGRAM_*` | unset: the channel arrives in M16 |

`fly.toml` already sets `APP_ENV=prod`, `RUN_SCHEDULER=true` and
`MIGRATE_ON_BOOT=true`.

Base64 on Windows:

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("secrets\token-prod.enc"))
```

Stage them all, then deploy once:

```bash
fly secrets set --stage DATABASE_URL="..." GEMINI_API_KEY="..." FERNET_KEY="..." ...
```

---

## 3. Deploy

**M15 deploys the tag `m15-day0`, not the branch head.** The branch has since
gained M16's decision queue, and the measurement window must run the code M15
specified. M16 deploys after M15 closes (M16 task 16.23). Deploy from a
worktree of the tag, so the working copy is left alone:

```bash
git worktree add ../mailagent-m15 m15-day0
cd ../mailagent-m15
fly launch --no-deploy        # first time only; keeps the existing fly.toml
fly deploy --ha=false
fly scale count 1
fly status                    # exactly one machine
```

`--ha=false` matters. A second machine would run a second poller, and two boots
would race to apply migrations. `fly.toml` also gives the poller 120 seconds to
finish its current message on shutdown (`kill_timeout`). If a message is killed
anyway, boot marks its claim "stranded by shutdown" rather than leaving it
invisible.

---

## 4. Day 0 checks

```bash
curl -i https://<app>.fly.dev/health
```

Expect `200` with `"dry_run": true`, `"token_state": "production-unconfirmed"`
and `"token_in_use": "primary"`. Within one poll interval of boot,
`last_poll_ok_at` fills in.

**Production jobs run on the instance, never locally.** The local `.env` points
at localhost. Pointing it at the production database would let a local `poll`
claim production rows.

```bash
fly ssh console -C "sh -c 'cd /app && python -m app.jobs.approve --list'"
fly logs            # "INFO app.jobs.scheduler: poll: ..." every interval
```

Run the `approve --list` check on day 0: it proves the ssh route works before
anything depends on it. `job_runs` in the Supabase SQL editor shows every tick:

```sql
SELECT job, ok, count(*) FROM job_runs GROUP BY 1, 2 ORDER BY 1, 2;
```

On day 1, send yourself a meeting email from another account, so a proposal
parks and the mid-window redeploy (M15 task 19) has something to preserve.

---

## 5. Day 4: the standby token

Mint a second production token, the same way as the primary, into its own
file:

```powershell
$env:GOOGLE_TOKEN_PATH = "secrets/token-standby-prod.enc"
.\tasks.ps1 reauth --minted-under production
```

```bash
fly secrets set GOOGLE_TOKEN_STANDBY_B64="<base64 of token-standby-prod.enc>"
```

`/health` now shows `standby_token_state`. If Google rejects the primary on
day seven, polling carries on with the standby. The rejection is recorded, and
`/health` reports `"token_in_use": "standby"` instead of going down.

---

## 6. Running jobs against production

Always through `fly ssh console`:

```bash
fly ssh console -C "sh -c 'cd /app && python -m app.jobs.approve --list'"
fly ssh console -C "sh -c 'cd /app && python -m app.jobs.approve --sweep-all'"
```

`--sweep-all` ends every parked proposal with the reason "swept: observe mode
ended". It is the last step of M15, and it must run before `DRY_RUN` is ever
turned off.

---

## 7. The seven-day question

While the OAuth app was in "Testing", Google invalidated refresh tokens
weekly. Publishing it "In production" without verification reportedly removes
that. M15 proves it with evidence rather than assuming it:

| Token state | Meaning |
|---|---|
| `production-unconfirmed` | Minted under production; seven days have not yet passed with a successful refresh |
| `production-confirmed` | A refresh succeeded **after** day seven. Recorded in `job_runs` and reloaded at boot |
| `expired` | Google rejected the token (`invalid_grant`) |

If the primary expires, "In production" did not lift the limit. The standby
keeps the window going, and the owner chooses a fallback: Testing status
with a weekly `reauth`, or a Workspace mailbox with an "Internal" app.

---

## 8. What M15 does not do

- **Turn off `DRY_RUN`.** A proposal records the `DRY_RUN` value it parked
  under, and M17 makes `act` refuse a mismatch. Until then, writing is off.
- **Telegram.** The owner's network blocks it, and M16 replaces it with a web
  app.
- **Ingestion.** Off until M18 strips one-time codes and reset links before
  anything is embedded.

---

## Checklist

- [ ] Billing enabled and budget alert set **before** the first deploy
- [ ] Supabase direct connection string, `vector` extension enabled
- [ ] OAuth app published; primary token minted with `--minted-under production` and the production key
- [ ] Secrets staged with absolute paths; `DRY_RUN=true`
- [ ] `fly deploy --ha=false`; `fly status` shows one machine
- [ ] `/health` returns 200 with `production-unconfirmed`, and `last_poll_ok_at` fills in
- [ ] `approve --list` works over `fly ssh console`
- [ ] Better Stack monitor green
- [ ] Day 1: planted meeting email parks a proposal
- [ ] Day 4: standby token uploaded; `/health` shows `standby_token_state`
