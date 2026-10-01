"""Mail sync (M20): every relevant message in the mailbox, as metadata.

- `quota`: the pacer every Gmail call in the process goes through;
- `messages`: what a message is classified as, and how it is stored;
- `sync`: the cursor, the incremental passes, the catch-up, the queue and the backfill;
- `feed`: the meeting pipeline's candidates;
- `recall`: the daily checks of the sync and of the feed.
"""
