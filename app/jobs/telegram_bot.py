"""Telegram plumbing: webhook management, and a development poller.

    python -m app.jobs.telegram_bot --whoami        # find your chat ID
    python -m app.jobs.telegram_bot --set-webhook   # production (needs PUBLIC_URL)
    python -m app.jobs.telegram_bot --delete-webhook
    python -m app.jobs.telegram_bot --poll          # local, no public URL needed

`--poll` and the webhook are mutually exclusive: Telegram will not deliver
updates to `getUpdates` while a webhook is registered. Delete the webhook first
when dropping back to local development.
"""

from __future__ import annotations

import argparse
import time

from app.config import Settings, get_settings
from app.store.db import connect_autocommit
from app.telegram.client import TelegramClient
from app.telegram.handler import NotAllowedError, TelegramHandler


def _bot(settings: Settings) -> TelegramClient:
    if settings.telegram_bot_token is None:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather first.")
    return TelegramClient(settings.telegram_bot_token.get_secret_value())


def whoami(bot: TelegramClient) -> None:
    """Print the chat IDs that have messaged the bot.

    Chicken-and-egg otherwise: the allowlist needs a chat ID, and the ID only
    exists once someone has messaged the bot.
    """
    updates = bot.get_updates()
    if not updates:
        print("No updates. Send your bot any message, then run this again.")
        print("(If a webhook is registered, delete it first -- it swallows getUpdates.)")
        return

    seen: dict[int, str] = {}
    for update in updates:
        source = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
        chat = source.get("chat") or {}
        if chat.get("id") is not None:
            seen[int(chat["id"])] = str(chat.get("username") or chat.get("first_name") or "")

    for chat_id, name in seen.items():
        print(f"  chat_id={chat_id}  {name}")
    print(f"\nPut these in .env:  ALLOWED_CHAT_IDS=[{', '.join(str(c) for c in seen)}]")


def poll(settings: Settings, bot: TelegramClient, seconds: int) -> None:
    """Development loop. Production uses the webhook."""
    if not settings.allowed_chat_ids:
        print("ALLOWED_CHAT_IDS is empty -- every update will be rejected. Run --whoami first.")

    deadline = time.monotonic() + seconds
    offset: int | None = None
    print(f"Polling for {seconds}s. Ctrl-C to stop.")

    while time.monotonic() < deadline:
        updates = bot.get_updates(offset=offset, timeout=10)
        if not updates:
            continue

        offset = max(int(u["update_id"]) for u in updates) + 1

        # Decisions only: a running app's worker applies them.
        with connect_autocommit(settings.database_url) as conn:
            handler = TelegramHandler(
                conn=conn,
                bot=bot,
                allowed_chat_ids=frozenset(settings.allowed_chat_ids),
            )
            for update in updates:
                try:
                    print(f"  {handler.handle(update)}")
                except NotAllowedError as exc:
                    print(f"  rejected: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram webhook and development poller.")
    parser.add_argument("--whoami", action="store_true")
    parser.add_argument("--set-webhook", action="store_true")
    parser.add_argument("--delete-webhook", action="store_true")
    parser.add_argument("--poll", action="store_true")
    parser.add_argument("--seconds", type=int, default=120, help="How long --poll runs.")
    args = parser.parse_args()

    settings = get_settings()
    bot = _bot(settings)

    if args.whoami:
        whoami(bot)
    elif args.delete_webhook:
        bot.delete_webhook()
        print("Webhook deleted. getUpdates polling will now work.")
    elif args.set_webhook:
        if settings.public_url is None or settings.telegram_webhook_secret is None:
            raise SystemExit("PUBLIC_URL and TELEGRAM_WEBHOOK_SECRET must both be set.")
        url = f"{settings.public_url.rstrip('/')}/telegram/webhook"
        bot.set_webhook(url, settings.telegram_webhook_secret.get_secret_value())
        print(f"Webhook set to {url}")
    elif args.poll:
        poll(settings, bot, args.seconds)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
