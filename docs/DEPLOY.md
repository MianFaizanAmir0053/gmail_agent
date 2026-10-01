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

M16 adds the web app and web push on top of this stack, after M15 closes.
Its steps are in [§9](#9-m16-the-web-app-and-push).

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
   - **Turn the Data API off** (Dashboard → Integrations → Data API → *Enable Data API* off). Supabase grants every table in `public` to the roles its REST and GraphQL endpoints use, so its anon key could otherwise read the checkpoints, which hold whole emails. This app never uses those endpoints. M16's migration 009 also removes the grants.
   - **Enforce SSL** (Dashboard → Database → Settings → SSL Configuration). Supabase accepts unencrypted connections unless told otherwise.
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

## 9. M16: the web app and push

M16 adds a web app on Vercel, where the owner sees and decides proposals, and
web push to both phones. It deploys **after M15 closes** (M16 task 16.23),
from the branch head in this working copy rather than from a tag. `DRY_RUN`
stays `true`. The spec is
[`plans/M16-web-channel.md`](plans/M16-web-channel.md).

| Part | Choice | About |
|---|---|---|
| Web app | Vercel Hobby, functions in `sin1` (`dashboard/vercel.json`) | $0 |
| Sign-in | Google, through a **separate** Cloud project | $0 |
| Push | Web push with VAPID keys, sent from Fly | $0 |

The browser never calls Fly. The web app's server reads Supabase as a
read-only role and sends every decision to Fly's `/api/*` with a bearer
secret. Fly's worker applies it.

### 9.1 The sign-in project (owner, day 1)

1. Create a **new** Google Cloud project, separate from the Gmail one. If the
   two shared a project and it fell back to Testing, Google would refuse every
   other account before the web app's allow-list saw them, and the "second
   account is refused" check would prove nothing.
2. On its OAuth consent screen: user type External, scopes `openid`, `email`
   and `profile` only. Then **Publish app**. These scopes need no
   verification.
3. Create an OAuth client of type *Web application*, with these redirect URIs:
   - `https://<vercel app>/api/auth/callback/google`
   - `http://localhost:3000/api/auth/callback/google`, for local runs

   Its client ID and secret go to Vercel (§9.4).

### 9.2 Keys and secrets (owner, on your machine)

| Secret | Generate with | Goes to |
|---|---|---|
| VAPID key pair | `.\tasks.ps1 vapid` | The private half to Fly only; the public half to Vercel |
| `WEB_API_SECRET` | `uv run python -c "import secrets; print(secrets.token_urlsafe(32))"` | Fly **and** Vercel, the same value |
| `AUTH_SECRET` | `npx auth secret` in `dashboard/`, or `openssl rand -base64 33` | Vercel only |
| The `web_reader` password | The same command as `WEB_API_SECRET` | Supabase (§9.3) and, inside the connection string, Vercel |

Paste each value straight into Fly, Vercel or Supabase. Never put one in a
file in the repo, a chat or an issue.

Replacing the VAPID pair later invalidates every push subscription. Each phone
replaces its own the next time the app opens. A browser that subscribes only
from a tap shows **Turn on notifications** again instead.

### 9.3 The database role (owner, deploy day)

Fly's boot applies migrations 007 to 009 (`MIGRATE_ON_BOOT=true`). Migration
008 creates the role `web_reader`, which can read only what the web app shows
and cannot log in yet. Migration 009 takes back what Supabase granted its
Data API roles (§1.3).

1. After that boot, give the role a password in Supabase's SQL editor:

   ```sql
   ALTER ROLE web_reader WITH LOGIN PASSWORD '<password>';
   ```

   Run it as a new query, then delete that query: the editor keeps what it
   ran.

2. The web app connects through the **session pooler**, because the direct
   host is IPv6-only and Vercel has no IPv6 egress. Copy the pooler host from
   *Connect → Session pooler* in the Supabase dashboard:

   ```text
   postgresql://web_reader.<project-ref>:<password>@<pooler host>:5432/postgres
   ```

   The user name is `web_reader.<project-ref>`, not `web_reader`: that is how
   the shared pooler finds the project. **Never port 6543.** Add no
   `sslmode` to it: the web app sets up the encryption itself, and an
   `sslmode` would override that.

3. Download Supabase's CA certificate (*Database → Settings → SSL
   Configuration → Download certificate*). Its contents go to Vercel as
   `DATABASE_CA_CERT`, so the web app checks that it is talking to Supabase.
   The connection is encrypted without it too, but not checked.

### 9.4 The Vercel project (owner)

Created on day 1 for the sign-in test (M16 task 16.2), and completed at
deploy:

- Import the GitHub repo. Set **Root Directory** to `dashboard`; the framework
  is detected as Next.js.
- **Node.js 24.x.** It is Vercel's default, and `engines` in
  `dashboard/package.json` asks for it too. CI builds on the same version.
- Production branch: the branch being deployed (`v2-plan` today).

Environment variables, for Production. Mark the secret ones **Sensitive**, so
they cannot be read back:

| Variable | Value | Set |
|---|---|---|
| `AUTH_SECRET` | From §9.2. Sensitive | Day 1 |
| `AUTH_GOOGLE_ID`, `AUTH_GOOGLE_SECRET` | The sign-in client from §9.1. The secret is Sensitive | Day 1 |
| `OWNER_EMAIL` | Your Google address. Blank admits nobody | Day 1 |
| `DATABASE_URL` | The `web_reader` connection string from §9.3. Sensitive | Deploy |
| `DATABASE_CA_CERT` | The contents of Supabase's CA certificate, from §9.3 | Deploy |
| `FLY_API_URL` | `https://<app>.fly.dev` | Deploy |
| `WEB_API_SECRET` | From §9.2, the same as Fly's. Sensitive | Deploy |
| `OWNER_TIMEZONE` | The same zone as Fly's `USER_TIMEZONE`, e.g. `Asia/Karachi` | Deploy |
| `NEXT_PUBLIC_VAPID_PUBLIC_KEY` | The public half from §9.2 | Deploy |

`NEXT_PUBLIC_VAPID_PUBLIC_KEY` is built into the page, so redeploy after
setting or changing it. Pages that read the database fail until
`DATABASE_URL` is set; on day 1 only sign-in is under test.

### 9.5 Fly (deploy day)

Three more secrets join §2's:

| Variable | Value |
|---|---|
| `WEB_API_SECRET` | From §9.2 |
| `VAPID_PRIVATE_KEY` | The private half from §9.2 |
| `WEB_APP_URL` | The web app's bare origin, `https://<vercel app>`. Web push sends it to Apple and Google as the VAPID subject. A path, a port or `http://` stops the app at boot, rather than failing every push quietly |

`TELEGRAM_*` stays unset: the web app replaces it.

### 9.6 Deploy day, in order (M16 task 16.23)

1. Stage the Fly secrets (§9.5) and deploy, from this working copy:

   ```bash
   fly secrets set --stage WEB_API_SECRET="..." VAPID_PRIVATE_KEY="..." WEB_APP_URL="https://..."
   fly deploy --ha=false
   fly status                    # exactly one machine
   ```

   Boot applies 007 to 009. Reconciliation then gives any proposal still
   parked from M15 its row, so the timeline shows it.
2. Set the `web_reader` password, and download the CA certificate (§9.3).
3. Set Vercel's deploy-day variables (§9.4), then redeploy production.
4. On each phone, open the app and sign in, then tap **Turn on
   notifications** and allow them. On iPhone, first tap Share, then **Add to
   Home Screen**, and open it from there: a Safari tab cannot receive push.
5. Check `/health` with the bearer secret. Without it, `/health` shows only
   M15's public fields:

   ```bash
   curl -i -H "Authorization: Bearer <WEB_API_SECRET>" https://<app>.fly.dev/health
   ```

   Expect `200`, `"push_subscriptions"` of at least 2, and
   `"oldest_open_decision_seconds": null`, which means no decision is waiting
   for the worker.

### 9.7 Revoking a device

Sessions are signed cookies (Auth.js JWTs), so one device cannot be signed
out on its own.

1. Generate a new `AUTH_SECRET` (§9.2), set it in Vercel, and redeploy. Every
   device is signed out.
2. In Supabase's SQL editor, run `DELETE FROM push_subscriptions;`. Notifications
   carry no proposal content, but a lost phone should not keep receiving them.
3. Sign in again on the devices you keep, and open the app on each: it posts
   its subscription again every time it opens.

### 9.8 Pairing, only if iPhone sign-in fails

Built and switched off. Use it only if Google sign-in fails inside the
installed iPhone app.

1. Set `PAIRING_ENABLED=true` on Fly (`fly secrets set PAIRING_ENABLED=true`) and in Vercel, then redeploy Vercel.
2. On a device that is already signed in, open `/me`, follow **Pair the installed iPhone app**, and tap **Show a pairing code**. It lasts five minutes and allows five tries; showing a new one ends the old one.
3. In the installed iPhone app, type the code into the sign-in page's **Pairing code** box.
4. Set both flags back to `false`, and redeploy Vercel. The paired iPhone stays signed in; only rotating `AUTH_SECRET` (§9.7) signs it out.

While pairing is on, anyone who reaches the sign-in page can use up a live
code's five tries, so leave it on only for the minutes it takes.

### 9.9 Left as it was

- `/health`'s token fields from M15 stay public. They name the token's state,
  never its value. Whether they move behind the bearer is to be revisited
  after M16.
- `DRY_RUN` stays `true`, until M17.

---

## 10. M20: mail sync

The agent now reads Gmail's history instead of the newest page of unread
mail: every Primary, Updates, Forums and sent message is stored as metadata
(no subject, snippet or body), and the meeting pipeline reads new Primary
mail from that record, read or not. Spec:
[`docs/plans/M20-mail-sync.md`](plans/M20-mail-sync.md).

### 10.1 Before the deploy

- Migration `011_mail_sync.sql` applies at boot with `MIGRATE_ON_BOOT=true`,
  or by hand with `python -m app.store.db`. It is additive and can be re-run.
- No new secret or setting. `OWNER_EMAIL` and `OWNER_ALIASES` matter more
  than before: mail from any of them to any of them is the owner's own
  (`to_self`) and is never fed to the pipeline.
- Feeding read mail as well as unread means one-time-code and password-reset
  mail in Primary now reaches the classifier. The ledger records "not a
  meeting" for it, never the model's reasoning; M18 strips codes before
  anything stores them.

### 10.2 The first runs

The sync runs every two minutes, each run at most a minute.

1. **The cursor.** The first run starts where the old poller stopped: its
   last stored history id (`sync_state`), and `feed_from` is that pass's
   time. On a fresh database it starts at the mailbox's present, and
   `feed_from` is now. From this run on, poll reads the feed instead of the
   unread page, and stops writing `sync_state`.
2. **The replay.** The first pass replays everything since the old poller's
   last pass. The feed takes mail from `feed_from` less an hour; nothing
   older ever reaches the pipeline.
3. **The switch-over listing,** once: unread mail outside the four other tabs
   from the last seven days is stored.
4. **The backfill** works back 90 days from `feed_from`, one day at a time,
   in whatever quota is left. A busy mailbox takes hours. Nothing waits for
   it, and nothing it stores is fed.
5. If the old poller's history id has expired (about a week), the first run
   is a catch-up instead (10.6).

At every boot, claims a previous process left mid-message are settled: a
parked one is left to reconciliation, one the feed would offer again is
released, and the rest are marked FAILED ("stranded by shutdown").

### 10.3 The quota

Gmail allows 6,000 units per user per minute for everything. The sync,
its queue, its catch-up and its backfill spend at most 2,000; the rest is
the pipeline's and M17's. A fetch costs 20 units, a listing 5, a history page
2, and every retry costs the same again. One Gmail call -- waiting for the
quota, retrying a 429 or a 5xx -- takes at most about 30 seconds in all.

The pacer counts per process. M15's `measure` runs in its own process, so
do not run it while a backfill or a catch-up is in progress (`--status`
says).

### 10.4 Watching it

- **`/health`** returns 503 ("no mail sync pass reached the end of history
  in three intervals") when no pass has caught up for six minutes, after a
  two-minute boot grace. A sync stuck behind a backlog counts as down.
- **With the bearer secret** (`Authorization: Bearer $WEB_API_SECRET`),
  `/health` shows `mail_sync`: the cursor's age, `feed_from`, the
  backfill's reach, the fetch queue (queued, unreadable), any catch-up, the
  too-old count, the row count, and the last recall.
- **`job_runs`:** `mail_sync` (at most every ten minutes per outcome, and
  every catch-up), and three rows a day from the recall at 05:15 UTC:
  `mail_recall_sync`, `mail_recall_feed`, `mail_recall_categories`.
- **Alerts,** once a day each: "Mail sync missed messages" (Gmail listed mail
  the sync did not have -- it is stored and fed as it is found -- or a
  category disagreed), and "The mail feed has stalled" (mail met the feed's
  rule for over an hour without being processed, outside a pause or a
  stopped cap, or the age rule skipped mail under a day old).
- **Too old:** mail first reached more than seven days after it arrived --
  after a long outage, restored from the trash, held by a pause -- is
  recorded SKIPPED ("too old when reached"), never processed.

### 10.5 The command line

On the instance, through `fly ssh console`. Every command waits for a
scheduled run to finish first, and the scheduled run skips its turn while a
command holds the lock.

```bash
fly ssh console -C "sh -c 'cd /app && python -m app.mail.sync --status'"
fly ssh console -C "sh -c 'cd /app && python -m app.mail.sync --show <message id>'"
fly ssh console -C "sh -c 'cd /app && python -m app.mail.sync --once'"
fly ssh console -C "sh -c 'cd /app && python -m app.mail.sync --catch-up'"
fly ssh console -C "sh -c 'cd /app && python -m app.mail.sync --check-feed'"
```

| Option | What it does |
|---|---|
| `--status` | The cursor, `feed_from`, the backfill, queue and catch-up, counts by direction, category and how rows arrived, the latest too-old records, the last recalls |
| `--show <id>` | One row's metadata, its ledger status and any queue entry. No content: none is stored |
| `--once` | One run, as the scheduler does |
| `--catch-up` | Records a gap from an hour before the last caught-up pass to now, as if the cursor had expired. The scheduled runs work it off |
| `--check-feed` | The exit criterion's checks; exits 1 if either fails |

Locally, against the dev mailbox: `uv run python -m app.mail.sync --once`.

### 10.6 Catch-ups

Gmail keeps history for about a week. If the sync has been down longer, the
next run records the gap -- from an hour before its last caught-up pass to
now -- moves the cursor to the present, and queues every stored message
from the last seven days to be fetched again. The gap is listed a day at a
time into the fetch queue, and worked off at the quota's pace; the feed
holds back a message until its re-fetch answers, so mail trashed during the
outage is never fed. Nothing is marked gone for not being listed. Progress
shows in `--status` and `/health`.

A message that fails to fetch or store on its own five times is marked
unreadable and passed over; `/health` counts them. An outage (5xx, 429, the
network) stops a run without counting against any message. A fetch that
fails that way is checked with one cheap call first: if Gmail answers it, the
message alone is struck, and the run goes on.

### 10.7 Retention

The hourly purge deletes mail metadata older than 180 days, and rows a week
after their message left the mailbox. Ledger rows are kept. The row count is
in `/health`.

### 10.8 The owner's end tests (exit criterion)

1. Send a message; within one sync interval `--show <id>` shows it as `out`.
2. Open a new Primary message on the phone before the next tick: it is still
   processed (`approve --list`, or its ledger row). A Promotions message is
   not stored (`--show` says "not stored").
3. Trash a recent Primary message, then run `--catch-up`: the gap is worked
   off (`--status`), the message's labels show TRASH, and it is never fed.
4. `--check-feed` passes once the backfill has reached its first day.
5. `mail_recall_sync`, `mail_recall_feed` and `mail_recall_categories` are
   `ok` in `job_runs` for seven days running.

---

## Checklist

- [ ] Billing enabled and budget alert set **before** the first deploy
- [ ] Supabase direct connection string, `vector` extension enabled, Data API off, SSL enforced
- [ ] OAuth app published; primary token minted with `--minted-under production` and the production key
- [ ] Secrets staged with absolute paths; `DRY_RUN=true`
- [ ] `fly deploy --ha=false`; `fly status` shows one machine
- [ ] `/health` returns 200 with `production-unconfirmed`, and `last_poll_ok_at` fills in
- [ ] `approve --list` works over `fly ssh console`
- [ ] Better Stack monitor green
- [ ] Day 1: planted meeting email parks a proposal
- [ ] Day 4: standby token uploaded; `/health` shows `standby_token_state`

**M16**

- [ ] Sign-in project separate from the Gmail one, published, `openid email profile` only
- [ ] Vercel project: root `dashboard`, Node 24, day-1 variables; a second Google account is refused
- [ ] VAPID pair, `WEB_API_SECRET` and `AUTH_SECRET` generated and pasted straight into Fly and Vercel
- [ ] Fly secrets staged; `fly deploy --ha=false` from the branch head after M15 closes
- [ ] `web_reader` password set, and its SQL editor query deleted
- [ ] Vercel deploy-day variables set, Supabase's CA certificate included, then redeployed
- [ ] Both phones signed in with notifications on; `/health` with the bearer shows at least 2 subscriptions

**M20**

- [ ] Migration 011 applied; `OWNER_EMAIL` and `OWNER_ALIASES` complete
- [ ] First run: `--status` shows the cursor, `feed_from` at the old poller's last pass, the switch-over listed
- [ ] `/health` 200, and with the bearer `mail_sync.cursor_age_seconds` under two minutes
- [ ] No `measure` run while the backfill or a catch-up is in progress
- [ ] The exit criterion (10.8), then seven clean days of recall
