# Deploying mailagent

This deploys the whole agent at once, to run **unattended, writing nothing**:
- M15's hardened backend on Fly;
- M16's web app on Vercel, with push to the owner's phones;
- M17's action policy;
- M20's mail sync.

It reads Gmail, parks meeting proposals, and lets the owner decide them from
the phone. It never writes to a calendar while `DRY_RUN` is `true`:
- `DRY_RUN` goes off on the test calendar only for M17's end tests (§11.8);
- it goes off for real use only after M18.

The specs are in [`plans/`](plans/).

**One deploy, not two.** The plan was to run M15 alone for its eight days
(from the tag `m15-day0`), then deploy M16 on top. On 2026-10-02 the owner
chose one deploy of the branch head instead. M15's eight-day check, the
token's seven-day question and the measurements run on the full system. The
order of the day is in [§3](#3-deploy-day-in-order).

The stack, chosen in M15 task 1 (prices and sources are in that module's
running notes), with M16's parts in [§9](#9-m16-the-web-app-and-push):

| Part | Choice | About |
|---|---|---|
| App | Fly.io, `sin` (Singapore), one shared-cpu-1x 512 MB machine with swap | $4 a month |
| Database | Supabase Free, Singapore, **direct** connection | $0 |
| Web app | Vercel Hobby (§9) | $0 |
| Uptime | Better Stack Free, HTTP check on `/health` | $0 |
| Models | Gemini API on the **paid** tier | capped at $40 a month (§11.2) |

One process on Fly: FastAPI serves `/health` and the web app's `/api/*`, and
APScheduler runs every job inside it. The jobs are the poll, the decisions
worker, the mail sync, the watch, the hourly purge and the token check.

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

4. **A production Gmail OAuth app, published.** A Cloud project of its own
   is cleanest. The owner chose an existing one on 2026-10-02, already "In
   production": one consent screen serves every app in a project, so its name
   is what the Gmail grant shows, and Google may ask that project for
   verification.
   - enable the **Gmail API** and the **Google Calendar API**;
   - in *Google Auth Platform*, the user type is External;
   - under *Data Access*, the agent's two scopes may be listed,
     `https://www.googleapis.com/auth/gmail.readonly` and
     `https://www.googleapis.com/auth/calendar.events`. The justification
     and video boxes there belong to a verification request, and stay
     empty. For personal use the list is optional: the sign-in asks for both
     scopes, and the owner continues past the "unverified app" warning;
   - under *Audience*, the status must be **In production**: press **Publish
     app** if it is not, and never **Back to testing**. Do not submit for
     verification: this is personal use by fewer than 100 users. Publishing
     is the change M15 exists to test (see §7);
   - under *Clients*, create a client of type **Desktop app**, download its
     JSON, and save it as `secrets/client_secret-prod.json`. Development keeps
     `secrets/client_secret.json` and its own client.

   The web app's sign-in (§9.1) needs a separate, new project all the same.

5. **A production-only encryption key.** Never reuse your dev key:

   ```powershell
   .\tasks.ps1 fernet
   ```

6. **Mint the primary token** with that key, into its own file. Google shows
   an "unverified app" warning; continue via *Advanced*.

   ```powershell
   $env:FERNET_KEY = "<production key>"
   $env:GOOGLE_CLIENT_SECRETS_PATH = "secrets/client_secret-prod.json"
   $env:GOOGLE_TOKEN_PATH = "secrets/token-prod.enc"
   .\tasks.ps1 reauth --minted-under production
   ```

   Run all four lines in one PowerShell window: the settings last only for
   that window. `--minted-under` is required. Say what the console shows
   *right now*: the status at the moment of consent decides whether the token
   lapses. A token works only with the client it was minted with, so Fly gets
   the production client's file too (§2).

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
| `GOOGLE_CLIENT_SECRETS_B64` | base64 of `secrets/client_secret-prod.json`, the client the token was minted with (§1.4) |
| `GOOGLE_CLIENT_SECRETS_PATH` | `/app/secrets/client_secret.json` |
| `GOOGLE_TOKEN_B64` | base64 of `secrets/token-prod.enc` |
| `GOOGLE_TOKEN_PATH` | `/app/secrets/token.enc` |
| `GOOGLE_TOKEN_STANDBY_PATH` | `/app/secrets/token-standby.enc` (the token itself comes on day 4, §5) |
| `TEST_CALENDAR_ID` | The throwaway calendar. `graph_session` refuses to start without it |
| `OWNER_EMAIL` | Your address |
| `USER_TIMEZONE` | e.g. `Asia/Karachi` |
| `DRY_RUN` | `true`: it goes off only for the end tests (§11.8) |
| `INGEST_ENABLED` | `false`, until M18 strips one-time codes |
| `SEARCH_CONTEXT_ENABLED` | `false`: the production corpus is empty |
| `REVIEWER_ENABLED` | `false` |
| `ALLOWED_CHAT_IDS` | `[]` |
| `TELEGRAM_*` | unset: the web app replaces it (§9) |

Staged with these: M16's three secrets, `WEB_API_SECRET`, `VAPID_PRIVATE_KEY`
and `WEB_APP_URL` (§9.5), and, if the defaults do not suit, M17's two limits
(§11.1).

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

## 3. Deploy day, in order

**Before the day:**
- §1, in order;
- the sign-in project, the keys and the Vercel project's day-1 variables (§9.1, §9.2, §9.4);
- the phone sign-in test (M16 task 16.2).

**The day:** one deploy of the branch head (`v2-plan`), from this working copy.

1. Stage the Fly secrets: §2's, M16's (§9.5), and M17's limits if the defaults do not suit (§11.1). `DRY_RUN=true`.
2. Deploy one machine:

   ```bash
   fly launch --no-deploy        # first time only; keeps the existing fly.toml
   fly deploy --ha=false
   fly scale count 1
   fly status                    # exactly one machine
   ```

   Boot applies every migration, the first time from 001.
3. In Supabase, give `web_reader` its password, and download the CA certificate (§9.3).
4. Set Vercel's deploy-day variables, then redeploy production (§9.4).
5. On each phone, open the app, sign in, and turn on notifications (§9.6, step 4).
6. Run the checks: §4, §9.6 step 5, §10.2 and §11's checklist.

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
$env:FERNET_KEY = "<production key>"
$env:GOOGLE_CLIENT_SECRETS_PATH = "secrets/client_secret-prod.json"
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
ended". It is no longer needed before `DRY_RUN` goes off: since M17, a
restart with `DRY_RUN` off expires every proposal still waiting from dry-run
days (§11.1). It stays for clearing proposals by hand, for instance at the end
of M15's window (exit criterion 5).

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

## 8. What this deploy does not do

- **Turn off `DRY_RUN`.**
  - It goes off on the test calendar for M17's end tests (§11.8), after the probe passes.
  - It goes off for real use only after M18.
  - A proposal records the `DRY_RUN` value it parked under, and M17 refuses a mismatch.
- **Telegram.** The owner's network blocks it, and M16 replaces it with a web
  app.
- **Ingestion.** Off until M18 strips one-time codes and reset links before
  anything is embedded.

---

## 9. M16: the web app and push

M16 adds a web app on Vercel, where the owner sees and decides proposals, and
web push to both phones. It deploys **with everything else**, in the one
deploy of §3 (M16 task 16.23). `DRY_RUN` stays `true`. The spec is
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

Fly's first boot applies every migration (`MIGRATE_ON_BOOT=true`). Migration
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

Three more secrets, staged with §2's before the one deploy (§3):

| Variable | Value |
|---|---|
| `WEB_API_SECRET` | From §9.2 |
| `VAPID_PRIVATE_KEY` | The private half from §9.2 |
| `WEB_APP_URL` | The web app's bare origin, `https://<vercel app>`. Web push sends it to Apple and Google as the VAPID subject. A path, a port or `http://` stops the app at boot, rather than failing every push quietly |

`TELEGRAM_*` stays unset: the web app replaces it.

### 9.6 Deploy day: the web app's part (M16 task 16.23)

The day's order is §3's. The web app's steps:

1. The Fly deploy is §3's, with §9.5's secrets staged alongside §2's.
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
  mail in Primary now reaches the classifier. When the classifier finds no
  meeting, the ledger records "not a meeting", not the model's reasoning.
  Two reasons are still the model's words: a meeting with no start time
  records the extractor's reasoning, and a proposal the reviewer rejects
  records its issues. The purge clears both after a week; M18 strips codes
  before anything stores them.

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
   from the last seven days, up to the moment of the listing, is stored.
4. **The backfill** works back 90 days from `feed_from`, one day at a time,
   in whatever quota is left. A busy mailbox takes hours, and nothing waits
   for it. The feed decides by time: what the backfill stores from the hour
   before `feed_from` is fed (the old poller's last hour, covered twice
   rather than not at all), and anything older never is.
5. If the old poller's history id has expired (about a week), the first run
   is a catch-up instead (10.6).

At every boot, claims a previous process left mid-message are settled: a
parked one is left to reconciliation, one the feed would offer again is
released, and the rest are marked FAILED ("stranded by shutdown"). A claim
under ten minutes old waits for a second pass ten minutes after boot, in
case a poller in another process (a CLI pass) still holds it. A claim that
cannot be settled is logged and left; boot completes regardless.

### 10.3 The quota

Gmail allows 6,000 units per user per minute for everything. The sync,
its queue, its catch-up, its backfill and the daily recall spend at most
2,000; the rest is the pipeline's and M17's. The recall holds the sync's
lock while it checks, so the two never run at once. A fetch costs 20 units,
a listing 5, a history page 2, and every retry costs the same again. One
Gmail call -- waiting for the quota, retrying a 429 or a 5xx -- takes at
most about 30 seconds in all.

The pacer counts per process. M15's `measure` runs in its own process, so
do not run it while a backfill or a catch-up is in progress (`--status`
says).

### 10.4 Watching it

- **`/health`** returns 503 ("no mail sync pass reached the end of history
  in three intervals") when no pass has caught up for six minutes, after a
  two-minute boot grace. A sync stuck behind a backlog counts as down.
- **Every fetch failing** -- a field mask or a policy Gmail refuses -- still
  lets each pass reach the end of history. A run that tried three fetches or
  more and fetched none is recorded not ok ("every fetch failed"), and
  `/health` returns 503 ("every mail sync fetch has failed for three
  intervals") until a run fetches a message again.
- **A stale sync holds the feed.** While no pass has caught up for 30
  minutes, the feed offers nothing: the stored labels may be out of date. A
  message the owner trashed or marked as spam meanwhile is also caught by
  the pipeline's own fetch, and recorded SKIPPED ("no longer in the
  mailbox").
- **With the bearer secret** (`Authorization: Bearer $WEB_API_SECRET`),
  `/health` shows `mail_sync`: the cursor's age, since when every fetch has
  failed (`fetches_failing_since`), `feed_from`, the backfill's reach, the
  fetch queue (queued, unreadable), any catch-up, the too-old count, the row
  count, the last recall, and the latest recall attempt's failure, if it
  failed (`last_recall_failure`).
- **`job_runs`:** `mail_sync` (at most every ten minutes per outcome, and
  every catch-up), and three rows a day from the recall:
  `mail_recall_sync`, `mail_recall_feed`, `mail_recall_categories`. The
  recall job wakes hourly and checks at its first wake after 05:15 UTC; a
  restart costs an hour at most, and a failed attempt is a `mail_recall` row
  that is not ok, tried again the next hour.
- **Alerts,** once a day each, through every configured channel: "Mail sync
  missed messages" (Gmail listed mail the sync did not have -- it is stored
  and fed as it is found -- or a category disagreed), and "The mail feed has
  stalled" (mail met the feed's rule for over an hour without being
  processed, not counting time paused or stopped by the spending cap; the
  fetch queue held a message back for over six hours; or the age rule
  skipped mail under a day old).
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
fly ssh console -C "sh -c 'cd /app && python -m app.mail.sync --retry-unreadable'"
fly ssh console -C "sh -c 'cd /app && python -m app.mail.sync --check-feed'"
```

| Option | What it does |
|---|---|
| `--status` | The cursor, `feed_from`, the backfill, queue and catch-up, counts by direction, category and how rows arrived, the latest too-old records, the last recalls |
| `--show <id>` | One row's metadata, its ledger status and any queue entry. No content: none is stored |
| `--once` | One run, as the scheduler does |
| `--catch-up` | Records a gap from an hour before the last caught-up pass to now, as if the cursor had expired, and queues unreadable mail again. The scheduled runs work it off |
| `--retry-unreadable` | Queues every unreadable message again with no strikes, within the backfill's 90 days. Run it once whatever failed them is fixed |
| `--check-feed` | The exit criterion's checks. The second asks Gmail for the switch-over hour and gives each message a verdict -- processed, held (it says why) or left out by the feed's rule; a message never stored, or met by the rule and left waiting, fails it. Exits 1 on any failure |

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
unreadable and passed over; `/health` counts them, and `--retry-unreadable`
queues them again. An outage (5xx, 429, the
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

## 11. M17: the action policy

Everything the agent does to the world now goes through one registry, under
an approval bound to the exact arguments and to the `DRY_RUN` the owner saw.
Calendar writes can be finished after a crash without booking twice; an
invite's guests must be in the email's thread or allowed by the owner;
model spending stops at a monthly cap; the owner can pause the agent and
withdraw a queued decision; and every attempt is in an append-only audit
log. Spec: [`docs/plans/M17-action-policy.md`](plans/M17-action-policy.md).

`DRY_RUN` stays `true` until the owner's end tests (11.8).

### 11.1 Before the deploy

- Migration `010_action_policy.sql` applies at boot with
  `MIGRATE_ON_BOOT=true`, before `011`. It is additive and can be re-run. It
  gives `web_reader` read access to `control`, `confirmed_contacts` and
  `audit_log`, for the web app's header, cards and Activity page.
  `012_review_fixes.sql` follows `011`:
  - it lets the budget's state say that a model has no price (11.2);
  - it indexes the audit log by decision.
- `FERNET_KEY` must be set on Fly: it keys the hash every approval binds,
  and the audit log's record of a contact. Without it, Allow answers 503.
- Two optional settings: `MONTHLY_BUDGET_USD` (default 40) and
  `MESSAGE_CEILING_USD` (default 0.50). The defaults are the owner's.
- Development uses its own Gemini API key, so its spend never hides inside
  production's budget. The cap counts what each database's `model_spend`
  records, not what Google bills a key.
- At boot, and hourly, reconciliation expires a proposal made under the
  other `DRY_RUN` ("made under another mode"): turning `DRY_RUN` off expires
  every proposal still waiting from dry-run days, and the reverse. Decide
  what is waiting before switching.
- Confirming from the command line now needs the token that `approve
  --list` prints for the proposal: what the owner is confirming. A token
  from before the proposal last changed, or from the other mode, is refused
  as stale, on the command line as on the web and Telegram:

  ```bash
  fly ssh console -C "sh -c 'cd /app && python -m app.jobs.approve --message-id <id> --action confirm --expect <token>'"
  ```

### 11.2 The spending cap

- **What counts:** every model call, metered as it is made and recorded in
  `model_spend` with no content: the model, the message it served, tokens and
  cost. Embeddings are estimated at four characters a token. A call the gate
  refuses is recorded too, at no cost. The month is the UTC calendar month.
- **Where work stops,** once the month's spend plus a $0.10 reserve reaches
  the cap:
  - poll claims nothing, and mail waits in the feed. A message stopped mid-run
    goes back to the feed, from the start, never FAILED. The tick still
    records as successful. Mail that waits more than seven days is skipped
    as too old (10.4), so a long cap is better raised than waited out;
  - an Edit waits, costing no attempt; its card says so. Confirm and Cancel
    call no model, and still apply;
  - ingestion stops between batches. Its scheduled runs cover only their own
    window, so after a stop longer than that, run
    `python -m app.jobs.ingest_job --backfill` to fill the gap.
- **One message** may spend at most `MESSAGE_CEILING_USD`. Past that it is
  recorded SKIPPED ("too costly to read"), and audited.
- **Alerts,** through every configured channel, with no amounts in the
  words: "Model spending is at 80% of this month's cap", then "Model spending
  cap reached: mail processing has stopped". Each is sent once per month and
  cap value; the second fires when new work stops, $0.10 short of the cap. An
  alert no channel delivers is offered again hourly. The web app's header
  says the same while it lasts.
- **Raising the cap:** `fly secrets set MONTHLY_BUDGET_USD=60`, which
  restarts the machine. The alerts re-arm for the new value, the header
  clears within five minutes, and held work moves on. A new month does the
  same by itself.
- **A model in use with no price:**
  - `/health` returns 503 ("a model in use has no price");
  - the gate refuses every call to it, and new work waits as it does at the cap;
  - the web app's header says new work has stopped, and a held Edit's card says why;
  - no budget alert goes out: nothing was spent, and the 503 is what the uptime monitor sees.

  Add its rate to `app/obs/pricing.py`, or switch back to a priced model. The
  embedding model counts only while search or ingestion is on.

### 11.3 Pause and Resume

From the web app's header, on any page, or the command line:

```bash
fly ssh console -C "sh -c 'cd /app && python -m app.jobs.control pause'"
fly ssh console -C "sh -c 'cd /app && python -m app.jobs.control resume'"
fly ssh console -C "sh -c 'cd /app && python -m app.jobs.control status'"
```

Locally, `.\tasks.ps1 pause`, `.\tasks.ps1 resume` and `.\tasks.ps1 status`
run against the database `.env` names. Each prints that database's host:
check it before trusting a pause. Every change is audited, with where it came
from: the Activity page says "from the web app" or "from the command line".

- **Resume asks first in the web app.** The first tap shows the question,
  "Keep paused" or "Resume now". "Resume now" answers only after a moment,
  so a double tap on Resume changes nothing.

  Resume releases every held decision at once, and a Confirm it sends cannot
  be called back. A Resume from the command line does not wake the worker:
  its next tick, within fifteen seconds, carries on.
- **While paused,** within one tick:
  - poll claims nothing;
  - ingestion claims nothing;
  - the worker applies no decision. It reads Pause before each decision, and
    again just before it resumes a thread;
  - the registry refuses any action already on its way.

  A decision held this way costs no attempt and applies as soon as the agent
  resumes.
- **What carries on:** reads, reconciliation, the purge, the token check and
  the mail sync. None calls a model. Notifications still go out: a
  reconciled proposal is announced, and a token alert is sent.
- **`/health`** stays 200. With the bearer it shows `paused`, as of the
  decisions job's last tick (every fifteen seconds); the web app's header
  reads it live. Held decisions do not count toward the one-hour clock for
  stuck ones. Resume moves each held decision on by the length of the pause:
  one that was already overdue before it stays overdue, and is still
  reported.

### 11.4 Withdraw

A card being applied ("Applying…") offers Withdraw. The worker carries the
request out before anything else, even while paused. It reads the request as
it takes the decision, so one made a moment after the worker began its pass
is still seen first.

- **If nothing has run yet,** the decision is withdrawn and the proposal
  comes back at a new generation, so the old card's Confirm is refused as
  stale.
- **If a Pause caught a Confirm at its last step,** before its calendar write
  began, Withdraw still stops it. The card cannot come back, because the
  agent is past the point where it asked: the proposal ends as rejected,
  "withdrawn by the owner". Nothing is sent.
- **If the decision is already being applied** -- a calendar write begun, or
  an Edit's re-extraction under way -- the request is declined, the card says
  "Already being applied", and the decision goes on. A request made while
  the decision is being applied is declined as it finishes. The Activity page
  shows it.
- **A withdraw that fails** is tried again every five minutes. The decision
  is not applied meanwhile, and `/health` reports a request more than an
  hour old as stuck.

To stop a queued Confirm for certain: Pause, then Withdraw, then Resume.

Accepted, as they are:
- **The decisions job needs Google's token** to carry out a withdraw. During
  a token outage the request waits, then runs before the decision could be
  applied.
- **After a restart in the middle of a pass,** the decision that pass held
  waits up to thirty minutes for its lease to lapse. Its withdraw request
  then runs first.
- **A Confirm held at its last step** -- by a Pause, or by Gmail being down
  -- whose guests change meanwhile fails when it runs ("guests outside the
  thread"). Nothing is sent, but the owner must make the event again.

### 11.5 Guests outside the thread

An invite's guest counts as in the thread when they were a recipient of the
owner's sent mail in it, or sent mail in it that Gmail authenticated
(`dmarc=pass` for their domain), or the owner allowed them. Anyone else is
marked on the card, and Confirm is refused until each is allowed or the
proposal is edited to drop them.

- **Allow:** the card's Allow button, or `approve --allow <address>`. An
  allowed contact stays allowed.
- **Remove:** on the command line only, so an allowance is never undone by
  a stray tap:

```bash
fly ssh console -C "sh -c 'cd /app && python -m app.jobs.contacts --remove <address>'"
```

- **Gmail down:** a Confirm waits up to an hour for the thread to be read,
  costing no attempt. The hour runs from the later of the Confirm and the
  last Resume, so a long pause does not use it up. After that its guests count
  as outside, and the proposal comes back. Cancel and Edit never wait.

### 11.6 Watching it

- **`/health`** with the bearer adds `budget` (state, the month's spend, the
  cap, and when the watch last read them), `unpriced_models`, `paused`, and
  `unconfirmed_writes`. Its stuck-queue check now reads "a decision has been
  due for over an hour". A decision waiting for its next attempt, or held by
  a pause, the cap or a model with no price, is not due. A withdraw request
  over an hour old counts, paused or not.
- **A calendar write that could not be confirmed:** the write began, the
  attempts ran out, and Google could not be asked whether the event exists.
  Nothing is settled on a guess: the decision stays open and asks Google
  again every hour. One alert per decision ("A calendar write could not be
  confirmed"); `/health` counts them apart from stuck decisions. Check the
  test calendar; it settles by itself once Google answers.
- **The Activity page** (`/activity`) lists the latest 100 audit entries:
  every attempt to act, refusals included, and every Pause, Resume,
  Withdraw, budget change and contact change. The log holds no email
  content.
- **`job_runs`:** a `watch` row when the job that reads the budget and sends
  these alerts fails, at most every half hour.
- **What is kept.** `outbound_actions`, `model_spend` and `audit_log` are
  kept for good, as M24's evidence and the budget's; none holds email
  content. A calendar write's stored request is cleared once the write is
  done, and by the purge a week after it began. Confirmed contacts stay until
  removed (11.5). The audit log is append-only: a trigger refuses `UPDATE`
  and `DELETE`. It guards against the code, not against the database's owner,
  who can still `TRUNCATE` it.

### 11.7 The calendar probe

```powershell
uv run python -m app.jobs.calendar_probe
```

On the test calendar only, with a fresh id each run, it checks the two
behaviours a re-driven write relies on: an id already taken is refused with
a `409` rather than booked twice, and a deleted event keeps its id, so a
re-drive never recreates an event the owner removed. It is the one tool that
ignores `DRY_RUN`, and says so before it writes. Run it before `DRY_RUN`
goes off; if it fails, `DRY_RUN` stays on.

### 11.8 The owner's end tests (exit criterion)

After the probe passes, with `DRY_RUN` off on the test calendar:

1. **Bound.** Confirming a hold creates exactly one event. Then the mode
   check: Pause; Confirm a proposal made under dry run; set `DRY_RUN=false`
   and restart; Resume. The proposal is expired ("made under another mode"),
   nothing is booked, and the old card's Confirm is refused as stale.
2. **Finishable.** Shown by the fault-injection tests on Neon.
3. **Recipients.** An invite whose guest appears only in an inbound `Cc`
   cannot be confirmed until that guest is allowed.
4. **Cap.** With `MONTHLY_BUDGET_USD` set below the month's spend: polling
   stops and a push arrives; `model_spend` shows only refusals from then on;
   raising the cap restarts polling.
5. **Pause.** Pause stops polling and applying within one tick; Withdraw
   returns a queued decision, and its old card cannot confirm it; Resume
   restarts both.
6. **Audit.** Every attempt has an audit row, and none quotes an email.

Before `DRY_RUN` goes off for real use, beyond these tests, choose the
calendar, and whether an invite should email its guests. Events go to
`TEST_CALENDAR_ID`, and the insert does not set `sendUpdates`, so Google
sends guests no invitation, though its documentation warns some emails may
still go out.

---

## Checklist

- [ ] Billing enabled and budget alert set **before** the first deploy
- [ ] Supabase direct connection string, `vector` extension enabled, Data API off, SSL enforced
- [ ] Production OAuth project with the Gmail and Calendar APIs, In production; its Desktop client saved as `secrets/client_secret-prod.json`
- [ ] Primary token minted with that client, `--minted-under production` and the production key
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
- [ ] M16's Fly secrets staged with §2's, before the one deploy (§3)
- [ ] `web_reader` password set, and its SQL editor query deleted
- [ ] Vercel deploy-day variables set, Supabase's CA certificate included, then redeployed
- [ ] Both phones signed in with notifications on; `/health` with the bearer shows at least 2 subscriptions

**M20**

- [ ] Migration 011 applied; `OWNER_EMAIL` and `OWNER_ALIASES` complete
- [ ] First run: `--status` shows the cursor, `feed_from` at the mailbox's present (a fresh database, §10.2), the switch-over listed
- [ ] `/health` 200, and with the bearer `mail_sync.cursor_age_seconds` under two minutes
- [ ] No `measure` run while the backfill or a catch-up is in progress
- [ ] The exit criterion (10.8), then seven clean days of recall

**M17**

- [ ] Migrations 010 and 012 applied; `FERNET_KEY` set on Fly
- [ ] Development runs on its own Gemini API key
- [ ] `/health` 200, and with the bearer `budget.state` is `ok` and `paused` is false
- [ ] Pause and Resume from the header; the Activity page shows both
- [ ] The web app opened once on each phone, so the new service worker knows the new alerts' tags
- [ ] The probe passes; only then `DRY_RUN=false`, after deciding what is still waiting (11.1)
- [ ] The exit criterion (11.8)
