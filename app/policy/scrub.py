"""Mail made safe for a model to read (M18, D1 and D2).

Applied where a message leaves the Gmail client, and again whenever a prompt
is assembled, so a checkpoint made before M18 is scrubbed on its next read.
Scrubbing twice changes nothing.

**Normalised first** (D1): Unicode NFKC, then format characters (zero-width
spaces, joiners and non-joiners, the byte-order mark, bidirectional controls)
and private-use characters removed, every space character made a space, and
line endings unified. Full-width letters, a zero-width space inside a code, or
a non-breaking space between its digits then hide nothing from the rules
below. Private-use characters go too, because they carry no text, and this
module uses them as placeholders.

**Credential mail** (decision 2): a strong phrase -- "verification code",
"one-time code", "sign-in code", "reset your password", "temporary password",
"recovery code", "two-factor" and their kin -- as whole words, in the subject
or anywhere in the body. Such a message is set aside whole before any model
reads it (`app/google/gmail.py`, `app/graph/`). What meeting invites say
("passcode", "PIN", "access code", "meeting password") is not a strong phrase.

**Links** (decision 1), handled before codes, so a meeting id in a kept link's
path is never read as one:
- `http`, `https` and `www.` URLs, and bare `host/path` forms. Defanged links
  (`hxxps`, `example[.]com`) are found too.
- Known wrappers are unwrapped first: Microsoft Safe Links, Proofpoint URL
  Defense (v1 to v3), Mimecast (which keeps only the domain), and Google's
  redirect.
- A link to a meeting host (`MEETING_HOSTS`, matched exactly or as a
  subdomain at a dot boundary) keeps its host and path, over https. Of its
  query, only the keys a meeting needs survive, and its fragment goes. A path
  that signs in, resets or verifies is not a meeting link.
- Every other link becomes `[link: host]`. A non-ASCII host is shown as
  punycode, so a look-alike cannot pass for a real one.

**Codes elsewhere:** within three non-empty lines of a cue word (code, OTP,
passcode, PIN, verification), a token of 4 to 10 digits, possibly in two
groups split by a space or a dash, or of 5 to 10 letters and digits with both
in it. It becomes `[code removed]`. These are not codes:
- times ("at 1430", "14:30", "1430 hrs", "14h30");
- years, dates, decimals and versions;
- phone numbers: after a `+` or an area code in brackets, or in three groups
  or more;
- numbers after a label: a room, floor, extension, order, ticket, booking or
  reference;
- email addresses, and what is already a placeholder.

**What the model writes** (D6): an event's title and location, scrubbed the
same way and folded onto one line (`scrub_line`) before a card shows them or
an event's arguments are hashed.

**Logged** as counts by kind, never the removed text.
"""

from __future__ import annotations

import base64
import logging
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlsplit, urlunsplit

log = logging.getLogger(__name__)

REMOVED_CODE = "[code removed]"

CREDENTIAL_NOTICE = (
    "[This message carried a sign-in code, a sign-in or reset link, or a secret. "
    "It was set aside before any model read it, and its text was not kept.]"
)
"""The whole body of credential mail once it leaves the Gmail client."""

MEETING_HOSTS: dict[str, frozenset[str]] = {
    "meet.google.com": frozenset(),
    "zoom.us": frozenset(),
    "zoom.com": frozenset(),
    "zoomgov.com": frozenset(),
    "teams.microsoft.com": frozenset({"context"}),
    "teams.live.com": frozenset(),
    "webex.com": frozenset({"mtid"}),
    "gotomeeting.com": frozenset(),
    "goto.com": frozenset(),
    "gotomeet.me": frozenset(),
    "meet.jit.si": frozenset(),
    "8x8.vc": frozenset(),
    "whereby.com": frozenset(),
}
"""The meeting hosts whose links survive (decision 1), each with the query
keys it keeps, lower-cased. Webex's `MTID` is its meeting id; Teams' `context`
names the meeting's tenant. Everything else in a query -- Zoom's `pwd`, Teams'
`p`, Webex's `tk` -- is a passcode or a token, and goes."""

WINDOW = 3
"""How many non-empty lines from a cue word a code may be."""

_DASHES = r"\-\u2010-\u2015\u2212"
_GAP = rf"[ \t{_DASHES}]*\n?[ \t]*"
"""Between the words of a strong phrase: spaces, tabs, a dash, or a single
line break, so "sign-in", "sign in" and "signin" are all one phrase. At most
one newline, so a phrase does not span a blank line between two paragraphs
(M18, finding 9)."""

_STRONG_PHRASES = (
    rf"verification{_GAP}(?:codes?|links?)",
    rf"confirmation{_GAP}codes?",
    rf"one{_GAP}time{_GAP}(?:pass)?(?:codes?|words?|pins?)",
    rf"single{_GAP}use{_GAP}(?:codes?|passwords?)",
    rf"(?:sign|log){_GAP}(?:in|on){_GAP}(?:codes?|links?)",
    rf"security{_GAP}(?:codes?|keys?)",
    rf"authentication{_GAP}codes?",
    rf"recovery{_GAP}(?:codes?|keys?)",
    rf"backup{_GAP}codes?",
    rf"magic{_GAP}links?",
    rf"reset{_GAP}(?:your{_GAP}|the{_GAP})?password",
    rf"password{_GAP}reset",
    rf"forgot(?:ten)?{_GAP}(?:your{_GAP})?password",
    rf"(?:temporary|new){_GAP}password",
    rf"(?:your{_GAP})?password{_GAP}is",
    rf"(?:two|2){_GAP}(?:factor|step){_GAP}(?:verification|authentication)?",
    r"2fa",
    rf"multi{_GAP}factor",
    r"mfa",
    rf"api{_GAP}(?:keys?|tokens?|secrets?)",
    rf"access{_GAP}tokens?",
    rf"(?:client|secret|private){_GAP}(?:keys?|secrets?)",
    rf"bearer{_GAP}tokens?",
    r"otp",
)
_STRONG = re.compile(r"\b(?:" + "|".join(_STRONG_PHRASES) + r")\b", re.IGNORECASE)

_CUE = re.compile(r"\b(?:codes?|otp|passcodes?|pins?|verification)\b", re.IGNORECASE)

_DEFANG = re.compile(r"\[\.\]|\(\.\)|\{\.\}")
_HXXP = re.compile(r"\bhxxp(s?)(?=://)", re.IGNORECASE)
_URL = re.compile(
    r"(?:\bhttps?://|\bwww\.)[^\s<>\"'`]{1,2000}"
    r"|\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.){1,20}[a-z]{2,24}/[^\s<>\"'`]{0,2000}",
    re.IGNORECASE,
)
"""The greedy parts are length-bounded so a very long run of URL-shaped
characters cannot make matching quadratic (M18, finding 8)."""
_TRAILING = ".,;:!?'\")]}"
_SIGN_IN_PATH = re.compile(
    r"(?:^|/)(?:sign-?in|sign-?up|log-?in|activat\w*|verif\w*|reset\w*|password\w*|"
    r"passwd|auth\w*|oauth\w*|sso|saml|token\w*)(?:[/_.?-]|$)",
    re.IGNORECASE,
)

_FROZEN = re.compile(
    r"\[link: [^\]\n]*\]|\[link\]|\[code removed\]|[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,8}"
)
"""What the code pass must not read: link markers, removed-code markers (whose
"code" would otherwise be a cue), and email addresses (a guest's address). The
local and domain parts are length-bounded so a long run of word characters
without an at-sign cannot make matching quadratic (M18, finding 8)."""

_DIGITS = re.compile(rf"(?<![\w+#{_DASHES}])\d+(?:[ \t{_DASHES}]\d+)*(?!\w)")
_DIGITS_STRICT = re.compile(rf"(?<!\d)\d+(?:[ \t{_DASHES}]\d+)*")
"""For a credential subject (M18, finding 4): every digit run, even one behind
a `G-` prefix or glued to a word, since the message already carries a secret."""
_GROUP_SPLIT = re.compile(rf"[ \t{_DASHES}]")
_ALNUM = re.compile(
    r"(?<![\w@./+#\-])(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{5,10}"
    r"(?![\w@/\-]|\.\w)"
)
_NUMBER_PART_BEFORE = re.compile(r"\d[:.,/]$|/$")
_NUMBER_PART_AFTER = re.compile(r"[:.,/]\d|/")
_TIME_BEFORE = re.compile(
    r"(?:\b(?:at|from|to|until|till|by|before|after|around|between|and)|@)\s*$",
    re.IGNORECASE,
)
_TIME_AFTER = re.compile(r"\s*(?:h|hrs?|hours|utc|gmt|z|[ap]\.?m\.?)(?!\w)", re.IGNORECASE)
_PHONE_BEFORE = re.compile(
    r"(?:\+|\(\s*\d{1,5}\s*\)|\b(?:tel|phone|mobile|cell|fax|call|dial|whatsapp)\b[^\n\d]{0,15})"
    r"\s*[-.]?\s*$",
    re.IGNORECASE,
)
_LABEL_BEFORE = re.compile(
    r"\b(?:room|rm|floor|fl|suite|office|unit|level|flat|apt|apartment|building|bldg|block|"
    r"gate|desk|ext|extension|order|invoice|ticket|booking|reservation|reference|ref|case|po)"
    r"\b\.?(?:\s*(?:no|number|num|id|#)\.?)?\s*(?:is|was|:|-|#)?\s*$",
    re.IGNORECASE,
)
_TIME_TOKEN = re.compile(
    r"\d{1,2}[h:]\d{2}|\d{1,4}\s*(?:am|pm|hrs?|hours|utc|gmt|z)", re.IGNORECASE
)
"""An alphanumeric token that is really a time: `9h30`, `14:30`, `1430hrs`,
`1030am`, `0900utc`."""
_ORDINAL = re.compile(r"\d+(?:st|nd|rd|th)", re.IGNORECASE)
_CLOCK = re.compile(r"[0-2]?\d[0-5]\d")
"""A four-digit 24-hour clock time, 0000 to 2359, for a time range like
`1430-1530` (M18, finding 7)."""

_HOLD_OPEN = chr(0xE000)
_HOLD_CLOSE = chr(0xE001)
_HOLD_DIGIT = 0xE100
"""A placeholder is its index in hexadecimal, each digit a private-use
character from here on: invisible to every rule, and never a digit, a letter
or a space, however many links one message holds. `normalise` removes
private-use characters, so mail cannot forge one."""
_HELD = re.compile(f"{_HOLD_OPEN}([{chr(_HOLD_DIGIT)}-{chr(_HOLD_DIGIT + 15)}]+){_HOLD_CLOSE}")


@dataclass(frozen=True, slots=True)
class Scrubbed:
    """A scrubbed text and what was done to it, by kind."""

    text: str
    links: int
    """Links rewritten to `[link: host]`."""
    meeting_links: int
    """Meeting links kept, without their passcodes."""
    codes: int
    """Codes rewritten to `[code removed]`."""


def normalise(text: str) -> str:
    """NFKC; format and private-use characters removed; spaces made spaces."""
    folded = unicodedata.normalize("NFKC", text)
    kept: list[str] = []
    for char in folded:
        category = unicodedata.category(char)
        if category in ("Cf", "Co"):
            continue
        kept.append(" " if category == "Zs" else char)
    return "".join(kept).replace("\r\n", "\n").replace("\r", "\n")


def is_credential(subject: str, body: str) -> bool:
    """Whether a strong phrase is in the subject or the body (decision 2)."""
    return bool(_STRONG.search(normalise(subject)) or _STRONG.search(normalise(body)))


def scrub(text: str, *, every_line: bool = False) -> str:
    """The text with links and codes removed (D2). Idempotent.

    `every_line` reads every line as near a cue: for the subject of mail
    already known to carry a code, which need not name it."""
    return scrub_counted(text, every_line=every_line).text


def scrub_line(text: str) -> str:
    """A title or a location the model wrote, made as safe as the mail it was
    read from (M18, D6): links and codes removed as `scrub` removes them, all
    of it on one line. Each run of line breaks and spaces becomes one space,
    and other control characters go, so nothing copied from an email stands
    as a line of its own on a card: a forged status line, or the "Correction
    for" line a Telegram reply is routed by. Folded before the scrub, so a cue
    anywhere in it covers all of it, and again after, since normalising can
    add a space. Idempotent."""
    return _one_line(scrub(_one_line(text)))


def _one_line(text: str) -> str:
    kept = "".join(char for char in text if char.isspace() or unicodedata.category(char) != "Cc")
    return " ".join(kept.split())


def redact_secrets(text: str) -> str:
    """A credential message's subject, with every code-shaped run and every
    link removed outright (M18, decision 2 and finding 4): no meeting link is
    kept and no not-a-code exception applies, since the message is already
    known to carry a secret."""
    return scrub_counted(text, every_line=True, strict=True, keep_meeting=False).text


def scrub_counted(
    text: str, *, every_line: bool = False, strict: bool = False, keep_meeting: bool = True
) -> Scrubbed:
    """`scrub`, with what was done counted by kind. `strict` removes every
    code-shaped run with no exception, and `keep_meeting=False` rewrites even an
    allowlisted meeting link to `[link: host]`: both for a credential subject."""
    held: list[str] = []
    text = normalise(text)
    text, counts = _links(text, held, keep_meeting=keep_meeting)
    text = _FROZEN.sub(lambda match: _hold(match.group(0), held), text)
    text, codes = _codes(text, every_line=every_line, strict=strict)
    text = _HELD.sub(lambda match: held[_held_index(match.group(1))], text)
    result = Scrubbed(text=text, links=counts["link"], meeting_links=counts["meeting"], codes=codes)
    if result.links or result.codes:
        log.info(
            "scrubbed mail text: %d link(s) rewritten, %d code(s) removed, %d meeting link(s) kept",
            result.links,
            result.codes,
            result.meeting_links,
        )
    return result


def unwrap(url: str) -> tuple[str, bool]:
    """The URL a known wrapper points to, and whether all of it could be
    recovered. Mimecast keeps only the domain: its link carries a token."""
    recoverable = True
    for _ in range(4):
        inner = _unwrap_once(url)
        if inner is None:
            break
        url, whole = inner
        recoverable = recoverable and whole
    return url, recoverable


# --- links ---------------------------------------------------------------------------


def _links(text: str, held: list[str], *, keep_meeting: bool = True) -> tuple[str, Counter[str]]:
    counts: Counter[str] = Counter()
    text = _DEFANG.sub(".", text)
    text = _HXXP.sub(lambda match: "http" + match.group(1), text)

    def replace(match: re.Match[str]) -> str:
        raw, trailing = _trim(match.group(0))
        rewritten, kept = _rewrite(raw, keep_meeting=keep_meeting)
        counts["meeting" if kept else "link"] += 1
        return (_hold(rewritten, held) if kept else rewritten) + trailing

    return _URL.sub(replace, text), counts


def _trim(url: str) -> tuple[str, str]:
    """Split off punctuation that ends a sentence, not the URL. A closing
    bracket stays when the URL opened one."""
    end = len(url)
    while end and url[end - 1] in _TRAILING:
        char = url[end - 1]
        opener = {")": "(", "]": "[", "}": "{"}.get(char)
        if opener and url[:end].count(opener) >= url[:end].count(char):
            break
        end -= 1
    return url[:end], url[end:]


def _rewrite(raw: str, *, keep_meeting: bool = True) -> tuple[str, bool]:
    """A kept meeting link, or `[link: host]`."""
    # A backslash is a slash in a URL to every browser, so a host written
    # `real.example\.zoom.us` resolves to `real.example`, not a meeting host
    # (M18, finding 1). Fold it before the host is read.
    url = raw.replace("\\", "/")
    url = url if re.match(r"https?://", url, re.IGNORECASE) else "http://" + url
    url, recoverable = unwrap(url)
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").rstrip(".").lower()
    except ValueError:
        return "[link]", False
    if not host:
        return "[link]", False
    keys = _meeting_keys(host)
    if keep_meeting and keys is not None and recoverable and not _SIGN_IN_PATH.search(parts.path):
        query = urlencode(
            [
                (key, value)
                for key, value in parse_qsl(parts.query, keep_blank_values=True)
                if key.lower() in keys
            ]
        )
        return urlunsplit(("https", host, parts.path or "/", query, "")), True
    return f"[link: {_display(host)}]", False


def _host_matches(host: str, domain: str) -> bool:
    """`host` is `domain` itself or a subdomain of it, at a dot boundary, so
    `notmimecast.com` is not a match for `mimecast.com` (M18, finding 12)."""
    return host == domain or host.endswith("." + domain)


def _meeting_keys(host: str) -> frozenset[str] | None:
    for domain, keys in MEETING_HOSTS.items():
        if _host_matches(host, domain):
            return keys
    return None


def _display(host: str) -> str:
    while host.startswith("www."):
        host = host[4:]
    if host.isascii():
        return host
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def _unwrap_once(url: str) -> tuple[str, bool] | None:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    query = parse_qs(parts.query)
    if _host_matches(host, "safelinks.protection.outlook.com"):
        return _first(query, "url")
    if host == "urldefense.proofpoint.com" and parts.path.startswith("/v1/url"):
        return _first(query, "u")
    if host == "urldefense.proofpoint.com" and parts.path.startswith("/v2/url"):
        encoded = query.get("u", [""])[0]
        if not encoded:
            return None
        return unquote(encoded.translate(str.maketrans("-_", "%/"))), True
    if host in ("urldefense.com", "urldefense.proofpoint.com") and "/v3/__" in url:
        return _proofpoint_v3(url)
    if _host_matches(host, "mimecast.com") and parts.path.startswith("/s/"):
        domain = query.get("domain", [""])[0]
        return (f"https://{domain}", False) if domain else None
    if _host_matches(host, "google.com") and parts.path == "/url":
        return _first(query, "q") or _first(query, "url")
    return None


def _first(query: dict[str, list[str]], key: str) -> tuple[str, bool] | None:
    values = query.get(key)
    return (values[0], True) if values and values[0] else None


_V3 = re.compile(r"v3/__(?P<url>.+?)__;(?P<replaced>[A-Za-z0-9_\-]*)")
_V3_RUNS = {
    char: index + 2
    for index, char in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
}


def _proofpoint_v3(url: str) -> tuple[str, bool] | None:
    """Proofpoint v3 carries the URL as is, except characters it replaced with
    `*` (one) or `**` and a run length; the replaced characters follow, base64."""
    match = _V3.search(url)
    if not match:
        return None
    encoded = match.group("replaced")
    try:
        replaced = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return match.group("url"), False
    chars = iter(replaced)

    def substitute(star: re.Match[str]) -> str:
        token = star.group(0)
        length = 1 if token == "*" else _V3_RUNS.get(token[-1], 0)
        return "".join(next(chars, "") for _ in range(length))

    return re.sub(r"\*\*[A-Za-z0-9_\-]|\*", substitute, match.group("url")), True


# --- codes ---------------------------------------------------------------------------


_WINDOW_CHARS = 48
"""How much text either side of a token the code rules read. Bounded so a very
long line does not make the pass quadratic (M18, finding 8); every rule looks
only a few words out."""


def _codes(text: str, *, every_line: bool, strict: bool = False) -> tuple[str, int]:
    lines = text.split("\n")
    filled = [index for index, line in enumerate(lines) if line.strip()]
    cues = [position for position, index in enumerate(filled) if _CUE.search(lines[index])]
    near = (
        set(filled)
        if every_line
        else {
            filled[other]
            for position in cues
            for other in range(max(0, position - WINDOW), min(len(filled), position + WINDOW + 1))
        }
    )
    total = 0
    for index in sorted(near):
        lines[index], removed = _remove_codes(lines[index], strict=strict)
        total += removed
    return "\n".join(lines), total


def _remove_codes(line: str, *, strict: bool) -> tuple[str, int]:
    removed = 0

    def digits(match: re.Match[str]) -> str:
        nonlocal removed
        before = match.string[max(0, match.start() - _WINDOW_CHARS) : match.start()]
        after = match.string[match.end() : match.end() + _WINDOW_CHARS]
        if not _is_digit_code(match.group(0), before, after, strict=strict):
            return match.group(0)
        removed += 1
        return REMOVED_CODE

    def alphanumeric(match: re.Match[str]) -> str:
        nonlocal removed
        token = match.group(0)
        before = match.string[max(0, match.start() - _WINDOW_CHARS) : match.start()]
        if not strict and (
            _TIME_TOKEN.fullmatch(token)
            or _ORDINAL.fullmatch(token)
            or _LABEL_BEFORE.search(before)
        ):
            return token
        removed += 1
        return REMOVED_CODE

    line = (_DIGITS_STRICT if strict else _DIGITS).sub(digits, line)
    line = _ALNUM.sub(alphanumeric, line)
    return line, removed


def _is_digit_code(token: str, before: str, after: str, *, strict: bool) -> bool:
    groups = _GROUP_SPLIT.split(token)
    count = sum(len(group) for group in groups)
    if not 4 <= count <= 10:
        return False  # too short or long
    if strict:
        return True  # a credential subject: every code-shaped run goes
    if len(groups) >= 3:
        # Usually a phone number or a date. A run of single digits, though, is
        # a code split one per cell: "4 8 2 9 1 3" (M18, finding 6).
        return all(len(group) == 1 for group in groups)
    if len(groups) == 2:
        if max(len(group) for group in groups) <= 2:
            return False  # a short date: 10-05
        if all(_CLOCK.fullmatch(group) for group in groups):
            return False  # a time range: 1430-1530 (M18, finding 7)
        if all(len(group) == 4 and 1900 <= int(group) <= 2099 for group in groups):
            return False  # a year range: 2025-2026
    if _NUMBER_PART_BEFORE.search(before) or _NUMBER_PART_AFTER.match(after):
        return False  # part of a time, a decimal, a date or a path
    if len(groups) == 1 and len(token) == 4:
        value = int(token)
        if 1900 <= value <= 2099:
            return False  # a year
        hours, minutes = divmod(value, 100)
        if (
            hours <= 23
            and minutes <= 59
            and (_TIME_BEFORE.search(before) or _TIME_AFTER.match(after))
        ):
            return False  # a time: at 1430, 1430 hrs
    return not (_PHONE_BEFORE.search(before) or _LABEL_BEFORE.search(before))


def _hold(text: str, held: list[str]) -> str:
    """Swap text for a placeholder that no rule can read."""
    held.append(text)
    digits = format(len(held) - 1, "x")
    return _HOLD_OPEN + "".join(chr(_HOLD_DIGIT + int(digit, 16)) for digit in digits) + _HOLD_CLOSE


def _held_index(digits: str) -> int:
    return int("".join(format(ord(digit) - _HOLD_DIGIT, "x") for digit in digits), 16)
