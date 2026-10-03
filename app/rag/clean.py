"""Reducing an email to the part that was actually written this time.

Every function here is pure and operates on plain text, which makes the whole
module testable against recorded bodies with no network and no database. That
matters more than usual: cleaning is the step most likely to be subtly wrong,
and the symptom of getting it wrong is not an exception but retrieval that
merely feels disappointing.

HTML has already been flattened upstream by `app.google.gmail.extract_body`, so
nothing here re-implements that. What arrives is plain text carrying four kinds
of noise, stripped in this order:

1. **Quoted replies.** Removed first, deliberately. A quoted block contains its
   own signature and its own footer; taking the quote out first means the later
   passes never have to reason about them.
2. **Forwarded headers.** The `From:/Sent:/To:/Subject:` block clients paste in.
3. **Signatures.** The `--` delimiter, mobile taglines, and trailing contact
   details.
4. **Boilerplate.** Confidentiality notices, unsubscribe lines, list footers.

Why bother: a signature embedded once per message means a thousand near-identical
vectors, and every search returns the same phone number instead of an answer.
The cost of skipping this is not a broken pipeline, it is a working pipeline
that retrieves nothing useful.

An empty return is a legitimate result -- a message whose entire body was a
quote genuinely contributed nothing new. `app.rag.ingest` counts those, so a
heuristic that starts eating real bodies shows up as a suspicious count rather
than as silence.
"""

from __future__ import annotations

import re

# --- Quoted replies ---------------------------------------------------------

_ATTRIBUTION = re.compile(
    # "On Tue, 12 Aug 2026 at 09:14, Ayesha Malik <a@example.com> wrote:"
    # DOTALL because clients wrap this line, and the wrap lands anywhere.
    r"(?ims)^[ \t>]*On\b.{0,300}?\bwrote:[ \t]*$",
)

_ATTRIBUTION_LOCALISED = re.compile(
    # Outlook and friends, plus the German/French forms that show up in any
    # mailbox that has ever talked to Europe.
    r"(?im)^[ \t>]*(?:"
    r"-{2,}\s*Original Message\s*-{2,}"
    r"|-{2,}\s*Forwarded message\s*-{2,}"
    r"|_{10,}"
    r"|Am\b.{0,200}?\bschrieb\b.{0,120}?:"
    r"|Le\b.{0,200}?\ba (?:é|e)crit\s*:"
    r")[ \t]*$",
)

_HEADER_BLOCK = re.compile(
    # A pasted header block. Anchored on From: followed by Sent:/Date: within a
    # few lines -- `From:` alone is far too common in ordinary prose to cut on.
    r"(?im)^[ \t>]*From:[ \t].+\n(?:[ \t>]*\S.*\n){0,3}?[ \t>]*(?:Sent|Date):[ \t].+$",
)

_QUOTED_LINE = re.compile(r"(?m)^[ \t]*>.*\n?")
"""The trailing newline is consumed on purpose.

Leaving it behind turns every removed quote line into a blank one, and a blank
line is a paragraph boundary -- so an interleaved reply would be split into two
chunks along seams that exist only because something was deleted there.
"""


def strip_quoted(text: str) -> str:
    """Drop quoted history and everything that follows it.

    Truncating at the first attribution rather than removing the block is
    deliberate: what comes after it is, by definition, older. Top-posting is
    close to universal, and the rare bottom-poster loses a reply that is quoted
    verbatim in the next message of the thread anyway.
    """
    return split_quoted(text)[0]


def split_quoted(text: str) -> tuple[str, str]:
    """`(written, quoted)`: what `strip_quoted` keeps, and what it drops -- the
    `>` lines above the first attribution or pasted header block, then
    everything from there on. A guest's source on a card is read from the
    two (M18, D5)."""
    cut = len(text)
    for pattern in (_ATTRIBUTION, _ATTRIBUTION_LOCALISED, _HEADER_BLOCK):
        match = pattern.search(text)
        if match is not None:
            cut = min(cut, match.start())

    head = text[:cut]

    # Interleaved replies still leave `>` lines above the cut.
    return _QUOTED_LINE.sub("", head), "".join(_QUOTED_LINE.findall(head)) + text[cut:]


# --- Signatures -------------------------------------------------------------

_SIG_DELIMITER = re.compile(r"(?m)^--[ \t]*$")

_SIGN_OFF = re.compile(
    # Formal closings only. "Thanks" and "Cheers" are deliberately absent:
    # "Thanks, see you Thursday at 3" is a sign-off *and* the only content in
    # the message, and cutting there would delete the meeting.
    r"(?im)^[ \t]*(?:Best regards|Kind regards|Warm regards|Regards|Sincerely|"
    r"Yours (?:sincerely|faithfully))\b[,.]?",
)

MAX_SIGN_OFF_TAIL = 200
"""How much may follow a formal closing before it stops looking like a name and
a phone number and starts looking like more of the message."""

_MOBILE_TAGLINE = re.compile(
    r"(?im)^[ \t]*(?:"
    r"Sent from my \w+.*"
    r"|Sent from Mail for Windows.*"
    r"|Get Outlook for (?:iOS|Android).*"
    r"|Sent via .*"
    r")$",
)

_CONTACT_LINE = re.compile(
    r"(?i)"
    r"(?:\+?\d[\d\s().-]{7,}\d)"  # phone number
    r"|(?:https?://|www\.)\S+"  # url
    r"|\b[\w.+-]+@[\w-]+\.[\w.-]+\b"  # bare address
    r"|\b(?:mobile|tel|phone|fax|cell|direct)\b[ \t]*:"
    r"|\b(?:CEO|CTO|COO|VP|Director|Manager|Engineer|Founder|Consultant)\b",
)

MAX_SIGNATURE_LINES = 8
"""How far up from the end the contact-block heuristic will reach.

Bounded so a short email that happens to end on a link is not deleted entirely.
"""


def strip_signature(text: str) -> str:
    """Remove the sign-off block.

    Two mechanisms, because only one of them is reliable. The `--` delimiter is
    an actual standard (RFC 3676) and cutting there is safe. Everything after is
    a heuristic over the trailing lines, kept deliberately timid: it stops at
    the first line that reads like prose, so it can trim a contact block but
    cannot run away up the message.
    """
    delimiter = _SIG_DELIMITER.search(text)
    if delimiter is not None:
        text = text[: delimiter.start()]

    # A formal closing near the end. This catches the case the line-by-line
    # heuristic below cannot: a sign-off, a name, a phone number and a URL all
    # wrapped onto one long line, which reads as prose by every measure of
    # length. Bounded by how much follows it, so a closing in the middle of a
    # message does not truncate the rest.
    for closing in _SIGN_OFF.finditer(text):
        if len(text) - closing.start() <= MAX_SIGN_OFF_TAIL:
            text = text[: closing.start()]
            break

    text = _MOBILE_TAGLINE.sub("", text)

    lines = text.rstrip().split("\n")
    keep = len(lines)

    for index in range(len(lines) - 1, max(len(lines) - MAX_SIGNATURE_LINES, 0) - 1, -1):
        line = lines[index].strip()
        if not line:
            continue
        # Long lines are sentences, not contact details, whatever they contain.
        if len(line) > 80 or not _CONTACT_LINE.search(line):
            break
        keep = index

    return "\n".join(lines[:keep])


# --- Boilerplate ------------------------------------------------------------

_BOILERPLATE = re.compile(
    r"(?im)^[ \t]*.{0,120}?\b(?:"
    r"unsubscribe"
    r"|manage (?:your )?(?:email )?preferences"
    r"|view (?:this (?:email|message)|it) in your browser"
    r"|you (?:are )?receiv(?:ed|ing) this (?:email|message) because"
    r"|this (?:e-?mail|message)(?: and any attachments)? (?:is|are) confidential"
    r"|if you are not the intended recipient"
    r"|privacy policy"
    r"|all rights reserved"
    r"|(?:please )?do not reply to this (?:e-?mail|message)"
    r"|sent from an unmonitored (?:inbox|mailbox)"
    r")\b.*$",
)


def strip_boilerplate(text: str) -> str:
    return _BOILERPLATE.sub("", text)


# --- Whitespace -------------------------------------------------------------

_TRAILING_SPACE = re.compile(r"(?m)[ \t]+$")
_LEADING_INDENT = re.compile(r"(?m)^[ \t]{2,}")
"""Flattened HTML tables arrive as a label, a newline, and twenty spaces before
the value. The indentation carries no meaning once the markup is gone, and it
is pure padding inside a chunk budget measured in characters."""
_BLANK_RUN = re.compile(r"\n{3,}")

_ZERO_WIDTH = re.compile(
    "[\N{ZERO WIDTH SPACE}-\N{RIGHT-TO-LEFT MARK}\N{WORD JOINER}\N{ZERO WIDTH NO-BREAK SPACE}]"
)
"""Written as escapes on purpose. Spelled literally these are invisible in the
source, and a reviewer cannot tell a deliberate character class from a stray
paste."""

_NBSP = "\N{NO-BREAK SPACE}"


def normalise_whitespace(text: str) -> str:
    """Collapse the debris that survives every other pass.

    Zero-width characters are stripped because marketing mail is full of them
    and they otherwise turn two identical bodies into two different content
    hashes, quietly defeating dedupe.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(_NBSP, " ")
    text = _ZERO_WIDTH.sub("", text)
    text = _TRAILING_SPACE.sub("", text)
    text = _LEADING_INDENT.sub("", text)
    text = _BLANK_RUN.sub("\n\n", text)
    return text.strip()


def clean(body: str) -> str:
    """Full pipeline. Returns "" when nothing original remains."""
    text = normalise_whitespace(body)
    text = strip_quoted(text)
    text = strip_signature(text)
    text = strip_boilerplate(text)
    return normalise_whitespace(text)
