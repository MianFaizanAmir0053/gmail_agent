"""Recall, of the sync and of the feed (M20, D5).

A daily job looks at the window from 26 hours ago to 2 hours ago, which keeps
it clear of the sync's own timing:

- **Sync recall.** The ids Gmail lists for the window, using D1's set, are
  checked against `gmail_messages`. Missing ones are fetched and stored, so a
  miss is repaired as well as reported; repaired inbound mail is fed if the
  rule takes it.
- **Feed recall.** A stored row that has met the feed's rule for over an
  hour, with no ledger row, means the feed stalled. Hours when M17 had paused
  the agent, or its spending cap had stopped work, are left out: the audit
  log records both. A `SKIPPED` "too old" record for mail under a day old
  means the age rule is wrong.
- **Category agreement, both ways.** Ids Gmail lists as not in the four other
  tabs must be `primary` here; ids it lists in Updates or Forums must not be.

Each result goes to `job_runs`, and a shortfall sends one alert a day.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import psycopg

from app.channel.channels import Channels, TelegramChannel, configured_channels
from app.channel.webpush import WebPushChannel
from app.config import Settings
from app.google.auth import build_service, load_credentials
from app.google.gmail import GmailClient, MessageGoneError, is_outage
from app.mail import feed
from app.mail.messages import D1_QUERY, PRIMARY_QUERY, classify, owner_addresses, store, stored_ids
from app.mail.sync import owners_of

log = logging.getLogger(__name__)

WINDOW_FROM = timedelta(hours=26)
WINDOW_TO = timedelta(hours=2)
"""The window, back from the check: a day, starting two hours ago so the
sync's own lag never reads as a miss."""

STALLED_AFTER = timedelta(hours=1)
"""How long a row may meet the feed's rule with no ledger row. Poll runs every
ten minutes, so an hour is six chances."""

IN_UPDATES = "category:updates -in:chats -in:drafts"
IN_FORUMS = "category:forums -in:chats -in:drafts"

AlertCode = Literal["mail_sync_missed", "mail_feed_stalled"]

ALERTS: dict[AlertCode, str] = {
    "mail_sync_missed": "Mail sync missed messages",
    "mail_feed_stalled": "The mail feed has stalled",
}
"""Generic words only: an alert crosses Apple's and Google's servers and lands
on a lock screen."""

_CAP_ENDS = ("budget_ok", "budget_warning")
"""Audit kinds that say spending has started again after a cap."""

_HELD_KINDS = ("paused", "resumed", "budget_exhausted", *_CAP_ENDS)


@dataclass(frozen=True, slots=True)
class RecallResult:
    since: datetime
    until: datetime
    listed: int
    """Ids Gmail lists in the window, in D1's set."""

    missed: int
    """Of those, how many the sync had not stored."""

    repaired: int
    """Of those, how many were fetched and stored now."""

    eligible: int
    """Stored rows in the window that meet the feed's rule today."""

    stalled: int
    """Rows that met the rule for over an hour with no ledger row."""

    held: int
    """Rows left out because M17 had paused the agent or capped its spend."""

    too_old_young: int
    """`SKIPPED` "too old" records for mail under a day old."""

    categories_checked: int
    categories_mismatched: int

    @property
    def sync_ok(self) -> bool:
        return self.missed == 0

    @property
    def feed_ok(self) -> bool:
        return self.stalled == 0 and self.too_old_young == 0

    @property
    def categories_ok(self) -> bool:
        return self.categories_mismatched == 0

    def alerts(self) -> list[AlertCode]:
        """At most one of each. A category that disagrees means D1's mapping
        is wrong, which costs the feed mail the way a miss does."""
        codes: list[AlertCode] = []
        if not (self.sync_ok and self.categories_ok):
            codes.append("mail_sync_missed")
        if not self.feed_ok:
            codes.append("mail_feed_stalled")
        return codes

    def summary(self) -> dict[str, Any]:
        """For `/health`: counts and times only."""
        values = asdict(self)
        values["since"] = self.since.isoformat()
        values["until"] = self.until.isoformat()
        return values


def recall(
    conn: psycopg.Connection,
    gmail: GmailClient,
    *,
    account: str,
    owners: Iterable[str] = (),
    now: datetime | None = None,
) -> RecallResult:
    """The day's three checks. An outage while fetching raises: the checks
    are only worth recording whole."""
    now = now or datetime.now(UTC)
    since, until = now - WINDOW_FROM, now - WINDOW_TO
    everyone = owner_addresses(account, *owners)

    listed = gmail.message_ids(D1_QUERY, after=since, before=until)
    have = stored_ids(conn, account, listed)
    missed = repaired = 0
    for message_id in listed:
        if message_id in have:
            continue
        try:
            row = classify(gmail.message_metadata(message_id), everyone)
        except MessageGoneError:
            continue  # deleted since it was listed: nothing to repair
        except Exception as exc:
            if is_outage(exc):
                raise
            log.warning("recall: %s could not be fetched (%s)", message_id, type(exc).__name__)
            missed += 1
            continue
        outcome = store(conn, account, row, "recall")
        if outcome != "not_kept":
            missed += 1
            repaired += outcome == "inserted"

    eligible, stalled, held = _feed_recall(conn, account, since, until, now)
    too_old_young = _too_old_young(conn, since)
    checked, mismatched = _category_agreement(conn, gmail, account, since, until)
    result = RecallResult(
        since=since,
        until=until,
        listed=len(listed),
        missed=missed,
        repaired=repaired,
        eligible=eligible,
        stalled=stalled,
        held=held,
        too_old_young=too_old_young,
        categories_checked=checked,
        categories_mismatched=mismatched,
    )
    log.info("recall: %s", result.summary())
    return result


def _feed_recall(
    conn: psycopg.Connection, account: str, since: datetime, until: datetime, now: datetime
) -> tuple[int, int, int]:
    """(eligible, stalled, held). A row's clock starts when it began to meet
    the rule (`offered_since`): one moved out of spam a moment ago has met it
    only since, but reading a stalled message restarts nothing. A row with no
    clock, written before there was one, counts from when it was first seen:
    a false alarm rather than a silent miss."""
    rows = conn.execute(
        f"""
        SELECT coalesce(m.offered_since, m.first_seen_at)
          FROM gmail_messages m
          JOIN gmail_cursors c ON c.account = m.account
         WHERE m.account = %(account)s
           AND m.internal_at >= %(since)s AND m.internal_at < %(until)s
           AND {feed.RULE}
        """,
        {"account": account, "since": since, "until": until, "margin": feed.MARGIN},
    ).fetchall()
    held_hours = held_intervals(conn, since, now)
    stalled = held = 0
    for (met_since,) in rows:
        waited = now - met_since
        if waited <= STALLED_AFTER:
            continue
        # The hold is taken off the wait: a short pause cannot excuse a day.
        if waited - held_for(held_hours, met_since, now) > STALLED_AFTER:
            stalled += 1
        else:
            held += 1
    return len(rows), stalled, held


def held_for(
    intervals: list[tuple[datetime, datetime]], start: datetime, end: datetime
) -> timedelta:
    """How much of `start` to `end` the agent was held, a pause and a cap
    that overlap counted once."""
    total = timedelta(0)
    reached = start
    for begin, finish in sorted(intervals):
        begin, finish = max(begin, reached), min(finish, end)
        if finish > begin:
            total += finish - begin
            reached = finish
    return total


def held_intervals(
    conn: psycopg.Connection, since: datetime, now: datetime
) -> list[tuple[datetime, datetime]]:
    """When M17 had paused the agent, or its spending cap had stopped work.

    Read from the audit log, if M17's migration has made one. A pause runs
    until the next resume. A cap runs until spending starts again: the next
    `budget_ok` or warning (a raised cap), or the end of its UTC month.
    """
    exists = conn.execute("SELECT to_regclass('audit_log') IS NOT NULL").fetchone()
    if not (exists and exists[0]):
        return []
    # A month back covers any cap still running at `since`; the last pause or
    # resume before that says whether the agent was paused all along.
    rows = conn.execute(
        """
        (SELECT kind, at FROM audit_log
          WHERE kind IN ('paused', 'resumed') AND at < %(from)s
          ORDER BY at DESC LIMIT 1)
        UNION ALL
        (SELECT kind, at FROM audit_log
          WHERE kind = ANY(%(kinds)s) AND at >= %(from)s)
        ORDER BY at
        """,
        {"from": since - timedelta(days=32), "kinds": list(_HELD_KINDS)},
    ).fetchall()
    intervals: list[tuple[datetime, datetime]] = []
    paused_at: datetime | None = None
    for index, (kind, at) in enumerate(rows):
        if kind == "paused" and paused_at is None:
            paused_at = at
        elif kind == "resumed" and paused_at is not None:
            intervals.append((paused_at, at))
            paused_at = None
        elif kind == "budget_exhausted":
            later = [t for k, t in rows[index + 1 :] if k in _CAP_ENDS]
            intervals.append((at, min([_next_month(at), now, *later])))
    if paused_at is not None:
        intervals.append((paused_at, now))
    return intervals


def _next_month(at: datetime) -> datetime:
    at = at.astimezone(UTC)
    if at.month == 12:
        return datetime(at.year + 1, 1, 1, tzinfo=UTC)
    return datetime(at.year, at.month + 1, 1, tzinfo=UTC)


def _too_old_young(conn: psycopg.Connection, since: datetime) -> int:
    """Too-old records made in the window for mail under a day old then."""
    row = conn.execute(
        """
        SELECT count(*)
          FROM processed_messages p
          JOIN gmail_messages m ON m.message_id = p.gmail_message_id
         WHERE p.status = 'skipped' AND p.error = %s
           AND p.created_at >= %s
           AND p.created_at - m.internal_at < interval '1 day'
        """,
        (feed.TOO_OLD, since),
    ).fetchone()
    return int(row[0]) if row else 0


def _category_agreement(
    conn: psycopg.Connection, gmail: GmailClient, account: str, since: datetime, until: datetime
) -> tuple[int, int]:
    """(checked, mismatched), over rows stored here: what is missing is the
    sync recall's to report."""
    primary = gmail.message_ids(PRIMARY_QUERY, after=since, before=until)
    other = gmail.message_ids(IN_UPDATES, after=since, before=until) + gmail.message_ids(
        IN_FORUMS, after=since, before=until
    )
    rows = conn.execute(
        """
        SELECT message_id, category FROM gmail_messages
         WHERE account = %s AND message_id = ANY(%s)
        """,
        (account, primary + other),
    ).fetchall()
    category: dict[str, str] = {row[0]: row[1] for row in rows}
    mismatched = len([m for m in primary if m in category and category[m] != "primary"])
    mismatched += len([m for m in other if category.get(m) == "primary"])
    checked = len([m for m in (*primary, *other) if m in category])
    return checked, mismatched


def run_daily(settings: Settings) -> RecallResult | None:
    """The scheduler's daily check. None before the first sync run: there is
    nothing to check yet. Alerts any shortfall, once a day."""
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        if not feed.active(conn):
            return None
        gmail = GmailClient(build_service("gmail", "v1", load_credentials(settings)))
        result = recall(conn, gmail, account=gmail.profile().address, owners=owners_of(settings))
        codes = result.alerts()
        if codes:
            channels = configured_channels(settings)
            send_alerts(
                conn,
                codes,
                names=channels.names,
                deliver=deliver_through(channels),
                day=result.until.date().isoformat(),
            )
    return result


# --- alerts ---------------------------------------------------------------------

Deliver = Callable[[str, frozenset[str]], set[str]]
"""Send a fixed phrase through every channel not in the set given. Returns the
names of the channels that delivered it."""


def send_alerts(
    conn: psycopg.Connection,
    codes: list[AlertCode],
    *,
    names: frozenset[str],
    deliver: Deliver,
    day: str,
) -> list[AlertCode]:
    """Send each alert at most once a day per channel.

    Recorded in `alerts_sent`, as M16's token alerts are: a row is written
    only once a channel delivered, so one that did not is asked again by the
    next check that finds a shortfall the same day.
    """
    sent: list[AlertCode] = []
    for code in codes:
        rows = conn.execute(
            "SELECT channel FROM alerts_sent WHERE code = %s AND subject = %s", (code, day)
        ).fetchall()
        already = frozenset(row[0] for row in rows)
        if not names - already:
            continue
        delivered = deliver(ALERTS[code], already)
        for name in sorted(delivered):
            conn.execute(
                """
                INSERT INTO alerts_sent (code, subject, channel) VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (code, day, name),
            )
        if delivered:
            sent.append(code)
    return sent


def deliver_through(channels: Channels) -> Deliver:
    """Deliver a mail alert through the configured channels.

    M16's channels know only their own alert codes, and M17 is reworking
    them; until the two merge, this speaks to each kind of channel directly,
    with the same generic words on the lock screen. A channel of any other
    kind is not told.
    """

    def deliver(text: str, skip: frozenset[str]) -> set[str]:
        delivered: set[str] = set()
        for channel in channels.channels:
            if channel.name in skip:
                continue
            try:
                if isinstance(channel, TelegramChannel):
                    channel.bot.send_message(channel.chat_id, f"⚠️ {text}.")
                    delivered.add(channel.name)
                elif isinstance(channel, WebPushChannel):
                    push = {"title": "mailagent", "body": f"{text}.", "url": "/", "tag": "alert"}
                    if channel._push(push) > 0:
                        delivered.add(channel.name)
            except Exception:
                log.exception("mail alert via %s failed", channel.name)
        return delivered

    return deliver
