# M01 · Google auth & API clients

**Est.** 1 day — budget a full evening for OAuth alone · **Depends on** M00 · **Blocks** M03, M04

## Goal

Read Gmail and write Calendar events from a script, with a token that survives restarts and a documented answer to the 7-day expiry problem.

This is the most annoying module in the plan and everything downstream is blocked on it. When it stalls, switch to M02.

## Deliverables

- GCP project with Gmail API + Calendar API enabled
- OAuth 2.0 credentials (**Desktop app** type — simplest local flow)
- Scopes, minimal set:
  - `https://www.googleapis.com/auth/gmail.readonly`
  - `https://www.googleapis.com/auth/calendar.events`
- `app/google/auth.py` — flow, token persistence, refresh
- `app/google/gmail.py` — `list_unread(since_history_id)`, `get_message(id) -> EmailMessage`
- `app/google/calendar.py` — `create_event(...)`, `delete_event(id)`, `freebusy(start, end)`
- `python -m app.google.reauth` — one command to re-run the consent flow
- `token_health()` returning `expires_at` and days remaining
- A **dedicated test calendar** (create it manually in Google Calendar; put its ID in config)

## The 7-day problem — read this before starting

Gmail read scope is **restricted**; Calendar write is **sensitive**. An unverified app stays in "Testing" publishing status, and **Google invalidates refresh tokens after 7 days in that state.** Your deployed bot will work all week and then silently stop.

Full verification requires a CASA security assessment — hundreds of dollars and months of turnaround. Not worth it here.

**How this plan handles it:**

1. Token stored encrypted (Fernet key from env), never in a committed file
2. `reauth` command documented in the README as a known weekly ritual
3. `token_health()` surfaced as a Telegram alert at T-2 days (wired up in M07)
4. README explains the verification tiers and the tradeoff

Write that README section **now**, while the pain is fresh. It's a genuinely good interview answer about OAuth verification tiers and knowing when not to over-invest.

**Escape hatch to note in the README:** a Google Workspace account (~$6/mo, custom domain) lets you set the app to "Internal" publishing, which removes the 7-day expiry entirely. One config change if you ever want it.

## Key decisions

- Add your own Gmail address as an OAuth **test user**, or the flow will reject you.
- The consent screen will show an "unverified app" warning. Expected — click through the advanced link.
- All calendar writes go to the test calendar ID until M06.
- Every write path respects `DRY_RUN`.
- Fetch message bodies with `format="full"` and walk the MIME parts; prefer `text/plain`, fall back to stripped `text/html`. Base64url-decode, don't assume utf-8.

## Exit criterion

One script lists 10 real emails **and** creates-then-deletes an event on the test calendar. With `DRY_RUN=true`, the same script performs zero writes.

## Running notes

**The planned `token_expires_at` health check was measuring the wrong thing.** `Credentials.expiry` is the *access* token — roughly an hour, refreshed transparently. It says nothing about the seven-day refresh-token deadline, and Google exposes no API that does.

So `TokenStore` records `issued_at` itself and counts forward. The clock resets **only** on a fresh consent flow: refreshing an access token reuses the same refresh token and must not extend it, so `save()` preserves the existing `issued_at` unless a caller passes a new one explicitly. `test_refresh_does_not_extend_the_seven_day_clock` pins that behaviour. Get this backwards and the health check cheerfully reports "6 days left" forever while the token is already dead.

**Enabling the APIs is a separate step from creating the OAuth client.** Consent succeeded and stored a valid token, then the first Gmail call failed with `403 accessNotConfigured`. Auth working proves nothing about whether the API is switched on in the project.

**`extract_body` is a pure function on purpose.** It takes a payload dict, not a service, so the eight nesting/encoding cases are tested without network or credentials. Worth keeping that shape as more real-world payloads break it.

### Verified

```
DRY_RUN=true    10 messages listed, zero writes
DRY_RUN=false   created iuiv5vffm5... then deleted it
token health    7.0 days remaining
```

`DRY_RUN` restored to `true` afterwards — it stays that way until M06.

**Inbox observation for M02:** the live unread set is entirely job alerts and application receipts — not one meeting email. Good news for the false-positive half of the eval set, but the meeting cases will have to be sourced deliberately rather than scraped from whatever happens to be unread.
