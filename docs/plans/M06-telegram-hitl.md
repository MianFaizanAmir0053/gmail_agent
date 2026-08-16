# M06 · Telegram interface & human-in-the-loop

**Est.** 2 days · **Depends on** M05 · **Blocks** M07

## Goal

Nothing irreversible happens without a human tap. One feature, two résumé bullets: it's the guardrail *and* the human-in-the-loop demo.

## Deliverables

- Telegram bot registered via BotFather
- **Webhook** endpoint on the FastAPI app (not long-polling — see below)
- Chat-ID allowlist middleware
- Approval card: proposed event + inline keyboard `[✓ Confirm] [✎ Edit] [✗ Cancel]`
- Callback handler resuming the graph via `Command(resume=...)`
- Edit flow: free-text correction → re-extract with the correction as added context
- `DRY_RUN` comes off

## Webhook, not long-polling

Long-polling needs a second always-running process alongside FastAPI and the scheduler. A webhook is one route on the app you already have. Set it once at startup:

```python
await bot.set_webhook(f"{settings.public_url}/telegram/webhook", secret_token=settings.tg_secret)
```

Verify the `X-Telegram-Bot-Api-Secret-Token` header on every incoming request.

## Security — the allowlist

Anyone who discovers the bot username can message it, and this bot reads your email. Hard-fail any update whose `chat.id` isn't in `ALLOWED_CHAT_IDS`. Log the rejection.

Small change, real vulnerability, legitimate talking point. Put it in the README's guardrails section.

## The approval card

Render times **in the user's local timezone**, not UTC — you'll misread a UTC card at some point and approve the wrong thing. Show:

- Title, start–end with day name, attendees, location
- Model confidence
- Any free/busy conflict detected in M04 (`⚠️ overlaps with "Standup"`)
- A link to the source email

Keep the callback payload small — Telegram caps `callback_data` at 64 bytes. Send `action:thread_id`, look the rest up.

## Edit flow

Confirm and Cancel are easy. Edit is where the interesting behaviour lives:

1. User taps Edit → bot replies with a force-reply prompt
2. User types "it's 4pm not 3pm, and Sara is coming"
3. Re-run extraction with the original email **plus** the correction as additional context
4. Send a fresh approval card

Log corrections. They are free labeled data for the M02 eval set and the most honest source of failure examples you'll get.

## Exit criterion

Real email → Telegram card → tap Confirm → event appears on your **real** calendar. Cancel writes `status='rejected'` and creates nothing.

## Running notes

**The handler is a function over an update dict, not a framework callback.** The same code path serves the production webhook and the development poller, and tests drive it with plain dictionaries — no Postgres, no Gmail, no Gemini, no HTTP.

That required a `SessionLike` Protocol rather than depending on `GraphSession` directly. mypy caught the shortcut; `GraphSession` satisfies the Protocol structurally, so nothing at runtime changed.

**`answerCallbackQuery` is sent before any work starts.** Telegram re-delivers an unanswered callback, and a re-delivery reads as a second tap on a button that books calendar events. Easy to skip, expensive to skip.

**The edit flow is stateless.** Tapping Edit sends a force-reply prompt whose text contains the message ID; the user's reply arrives with `reply_to_message`, and the ID is parsed back out. No pending-edit table to expire, leak, or get out of sync with the checkpointer.

**Cards render in the recipient's zone, never UTC.** Everything downstream stores UTC because that is the only sane way to compare instants — but approving "11:00" when you meant 16:00 is precisely the failure this step exists to prevent.

**Email content is HTML-escaped.** Cards use `parse_mode: HTML`, and subjects are written by whoever sent the mail. A subject containing `<b>` would otherwise break rendering at best.

**`callback_data` is length-checked at construction.** Telegram's 64-byte cap is generous for `confirm:<gmail-id>` (~24 bytes), but a silent truncation would route a decision to the wrong proposal, so it raises instead.

**A stale proposal is reported, not resumed.** If the checkpoint is gone — already decided, or cleared — the bot says so rather than calling `resume` on nothing.

**No Telegram library.** The Bot API is four HTTP endpoints; a library would add a dependency and an async-runtime opinion to save about forty lines.

**Webhook secret is required, not optional.** Without the `X-Telegram-Bot-Api-Secret-Token` check, the webhook URL is the only thing between the internet and a bot with write access to a calendar. An unauthorised chat gets a 200, deliberately: a 4xx makes Telegram retry, and retrying a rejected chat is pointless.

**Added `--poll` as a development path.** The plan is right that production should use a webhook — one process, no extra worker — but a webhook needs a public URL, which does not exist until M07. `--poll` runs the identical handler against `getUpdates`. The two are mutually exclusive: Telegram will not deliver to `getUpdates` while a webhook is registered, so `--delete-webhook` first.

### Status

Code and tests complete — 386 tests, mypy strict clean, no network in the suite.

**Exit criterion not met**: it requires a real email to become a real calendar event via a real button tap, and that needs a bot token that does not exist yet. `TELEGRAM_BOT_TOKEN` is still blank.

Also still true from M05: the inbox contains no meeting email, so nothing has parked at `await_approval` outside the test suite.

### Blocked locally: Telegram is unreachable from this network

Bot created and token configured, but every call fails:

```
api.telegram.org                    TCP 443 open, TLS handshake dies after 30s
generativelanguage.googleapis.com   TLS fine, 404 in 0.4s
```

TCP connects and TLS never completes — the signature of SNI-based filtering, where a middlebox accepts the connection and drops the handshake once it reads the hostname. Telegram has been restricted in Pakistan since 2024. No timeout or retry setting changes this, and the code is not at fault.

**Deferred to M07 rather than redesigned.** The deployed instance runs in a datacentre outside that jurisdiction and reaches Telegram normally, so the constraint disappears on deploy. Rebuilding the approval channel as a web page would cost roughly another M06 for a problem that does not exist in production — and if a web approval page is ever wanted, it is additive rather than a replacement, since the graph, ledger, and interrupt mechanism are channel-agnostic.

A VPN makes the local path work today if the flow needs eyeballing before deploy.

### To finish (on the deployed instance, M07)

1. `PUBLIC_URL` and `TELEGRAM_WEBHOOK_SECRET` set, then `--set-webhook`.
2. Message the bot once, `--whoami`, chat ID into `ALLOWED_CHAT_IDS`.
3. Send yourself a meeting email ("lunch Thursday 2pm"). Note Gmail marks self-sent mail as read, and the poller queries `is:unread` — send from another address or mark it unread.
4. Poll, then tap a button.
5. Set `DRY_RUN=false` only once Confirm has been seen reporting "Dry run — nothing written".
