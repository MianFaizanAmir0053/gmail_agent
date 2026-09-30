"""M15's measurement: mail volume and loose ends, from metadata only.

    python -m app.jobs.measure mail --since 2026-09-14 --until 2026-09-28 --timezone Asia/Karachi

Reads labels, `internalDate` and a handful of headers (`METADATA_HEADERS`),
never a body, and makes no model calls. It writes counts only: no address,
subject or id leaves this module.

**A loose end** is a *flagged thread*: one whose first qualifying ask got no
reply from the owner within 48 hours. A qualifying ask is an inbound message,
in the Primary tab, not sent by a machine, and addressed to the owner. It is a
proxy, and its biases are printed with its numbers.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.google.gmail import MessageMeta

JUDGE_AFTER = timedelta(hours=48)
"""How long the owner has to reply before an ask counts as a loose end."""

SNAPSHOT_LOOKBACK = timedelta(days=30)

PRIMARY = "CATEGORY_PERSONAL"

THRESHOLDS_COMMIT = "52ddc5e"
"""Where the go/no-go thresholds were committed, before any measurement ran."""

_NO_REPLY = re.compile(
    r"^(no-?reply|do-?not-?reply|notifications?|mailer-daemon|bounces?)([+._-].*)?$"
)
_CALENDAR_SENDERS = frozenset({"calendar-notification@google.com"})
_CALENDAR_SUBJECT = re.compile(
    r"^(updated invitation|invitation|accepted|declined|tentatively accepted|cancell?ed event)"
    r"(\s+with note)?:",
    re.IGNORECASE,
)

BIASES = (
    "Overcounts: FYIs that need no reply; replies sent outside Gmail; matters settled by "
    "phone or chat.",
    "Undercounts: aliases missing from OWNER_ALIASES; asks sitting in other tabs.",
    "One ask per thread: a later unanswered ask in a thread whose first ask was answered "
    "is not counted.",
)


class WindowError(ValueError):
    """A measurement window that cannot be judged."""


@dataclass(frozen=True, slots=True)
class MailWindow:
    since: datetime
    until: datetime
    timezone: ZoneInfo

    @classmethod
    def checked(
        cls, *, since: datetime, until: datetime, timezone: ZoneInfo, now: datetime
    ) -> MailWindow:
        if until <= since:
            raise WindowError("The window ends before it starts.")
        if until > now - JUDGE_AFTER:
            raise WindowError(
                "The window must end at least 48 hours ago: asks from its last two days "
                "cannot be judged yet."
            )
        return cls(since=since, until=until, timezone=timezone)

    @property
    def days(self) -> float:
        return (self.until - self.since).total_seconds() / 86400

    def contains(self, at: datetime) -> bool:
        return self.since <= at < self.until


def normalise_address(address: str) -> str:
    """One spelling per mailbox: lower case, no `+tag`, and for Gmail no dots
    and no `googlemail.com`, since Gmail ignores all three."""
    local, _, domain = address.strip().lower().partition("@")
    local = local.split("+", 1)[0]
    if domain in ("gmail.com", "googlemail.com"):
        return f"{local.replace('.', '')}@gmail.com"
    return f"{local}@{domain}"


def _addresses(header: str) -> list[str]:
    return [normalise_address(address) for _, address in getaddresses([header]) if address]


def is_automated(message: MessageMeta, *, excluded: frozenset[str]) -> bool:
    """Sent by a machine, or by a sender the owner asked to leave out."""
    headers = message.headers
    if headers.get("List-Unsubscribe"):
        return True
    auto_submitted = headers.get("Auto-Submitted", "").strip().lower()
    if auto_submitted and auto_submitted != "no":  # "no" is the header's way of saying human
        return True
    if headers.get("Precedence", "").strip().lower() in {"bulk", "list", "junk"}:
        return True

    senders = _addresses(headers.get("From", ""))
    sender = senders[0] if senders else ""
    if sender in excluded or sender in _CALENDAR_SENDERS:
        return True
    if _NO_REPLY.match(sender.partition("@")[0]):
        return True
    return bool(_CALENDAR_SUBJECT.match(headers.get("Subject", "").strip()))


def _is_ask(message: MessageMeta, owners: frozenset[str], excluded: frozenset[str]) -> bool:
    if "SENT" in message.label_ids or PRIMARY not in message.label_ids:
        return False
    if is_automated(message, excluded=excluded):
        return False
    recipients = _addresses(message.headers.get("To", "")) + _addresses(
        message.headers.get("Cc", "")
    )
    return any(address in owners for address in recipients)


def _age_bucket(age: timedelta) -> str:
    if age < timedelta(days=7):
        return "2-7 days"
    if age < timedelta(days=14):
        return "7-14 days"
    return "14-30 days"


def summarise_mail(
    threads: list[list[MessageMeta]],
    window: MailWindow,
    *,
    owners: frozenset[str],
    excluded: frozenset[str],
    now: datetime,
) -> dict[str, Any]:
    """Counts only. Every value returned is a number or a fixed phrase."""
    owners = frozenset(normalise_address(owner) for owner in owners)
    inbound_per_day: Counter[str] = Counter()
    sent_per_day: Counter[str] = Counter()
    split: Counter[str] = Counter()
    snapshot: Counter[str] = Counter()
    flagged = answered_later = 0

    for thread in threads:
        # Drafts are not replies, and not mail anyone received.
        messages = sorted(
            (m for m in thread if "DRAFT" not in m.label_ids), key=lambda m: m.internal_date
        )

        for message in messages:
            if not window.contains(message.internal_date):
                continue
            day = message.internal_date.astimezone(window.timezone).date().isoformat()
            if "SENT" in message.label_ids:
                sent_per_day[day] += 1
                continue
            inbound_per_day[day] += 1
            categories = {label for label in message.label_ids if label.startswith("CATEGORY_")}
            if not categories:
                split["no_category"] += 1
            elif PRIMARY in categories:
                split["primary"] += 1
            else:
                split["other_category"] += 1
            if is_automated(message, excluded=excluded):
                split["automated"] += 1

        ask = next(
            (
                m
                for m in messages
                if window.contains(m.internal_date) and _is_ask(m, owners, excluded)
            ),
            None,
        )
        if ask is not None:
            replies = [
                m.internal_date
                for m in messages
                if "SENT" in m.label_ids and m.internal_date > ask.internal_date
            ]
            if not any(reply <= ask.internal_date + JUDGE_AFTER for reply in replies):
                flagged += 1
                answered_later += bool(replies)

        before_end = [m for m in messages if m.internal_date < window.until]
        if before_end:
            last = before_end[-1]
            age = window.until - last.internal_date
            if JUDGE_AFTER <= age <= SNAPSHOT_LOOKBACK and _is_ask(last, owners, excluded):
                snapshot[_age_bucket(age)] += 1

    return {
        "window": {
            "since": window.since.isoformat(),
            "until": window.until.isoformat(),
            "timezone": str(window.timezone),
            "days": round(window.days, 2),
            "measured_at": now.isoformat(),
        },
        "inbound_total": sum(inbound_per_day.values()),
        "inbound_primary": split["primary"],
        "inbound_other_category": split["other_category"],
        "inbound_no_category": split["no_category"],
        "inbound_automated": split["automated"],
        "sent_total": sum(sent_per_day.values()),
        "inbound_per_day": dict(sorted(inbound_per_day.items())),
        "sent_per_day": dict(sorted(sent_per_day.items())),
        "flagged_threads": flagged,
        "flagged_per_week": round(flagged * 7 / window.days, 2),
        "flagged_answered_later": answered_later,
        "snapshot_open_at_window_end": {
            "lookback_days": SNAPSHOT_LOOKBACK.days,
            **{bucket: snapshot[bucket] for bucket in ("2-7 days", "7-14 days", "14-30 days")},
        },
        "biases": list(BIASES),
    }


def to_markdown(summary: dict[str, Any]) -> str:
    window = summary["window"]
    snapshot = summary["snapshot_open_at_window_end"]
    lines = [
        f"# Mail volume and loose ends, {window['since'][:10]} to {window['until'][:10]}",
        "",
        f"Window: {window['days']} days, bucketed in {window['timezone']}. "
        f"Measured {window['measured_at'][:16]} UTC. Counts only.",
        "",
        "| Measure | Count |",
        "|---|---|",
        f"| Inbound messages | {summary['inbound_total']} |",
        f"| ...in the Primary tab | {summary['inbound_primary']} |",
        f"| ...in other tabs | {summary['inbound_other_category']} |",
        f"| ...with no tab label | {summary['inbound_no_category']} |",
        f"| ...sent by machines | {summary['inbound_automated']} |",
        f"| Sent by the owner | {summary['sent_total']} |",
        f"| **Flagged threads** (no reply within 48 h) | **{summary['flagged_threads']}** |",
        f"| Flagged per week | {summary['flagged_per_week']} |",
        f"| Flagged, answered later | {summary['flagged_answered_later']} |",
        "",
        f"Open at the window's end (lookback {snapshot['lookback_days']} days, "
        "informational only): "
        f"2-7 days {snapshot['2-7 days']}, 7-14 days {snapshot['7-14 days']}, "
        f"14-30 days {snapshot['14-30 days']}.",
        "",
        "Known biases:",
        "",
        *[f"- {bias}" for bias in summary["biases"]],
    ]
    if "run" in summary:
        run = summary["run"]
        lines += [
            "",
            f"Run: commit {run['commit']}; thresholds committed in {run['thresholds_commit']}; "
            f"{run['owner_aliases']} alias(es), {run['excluded_senders']} excluded sender(s).",
        ]
    return "\n".join(lines) + "\n"


def _utc_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)


def _git_head() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _run_mail(args: argparse.Namespace) -> None:
    from app.config import get_settings
    from app.google.auth import build_service, load_credentials
    from app.google.gmail import GmailClient

    settings = get_settings()
    if args.timezone is None and settings.user_timezone == "UTC":
        raise SystemExit(
            "Pass --timezone (or set USER_TIMEZONE): days are counted in the owner's zone, "
            "and UTC here is only the default."
        )
    now = datetime.now(UTC)
    try:
        window = MailWindow.checked(
            since=args.since,
            until=args.until,
            timezone=ZoneInfo(args.timezone or settings.user_timezone),
            now=now,
        )
    except WindowError as exc:
        raise SystemExit(str(exc)) from exc

    gmail = GmailClient(build_service("gmail", "v1", load_credentials(settings)))
    owners = frozenset({gmail.profile_address(), *settings.owner_aliases})
    excluded = frozenset(normalise_address(sender) for sender in settings.measure_exclude_senders)

    fetch_from = min(window.since, window.until - SNAPSHOT_LOOKBACK)
    query = f"after:{int(fetch_from.timestamp())} before:{int(window.until.timestamp())}"
    threads = [gmail.thread_metadata(thread_id) for thread_id in gmail.thread_ids(query)]

    summary = summarise_mail(threads, window, owners=owners, excluded=excluded, now=now)
    summary["run"] = {
        "commit": _git_head(),
        "thresholds_commit": THRESHOLDS_COMMIT,
        "owner_aliases": len(settings.owner_aliases),
        "excluded_senders": len(excluded),
    }

    out = Path(args.out_dir) / f"volume-mail-{window.until:%Y-%m-%d}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    markdown = to_markdown(summary)
    out.with_suffix(".md").write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"Written: {out}.json, {out}.md -- counts only; review before committing.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="M15 measurement: counts only.")
    commands = parser.add_subparsers(dest="command", required=True)

    mail = commands.add_parser("mail", help="Mail volume and loose ends, from metadata only.")
    mail.add_argument("--since", required=True, type=_utc_date, help="YYYY-MM-DD, UTC")
    mail.add_argument("--until", required=True, type=_utc_date, help="YYYY-MM-DD, UTC")
    mail.add_argument("--timezone", help="IANA zone for day buckets; defaults to USER_TIMEZONE")
    mail.add_argument("--out-dir", default="results")

    args = parser.parse_args(argv)
    if args.command == "mail":
        _run_mail(args)


if __name__ == "__main__":
    main()
