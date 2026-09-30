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
import math
import random
import re
import secrets
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
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


@dataclass(frozen=True, slots=True)
class _Verdict:
    flagged: MessageMeta | None
    """The thread's first qualifying ask, if it went unanswered for 48 hours."""

    answered_later: bool
    human: MessageMeta | None
    """The thread's first human message in the Primary tab, in the window --
    what the owner is shown when checking for asks the proxy missed."""


def _judge(
    messages: list[MessageMeta],
    window: MailWindow,
    owners: frozenset[str],
    excluded: frozenset[str],
) -> _Verdict:
    """One thread's verdict. `messages` are sorted, with drafts removed."""
    human = next(
        (
            m
            for m in messages
            if window.contains(m.internal_date)
            and "SENT" not in m.label_ids
            and PRIMARY in m.label_ids
            and not is_automated(m, excluded=excluded)
        ),
        None,
    )
    ask = next(
        (m for m in messages if window.contains(m.internal_date) and _is_ask(m, owners, excluded)),
        None,
    )
    if ask is None:
        return _Verdict(flagged=None, answered_later=False, human=human)

    replies = [
        m.internal_date
        for m in messages
        if "SENT" in m.label_ids and m.internal_date > ask.internal_date
    ]
    if any(reply <= ask.internal_date + JUDGE_AFTER for reply in replies):
        return _Verdict(flagged=None, answered_later=False, human=human)
    return _Verdict(flagged=ask, answered_later=bool(replies), human=human)


def _without_drafts(thread: list[MessageMeta]) -> list[MessageMeta]:
    return sorted((m for m in thread if "DRAFT" not in m.label_ids), key=lambda m: m.internal_date)


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
        messages = _without_drafts(thread)

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

        verdict = _judge(messages, window, owners, excluded)
        if verdict.flagged is not None:
            flagged += 1
            answered_later += verdict.answered_later

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


# --- the owner's labelling --------------------------------------------------

FLAGGED_SAMPLE = 40
UNFLAGGED_SAMPLE = 20

LOOSE_ENDS_GO_PER_WEEK = 5.0
"""The committed threshold (M15 spec, B4): go if the lower bound of the
corrected weekly rate reaches it."""


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion. Honest at small n and near 0
    or 1, where the textbook normal approximation claims a certainty it does
    not have."""
    if n == 0:
        return 0.0, 1.0
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


@dataclass(frozen=True, slots=True)
class Sample:
    flagged: list[MessageMeta]
    unflagged: list[MessageMeta]
    seed: int


def sample_for_labelling(
    threads: list[list[MessageMeta]],
    window: MailWindow,
    *,
    owners: frozenset[str],
    excluded: frozenset[str],
    seed: int,
) -> Sample:
    """Random threads to label, reproducible from `seed`: flagged ones measure
    precision, and unflagged human ones measure what the proxy misses."""
    owners = frozenset(normalise_address(owner) for owner in owners)
    flagged: list[MessageMeta] = []
    unflagged: list[MessageMeta] = []
    for thread in threads:
        verdict = _judge(_without_drafts(thread), window, owners, excluded)
        if verdict.flagged is not None:
            flagged.append(verdict.flagged)
        elif verdict.human is not None:
            unflagged.append(verdict.human)

    rng = random.Random(seed)
    return Sample(
        flagged=rng.sample(flagged, min(FLAGGED_SAMPLE, len(flagged))),
        unflagged=rng.sample(unflagged, min(UNFLAGGED_SAMPLE, len(unflagged))),
        seed=seed,
    )


def require_terminal() -> None:
    """Labelling shows senders and subjects. On the owner's own screen, that
    is the point. Piped into a file, or run by an agent whose output becomes a
    transcript on disk, it would be a leak."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise SystemExit(
            "--label is interactive and for the owner only: run it in your own terminal."
        )


def _yes(ask: Callable[[str], str], show: Callable[[str], None], prompt: str) -> bool:
    while True:
        answer = ask(prompt).strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        show("Please answer y or n.")


def _describe(message: MessageMeta, timezone: ZoneInfo) -> str:
    when = message.internal_date.astimezone(timezone)
    sender = message.headers.get("From", "?")
    return f"{when:%Y-%m-%d %H:%M}  {sender}  {message.headers.get('Subject', '')}"


def label(
    sample: Sample,
    timezone: ZoneInfo,
    *,
    ask: Callable[[str], str],
    show: Callable[[str], None],
) -> dict[str, Any]:
    """The owner's answers, reduced to counts. What was shown is not kept."""
    precise = 0
    for number, message in enumerate(sample.flagged, 1):
        show(f"\nFlagged {number}/{len(sample.flagged)}  {_describe(message, timezone)}")
        if _yes(ask, show, "Did it need a reply from you? [y/n] ") and _yes(
            ask, show, "Was it still unanswered, by any channel, 48 hours later? [y/n] "
        ):
            precise += 1

    missed = 0
    for number, message in enumerate(sample.unflagged, 1):
        show(f"\nNot flagged {number}/{len(sample.unflagged)}  {_describe(message, timezone)}")
        if _yes(ask, show, "Was this an ask you left unanswered for 48 hours? [y/n] "):
            missed += 1

    precision = wilson(precise, len(sample.flagged))
    misses = wilson(missed, len(sample.unflagged))
    return {
        "seed": sample.seed,
        "precision_yes": precise,
        "precision_of": len(sample.flagged),
        "precision_interval": [round(bound, 3) for bound in precision],
        "misses_yes": missed,
        "misses_of": len(sample.unflagged),
        "miss_interval": [round(bound, 3) for bound in misses],
    }


def decide(summary: dict[str, Any], labelling: dict[str, Any]) -> dict[str, Any]:
    """The committed loose-ends decision: the flagged weekly rate, times the
    precision's lower bound, must reach the threshold."""
    corrected = summary["flagged_per_week"] * labelling["precision_interval"][0]
    return {
        "threshold_per_week": LOOSE_ENDS_GO_PER_WEEK,
        "corrected_per_week_lower_bound": round(corrected, 2),
        "loose_ends_go": corrected >= LOOSE_ENDS_GO_PER_WEEK,
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
    if "labelling" in summary:
        labelled = summary["labelling"]
        decision = summary["decision"]
        verdict = "go" if decision["loose_ends_go"] else "no-go"
        lines += [
            "",
            f"Labelled by the owner (seed {labelled['seed']}):",
            "",
            f"- Precision: {labelled['precision_yes']} of {labelled['precision_of']} flagged "
            f"threads were unanswered asks; 95% interval {labelled['precision_interval']}.",
            f"- Misses: {labelled['misses_yes']} of {labelled['misses_of']} unflagged human "
            f"threads were unanswered asks; 95% interval {labelled['miss_interval']}.",
            f"- Corrected weekly rate, lower bound: {decision['corrected_per_week_lower_bound']} "
            f"against a threshold of {decision['threshold_per_week']}: **{verdict}**.",
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
    if args.label:
        require_terminal()
        seed = args.seed if args.seed is not None else secrets.randbelow(1_000_000)
        sample = sample_for_labelling(threads, window, owners=owners, excluded=excluded, seed=seed)
        labelling = label(sample, window.timezone, ask=input, show=print)
        summary["labelling"] = labelling
        summary["decision"] = decide(summary, labelling)
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
    mail.add_argument(
        "--label",
        action="store_true",
        help="Owner only, in your own terminal: label a random sample to measure the proxy.",
    )
    mail.add_argument("--seed", type=int, help="Reproduce an earlier labelling sample.")

    args = parser.parse_args(argv)
    if args.command == "mail":
        _run_mail(args)


if __name__ == "__main__":
    main()
