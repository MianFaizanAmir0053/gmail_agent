"""Gmail reads.

Body extraction is deliberately a pure function (`extract_body`) so it can be
tested against recorded payloads without touching the network -- it is the part
most likely to be wrong, since real messages nest parts arbitrarily.

Every call goes through `GmailClient._execute` (M20): the process's pacer
counts what each attempt costs, and a rate limit or a server error is
retried there. One call -- the pacer's wait, every attempt and the pauses
between them -- takes at most `RETRY_FOR` seconds.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import random
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import getaddresses
from types import MappingProxyType
from typing import Any, Literal, Protocol, cast

import httplib2  # type: ignore[import-untyped]
from google.auth.exceptions import RefreshError, TransportError
from googleapiclient.errors import HttpError

from app.contracts import EmailMessage
from app.mail import quota
from app.policy.scrub import normalise, prepare

METADATA_HEADERS = (
    "From",
    "To",
    "Cc",
    "Subject",
    "List-Unsubscribe",
    "Auto-Submitted",
    "Precedence",
)
"""The only headers M15's measurement asks Gmail for.

Enough to tell who wrote a message, to whom, and whether a machine sent it.
Subject is used to recognise calendar notifications and for the owner's own
on-screen labelling. It is never written anywhere.
"""

SYNC_HEADERS = (
    "From",
    "To",
    "Cc",
    "List-Unsubscribe",
    "Auto-Submitted",
    "Precedence",
)
"""The only headers the mail sync asks for (M20, D1): who, to whom, and the
bulk signals. Unlike `METADATA_HEADERS` there is no Subject -- a subject can
carry a one-time code, and nothing stores one until M18 can strip it."""

GUEST_HEADERS = ("From", "To", "Cc", "Authentication-Results")
"""What the recipient rule reads from a thread (M17, D4): who wrote, to whom,
and Gmail's own verdict on the sender's domain."""

GUEST_FIELDS = "messages(id,labelIds,payload/headers)"
"""The field mask on the recipient rule's thread read: labels and headers only,
so no snippet is ever received."""

SYNC_FIELDS = "id,threadId,labelIds,internalDate,payload/headers"
"""The field mask on every sync fetch. Without it Gmail also sends the
snippet, which can quote a code; with it, a snippet is never even received."""

HISTORY_TYPES = ("messageAdded", "messageDeleted", "labelAdded", "labelRemoved")

HISTORY_PAGE_SIZE = 500
"""Gmail's largest page of history records."""

RETRY_FOR = 30.0
"""Seconds one call may take in all (M20, D3): the pacer's wait, every
attempt and the pauses between them. The pipeline's fetch runs inside a
message's run, which must stay well inside the platform's kill timeout (120 s)."""

SHORTEST_ATTEMPT = 5.0
"""Seconds an attempt is always given. A retry is not started with less time
left; a first attempt gets this much even when the pacer's wait left less,
so a call ends within `RETRY_FOR` plus this, at the very worst."""

_FIRST_RETRY_AFTER = 1.0
_RATE_LIMIT_REASONS = frozenset(
    {"rateLimitExceeded", "userRateLimitExceeded", "RATE_LIMIT_EXCEEDED", "RESOURCE_EXHAUSTED"}
)
"""A 403 that means "slow down": the older `errors` reasons, and the newer
`details` reason and status Google's front ends send beside or instead of them."""


class MessageGoneError(LookupError):
    """A fetch answered 404: the message has left the mailbox. The pipeline's
    fetch raises it too for a message now in the trash or spam (`BINNED`).

    A LookupError, so LangGraph's default retry rule never retries it either:
    a deleted message stays deleted, however often it is asked for.
    """


BINNED = frozenset({"TRASH", "SPAM"})
"""Labels that take a message out of the pipeline's hands."""


class CursorExpiredError(Exception):
    """`history.list` answered 404: Gmail no longer keeps the cursor's history.
    The only thing that starts a catch-up (M20, D3)."""


class Pacing(Protocol):
    """What the client needs of a pacer (`app.mail.quota.Pacer`)."""

    def spend(
        self, method: str, *, share: str | None = None, wait_for: float | None = None
    ) -> None: ...


def is_transient(exc: BaseException) -> bool:
    """Gmail or the network failing for everyone, for now: worth retrying."""
    if isinstance(exc, HttpError):
        status = int(exc.status_code)
        if status == 429 or status >= 500:
            return True
        return status == 403 and bool(_reasons(exc) & _RATE_LIMIT_REASONS)
    return isinstance(exc, OSError | httplib2.HttpLib2Error | TransportError)


def is_outage(exc: BaseException) -> bool:
    """A failure that says nothing about any one message (M20, D3).

    Gmail or the network unavailable, or the token refused: every message
    would fail the same way, so the pass stops and nobody is charged a
    strike. Anything else -- a 400 for one id, a malformed response -- is that
    message's own failure.
    """
    if isinstance(exc, RefreshError):
        return True
    if isinstance(exc, HttpError) and int(exc.status_code) == 401:
        return True
    return is_transient(exc)


def _reasons(exc: Any) -> set[str]:
    """Every reason an error gives, read from its body.

    Not from the client library's `error_details` alone: when Gmail sends both
    `error.errors` and `error.details`, it keeps only `details`, so a rate
    limit named in the other list went unseen and was taken for a refusal.
    """
    found = set(_reasons_in(getattr(exc, "error_details", None)))
    error = _error_body(exc)
    if error is not None:
        found.update(_reasons_in(error.get("errors")))
        found.update(_reasons_in(error.get("details")))
        if isinstance(error.get("status"), str):
            found.add(error["status"])
    return found


def _reasons_in(items: Any) -> set[str]:
    if not isinstance(items, list):
        return set()
    return {str(item["reason"]) for item in items if isinstance(item, dict) and "reason" in item}


def _error_body(exc: Any) -> dict[str, Any] | None:
    """The `error` object of a failed call's JSON body, or None."""
    try:
        data = json.loads(getattr(exc, "content", b"") or b"")
    except (TypeError, ValueError):  # not JSON: a proxy's page, say
        return None
    error = data.get("error") if isinstance(data, dict) else None
    return error if isinstance(error, dict) else None


def _status(exc: BaseException) -> int | None:
    return int(exc.status_code) if isinstance(exc, HttpError) else None


def _cap_timeout(request: Any, seconds: float) -> None:
    """Give an attempt's sockets at most `seconds`.

    A discovery request runs on an `httplib2.Http`, inside google-auth's
    wrapper, whose own timeout is a minute. It keeps its connections open
    between calls, and a connection keeps the timeout it was opened with, so
    those are capped too. A request of any other shape is left alone.
    """
    http = getattr(request, "http", None)
    http = getattr(http, "http", http)
    if not isinstance(http, httplib2.Http):
        return
    http.timeout = seconds
    for connection in http.connections.values():
        connection.timeout = seconds
        if connection.sock is not None:
            connection.sock.settimeout(seconds)


def epoch_window(after: datetime | None = None, before: datetime | None = None) -> str:
    """A Gmail search window in epoch seconds (M20, D3).

    Gmail reads `after:2026/10/01` as Pacific midnight, whatever the mailbox's
    zone, so a date would shift every window by hours. The operators work in
    whole seconds, so the window is widened by a second at each end: an
    overlap stores nothing twice, where a gap could lose a message.
    """
    parts = []
    if after is not None:
        parts.append(f"after:{math.floor(after.timestamp()) - 1}")
    if before is not None:
        parts.append(f"before:{math.ceil(before.timestamp())}")
    return " ".join(parts)


@dataclass(frozen=True, slots=True)
class Profile:
    address: str
    """Lower-cased: the mailbox's own address, and the sync's account key."""

    history_id: str


ChangeKind = Literal["added", "deleted", "labels_added", "labels_removed"]

_CHANGE_KEYS: tuple[tuple[ChangeKind, str], ...] = (
    ("added", "messagesAdded"),
    ("labels_added", "labelsAdded"),
    ("labels_removed", "labelsRemoved"),
    ("deleted", "messagesDeleted"),
)
"""A record's change lists, in the order they are applied."""


@dataclass(frozen=True, slots=True)
class HistoryChange:
    kind: ChangeKind
    message_id: str
    thread_id: str
    label_ids: frozenset[str] = frozenset()
    """The labels added or removed. Empty for an addition or a deletion:
    history carries ids, not a message's labels, so an addition is fetched."""


@dataclass(frozen=True, slots=True)
class HistoryRecord:
    id: str
    changes: tuple[HistoryChange, ...]


@dataclass(frozen=True, slots=True)
class HistoryPage:
    records: tuple[HistoryRecord, ...]
    next_page_token: str | None
    history_id: str
    """The mailbox's current history id: the cursor after the last page."""


def to_history_page(response: dict[str, Any]) -> HistoryPage:
    records: list[HistoryRecord] = []
    for record in response.get("history", []):
        changes: list[HistoryChange] = []
        for kind, key in _CHANGE_KEYS:
            for item in record.get(key, []):
                message = cast(dict[str, Any], item.get("message", {}))
                changes.append(
                    HistoryChange(
                        kind=kind,
                        message_id=str(message["id"]),
                        thread_id=str(message.get("threadId", "")),
                        label_ids=frozenset(cast(list[str], item.get("labelIds", []))),
                    )
                )
        records.append(HistoryRecord(id=str(record["id"]), changes=tuple(changes)))
    return HistoryPage(
        records=tuple(records),
        next_page_token=cast(str | None, response.get("nextPageToken")),
        history_id=str(response["historyId"]),
    )


@dataclass(frozen=True, slots=True)
class MessageMeta:
    """A message without its body: labels, time, and the named headers only."""

    id: str
    thread_id: str
    label_ids: frozenset[str]
    internal_date: datetime
    headers: Mapping[str, str]


def to_message_meta(
    message: dict[str, Any], names: tuple[str, ...] = METADATA_HEADERS
) -> MessageMeta:
    """Convert a `format=metadata` message. Headers outside `names` are
    dropped even if Gmail sends them, and so is anything else it sends: a
    snippet, a body."""
    payload = cast(dict[str, Any], message.get("payload", {}))
    wanted = {name.lower(): name for name in names}
    headers: dict[str, str] = {}
    for header in payload.get("headers", []):
        name = wanted.get(str(header.get("name", "")).lower())
        if name is not None and name not in headers:
            headers[name] = str(header.get("value", ""))

    return MessageMeta(
        id=cast(str, message["id"]),
        thread_id=cast(str, message["threadId"]),
        label_ids=frozenset(cast(list[str], message.get("labelIds", []))),
        # internalDate, never the Date header: Gmail sets it, senders do not.
        internal_date=datetime.fromtimestamp(int(message["internalDate"]) / 1000, tz=UTC),
        headers=MappingProxyType(headers),
    )


def _decode(data: str) -> str:
    """Decode Gmail's base64url payload. Returns "" on malformed input."""
    try:
        raw = base64.urlsafe_b64decode(data.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError, ValueError):
        return ""
    # Real mail is frequently mislabelled; never assume utf-8 decodes cleanly.
    return raw.decode("utf-8", errors="replace")


_ALWAYS_HIDDEN = frozenset({"head", "title", "style", "script", "template", "noscript"})
_BLOCK = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "dd",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "thead",
        "tfoot",
        "tr",
        "ul",
    }
)
_CELL = frozenset({"td", "th"})

_ZERO = re.compile(r"[+-]?0*(?:\.0*)?(?:px|pt|pc|em|rem|ex|ch|q|cm|mm|in|v\w+|%)?")
"""A length that is zero, in any CSS unit or none: `0`, `0px`, `0.0mm`, `0vmin`.
The zeros after the point come only after the point, so a long run of zeros
cannot be split two ways."""
_CSS_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6})\s?|\\(.)")
_CSS_IMPORTANT = re.compile(r"!\s*important", re.IGNORECASE)
_SPACES = re.compile(r"\s+")

# Preformatted spaces and newlines, protected through the line-by-line cleanup
# that collapses the rest, then restored.
_PRE_SPACE = "\ue020"
_PRE_NEWLINE = "\ue00a"

# html5lib yields names in the XHTML namespace; strip it to the bare tag.
_HTML_NS = "{http://www.w3.org/1999/xhtml}"


def _strip_css_comments(text: str) -> str:
    """CSS with its comments removed as a browser removes them: a comment
    left open runs to the end. One pass: a lazy regex scanned to the end
    again from every opener left open, which made a long style quadratic."""
    kept: list[str] = []
    start = 0
    while (opening := text.find("/*", start)) != -1:
        kept.append(text[start:opening])
        closing = text.find("*/", opening + 2)
        if closing == -1:
            return "".join(kept)
        start = closing + 2
    kept.append(text[start:])
    return "".join(kept)


def _css_value(raw: str) -> str:
    """A CSS value with escapes decoded, `!important` and whitespace removed,
    lower-cased: `display:\\6e one` comes back `none`. Comments are already
    gone (`_hides`)."""
    decoded = _CSS_ESCAPE.sub(_css_escape, raw)
    return _CSS_IMPORTANT.sub("", decoded).strip().lower()


def _css_escape(match: re.Match[str]) -> str:
    """One CSS escape, decoded as the CSS Syntax spec decodes it: zero, a
    surrogate, or a number past the last code point is U+FFFD. Six hex digits
    reach past it, and `chr` would raise, so any sender could fail their own
    message on every fetch (phase-3 review)."""
    if match.group(1) is None:
        return match.group(2)
    point = int(match.group(1), 16)
    if point == 0 or 0xD800 <= point <= 0xDFFF or point > 0x10FFFF:
        return chr(0xFFFD)
    return chr(point)


def _hides(attrs: dict[str, str | None]) -> bool:
    """Whether an element's own markup hides it (M18, D1).

    Inline style only: colour matched to the background, or a stylesheet class,
    is beyond markup, and the injection suite carries it as a known gap.
    """
    if "hidden" in attrs:
        return True
    style = attrs.get("style") or ""
    # Comments go first, before the split, so `display:/**/none` is `none` and
    # one left open hides what follows it, as in a browser.
    for declaration in _strip_css_comments(style).split(";"):
        name, sep, value = declaration.partition(":")
        if not sep:
            continue
        name = _css_value(name)
        value = _css_value(value)
        shorthand = value.split()[0] if value else ""
        if (
            (name == "display" and value == "none")
            or (name == "visibility" and value in ("hidden", "collapse"))
            or (
                name in ("font-size", "max-height", "max-width", "height", "width")
                and shorthand
                and _ZERO.fullmatch(shorthand)
            )
            or (name == "font" and shorthand and _ZERO.fullmatch(shorthand.split("/")[0]))
            or (name == "opacity" and value and _opacity_zero(value))
            or (name == "mso-hide" and value == "all")
            or (name == "text-indent" and value in ("-9999px", "-999em", "-9999em"))
        ):
            return True
    return False


def _opacity_zero(value: str) -> bool:
    """Opacity at or below zero: CSS clamps a negative to 0."""
    try:
        return float(value.rstrip("%")) <= 0
    except ValueError:
        return False


def _protect_pre(text: str) -> str:
    return text.replace(" ", _PRE_SPACE).replace("\t", _PRE_SPACE).replace("\n", _PRE_NEWLINE)


def _visible_text(text: str, pre: bool) -> str:
    return _protect_pre(text) if pre else _SPACES.sub(" ", text)


def _walk_visible(root: Any, chunks: list[str]) -> None:
    """Append the text a reader sees under `root`, in document order.

    A loop over a stack of work, not recursion: an email can nest elements
    deeper than Python's recursion limit, and html5lib keeps every level, so a
    recursive walk let any sender fail their own message (phase-3 review).
    Each item is an element still to open, with whether it sits in `<pre>`, or
    text already decided: a closing break, or a child's tail, which shows even
    when the child is hidden.
    """
    stack: list[tuple[Any, bool] | str] = [(root, False)]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            chunks.append(item)
            continue
        element, in_pre = item
        name = element.tag
        if not isinstance(name, str):
            # A comment or processing instruction: never shown, and its `.text`
            # is the comment body, which must not reach the model.
            continue
        tag = name.replace(_HTML_NS, "")
        # SVG and MathML keep their namespace in the tag. Gmail draws neither,
        # so their text is text the owner never sees (18.11).
        if tag.startswith("{") or tag in _ALWAYS_HIDDEN or _hides(element.attrib):
            continue
        pre = in_pre or tag == "pre"
        # A block stands on its own lines, a cell apart from its neighbours,
        # and a line break opens a line and closes nothing.
        apart = "\n" if tag in _BLOCK else " " if tag in _CELL else ""
        chunks.append("\n" if tag == "br" else apart)
        if element.text:
            chunks.append(_visible_text(element.text, pre))
        # Pushed in reverse, so each comes off in document order: a child, then
        # its tail, then the next child, and the element's closing break last.
        stack.append(apart)
        for child in reversed(list(element)):
            if child.tail:
                stack.append(_visible_text(child.tail, pre))
            stack.append((child, pre))


def _html_to_text(markup: str) -> str:
    """The text a reader of the HTML would see, parsed with the WHATWG tree
    algorithm (html5lib) so hidden content is nested exactly as the owner's
    client nests it. A regex or a lenient parser lets an attacker close a
    hidden element early with mismatched tags; the browser algorithm does not.
    """
    import html5lib

    document = html5lib.parse(markup, namespaceHTMLElements=False)
    chunks: list[str] = []
    _walk_visible(document, chunks)
    lines = [_SPACES.sub(" ", line).strip() for line in "".join(chunks).split("\n")]
    collapsed = re.sub(r"\n{2,}", "\n", "\n".join(lines)).strip()
    return collapsed.replace(_PRE_SPACE, " ").replace(_PRE_NEWLINE, "\n")


def _is_attachment(part: dict[str, Any]) -> bool:
    if part.get("filename"):
        return True
    for header in part.get("headers", []):
        if header.get("name", "").lower() == "content-disposition":
            return cast(str, header.get("value", "")).strip().lower().startswith("attachment")
    return False


def _shown(part: dict[str, Any]) -> list[tuple[str, str]]:
    """The text parts a reader is shown, in order, as (MIME type, text).

    Attachments and every part inside an attached message are skipped. Of a
    `multipart/alternative`, the last alternative holding HTML is shown, else
    the last holding text: alternatives run from plainest to richest, and
    Gmail shows the owner the HTML. Other containers show all their parts."""
    mime = cast(str, part.get("mimeType", "")).lower()
    if _is_attachment(part) or mime == "message/rfc822":
        return []
    children = [cast(dict[str, Any], child) for child in part.get("parts", [])]
    if mime == "multipart/alternative":
        options = [shown for shown in map(_shown, children) if shown]
        rich = [shown for shown in options if any(kind == "text/html" for kind, _ in shown)]
        return (rich or options or [[]])[-1]
    if children:
        return [shown for child in children for shown in _shown(child)]
    data = part.get("body", {}).get("data")
    if data and mime in ("text/plain", "text/html"):
        return [(mime, _decode(data))]
    return []


def extract_body(payload: dict[str, Any]) -> str:
    """The text of a Gmail payload as its reader sees it (M18, D1), normalised.

    HTML is preferred to `text/plain`, hidden content is removed, attachments
    and attached messages are skipped, and parts shown one after another are
    joined by a blank line. Returns "" when no part is text (attachment-only
    mail).
    """
    texts = [
        _html_to_text(text) if kind == "text/html" else text.strip()
        for kind, text in _shown(payload)
    ]
    return normalise("\n\n".join(text for text in texts if text))


def _header(payload: dict[str, Any], name: str) -> str:
    lowered = name.lower()
    for header in payload.get("headers", []):
        if header.get("name", "").lower() == lowered:
            return cast(str, header.get("value", ""))
    return ""


def _addresses(raw: str) -> list[str]:
    return [addr.lower() for _, addr in getaddresses([raw]) if addr]


def to_email_message(message: dict[str, Any]) -> EmailMessage:
    """Convert a Gmail `users.messages.get` response into our contract type,
    scrubbed (M18, D2): nothing leaves this function with a code or a link
    but a meeting link.

    Credential mail leaves flagged, its body a fixed notice; its subject keeps
    no token shaped like a code, cue word or not. Other mail leaves with its
    subject and body scrubbed. Addresses are left as they are: a guest's
    address is what the owner checks."""
    payload = cast(dict[str, Any], message.get("payload", {}))

    # internalDate is epoch milliseconds, UTC, and set by Gmail itself -- more
    # reliable than the Date: header, which senders routinely get wrong.
    received_at = datetime.fromtimestamp(int(message["internalDate"]) / 1000, tz=UTC)

    sender = _addresses(_header(payload, "From"))
    recipients = _addresses(_header(payload, "To")) + _addresses(_header(payload, "Cc"))
    subject, body, credential = prepare(_header(payload, "Subject"), extract_body(payload))

    return EmailMessage(
        id=cast(str, message["id"]),
        thread_id=cast(str, message["threadId"]),
        subject=subject,
        body_text=body,
        sender=sender[0] if sender else "",
        recipients=recipients,
        received_at=received_at,
        credential=credential,
    )


class GmailClient:
    def __init__(
        self,
        service: Any,
        *,
        pacer: Pacing | None = None,
        share: str | None = None,
        retry_for: float = RETRY_FOR,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """`pacer` defaults to the process's own (`app.mail.quota.PACER`), read
        at each call. `share` charges every call to a share of the minute: the
        sync's client is built with `share=quota.SYNC`."""
        self._service = service
        self._pacer = pacer
        self._share = share
        self._retry_for = retry_for
        self._sleep = sleep
        self._clock = clock

    def _execute(self, method: str, request: Any) -> dict[str, Any]:
        """Run one request, retried while Gmail or the network fails for
        everyone, all within `retry_for` seconds.

        Every Gmail call goes through here, so none escapes the pacer, and
        every attempt is priced: a retry costs quota like any call. The
        budget starts before the pacer is asked, so its wait counts; each
        attempt's sockets get only the time left, so one hung attempt cannot
        outlast it; and the pauses are jittered, so callers that failed
        together do not come back together. A failure that is not transient
        -- a 404, a 400 -- is raised at once.
        """
        pacer = self._pacer if self._pacer is not None else quota.PACER
        deadline = self._clock() + self._retry_for
        pause = _FIRST_RETRY_AFTER
        while True:
            pacer.spend(
                method,
                share=self._share,
                wait_for=max(deadline - self._clock() - SHORTEST_ATTEMPT, 0.0),
            )
            _cap_timeout(request, max(deadline - self._clock(), SHORTEST_ATTEMPT))
            try:
                return cast(dict[str, Any], request.execute())
            except Exception as exc:
                wait = pause * random.uniform(0.5, 1.5)
                if not is_transient(exc) or self._clock() + wait + SHORTEST_ATTEMPT > deadline:
                    raise
            self._sleep(wait)
            pause *= 2

    def profile(self) -> Profile:
        """The mailbox's address and current history id, in one call."""
        response = self._execute("getProfile", self._service.users().getProfile(userId="me"))
        return Profile(
            address=str(response["emailAddress"]).lower(),
            history_id=str(response["historyId"]),
        )

    def history_page(self, start_history_id: str, page_token: str | None = None) -> HistoryPage:
        """One page of the records after `start_history_id`, which Gmail leaves out.

        Raises `CursorExpiredError` when Gmail no longer keeps that history.
        """
        request = (
            self._service.users()
            .history()
            .list(
                userId="me",
                startHistoryId=start_history_id,
                historyTypes=list(HISTORY_TYPES),
                maxResults=HISTORY_PAGE_SIZE,
                pageToken=page_token,
            )
        )
        try:
            response = self._execute("history.list", request)
        except HttpError as exc:
            if _status(exc) == 404:
                raise CursorExpiredError(start_history_id) from exc
            raise
        return to_history_page(response)

    def message_metadata(self, message_id: str) -> MessageMeta:
        """A message's labels, time and six headers, through the field mask.

        Raises `MessageGoneError` for a 404.
        """
        request = (
            self._service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=list(SYNC_HEADERS),
                fields=SYNC_FIELDS,
            )
        )
        try:
            response = self._execute("messages.get", request)
        except HttpError as exc:
            if _status(exc) == 404:
                raise MessageGoneError(message_id) from exc
            raise
        return to_message_meta(response, SYNC_HEADERS)

    def message_ids(
        self, query: str, *, after: datetime | None = None, before: datetime | None = None
    ) -> list[str]:
        """Every message matching `query`, in an epoch-second window, across all pages.

        Unbounded, like `thread_ids`: the window bounds it. Spam and trash are
        never listed (Gmail's default).
        """
        q = f"{query} {epoch_window(after, before)}".strip()
        ids: list[str] = []
        page_token: str | None = None
        while True:
            response = self._execute(
                "messages.list",
                self._service.users()
                .messages()
                .list(userId="me", q=q, maxResults=500, pageToken=page_token),
            )
            ids.extend(str(message["id"]) for message in response.get("messages", []))
            page_token = cast(str | None, response.get("nextPageToken"))
            if not page_token:
                return ids

    def list_unread(self, max_results: int = 10) -> list[str]:
        """Return unread message IDs, newest first."""
        response = self._execute(
            "messages.list",
            self._service.users()
            .messages()
            .list(userId="me", q="is:unread", maxResults=max_results),
        )
        return [cast(str, m["id"]) for m in response.get("messages", [])]

    def search(self, query: str, limit: int = 200) -> list[str]:
        """Message IDs matching a Gmail search query, newest first.

        Paginated, unlike `list_unread`: retrieval ingestion walks months of
        history, and Gmail caps a single page at 500 regardless of what
        `maxResults` asks for. Stops at `limit` so a wide query cannot turn into
        an unbounded walk of the whole mailbox.
        """
        ids: list[str] = []
        page_token: str | None = None

        while len(ids) < limit:
            response = self._execute(
                "messages.list",
                self._service.users()
                .messages()
                .list(
                    userId="me",
                    q=query,
                    maxResults=min(500, limit - len(ids)),
                    pageToken=page_token,
                ),
            )
            ids.extend(cast(str, m["id"]) for m in response.get("messages", []))

            page_token = cast(str | None, response.get("nextPageToken"))
            if not page_token:
                break

        return ids[:limit]

    def get_message(self, message_id: str) -> EmailMessage:
        """The whole message, for the pipeline. Raises `MessageGoneError` for
        a 404, and for a message now in the trash or spam: one deleted or
        binned before its turn is recorded, not processed or retried (M20,
        D4). The labels come with the message at no extra cost, and are
        newer than the sync's, which may lag."""
        request = self._service.users().messages().get(userId="me", id=message_id, format="full")
        try:
            response = self._execute("messages.get", request)
        except HttpError as exc:
            if _status(exc) == 404:
                raise MessageGoneError(message_id) from exc
            raise
        if BINNED & set(response.get("labelIds", [])):
            raise MessageGoneError(message_id)
        return to_email_message(response)

    def current_history_id(self) -> str:
        """Mailbox history cursor. M04 seeds incremental sync from this."""
        return self.profile().history_id

    def profile_address(self) -> str:
        """The mailbox's own address, lower-cased for comparison."""
        return self.profile().address

    def thread_ids(self, query: str) -> list[str]:
        """Every thread matching a Gmail search query, across all pages.

        No limit, unlike `search`: M15's measurement counts a fixed window, and
        a capped walk would quietly undercount a busy fortnight. The window in
        the query is what bounds it.
        """
        ids: list[str] = []
        page_token: str | None = None
        while True:
            response = self._execute(
                "threads.list",
                self._service.users()
                .threads()
                .list(userId="me", q=query, maxResults=500, pageToken=page_token),
            )
            ids.extend(cast(str, thread["id"]) for thread in response.get("threads", []))
            page_token = cast(str | None, response.get("nextPageToken"))
            if not page_token:
                return ids

    def thread_metadata(self, thread_id: str) -> list[MessageMeta]:
        """A thread's messages as metadata only: no bodies are requested."""
        response = self._execute(
            "threads.get",
            self._service.users()
            .threads()
            .get(
                userId="me",
                id=thread_id,
                format="metadata",
                metadataHeaders=list(METADATA_HEADERS),
            ),
        )
        return [to_message_meta(message) for message in response.get("messages", [])]

    def thread_headers(self, thread_id: str) -> dict[str, Any]:
        """A thread as the recipient rule reads it (M17, D4): each message's
        labels and `GUEST_HEADERS`, in Gmail's order, and nothing else.

        A thread Gmail no longer has comes back empty: no one in it is a
        participant, so every guest is outside until the owner allows them.
        """
        try:
            return self._execute(
                "threads.get",
                self._service.users()
                .threads()
                .get(
                    userId="me",
                    id=thread_id,
                    format="metadata",
                    metadataHeaders=list(GUEST_HEADERS),
                    fields=GUEST_FIELDS,
                ),
            )
        except Exception as exc:
            if _status(exc) == 404:
                return {"messages": []}
            raise
