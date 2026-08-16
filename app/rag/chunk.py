"""Splitting a cleaned message into retrievable pieces.

Thread-aware rather than fixed-size. Most email is short enough to be a single
chunk, and cutting a four-line message into two halves produces two fragments
that each answer nothing. Splitting only happens when a message is genuinely
long, and then on paragraph boundaries, because a paragraph is the smallest
unit of email that still carries an intact thought.

Every chunk keeps `thread_id`, `participants`, and `sent_at`. M11 filters on
them, and they are what lets the agent answer "when did we last discuss this?"
rather than merely "this was discussed".
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime

from app.contracts import EmailMessage
from app.rag.clean import clean

MAX_CHARS = 1200
"""Target chunk size.

Comfortably inside `gemini-embedding-001`'s 2048-token input limit even for
dense text, and small enough that a hit points at a passage rather than at a
whole message. Larger chunks retrieve more often and answer less precisely --
the embedding of a long chunk is an average, and averages match everything
weakly.
"""

OVERLAP_CHARS = 200
"""Carried from the tail of the previous chunk, so a sentence spanning a
boundary is still findable from either side."""

SEPARATOR = "\n\n"

HARD_MAX_CHARS = MAX_CHARS + OVERLAP_CHARS + len(SEPARATOR)
"""The ceiling that actually holds.

Overlap is prepended to a unit that was already packed to `MAX_CHARS`, so that
constant is a packing target, not a bound. The separator is in here because
leaving it out makes this off by two -- which is harmless in itself and exactly
the kind of quietly-false invariant that is worth not writing down.
"""

MIN_CHARS = 8
"""Below this a chunk is "ok", "yes", or a stray line of punctuation.

Deliberately low. Length is a poor proxy for whether a chunk carries meaning,
and the expensive mistake is the wrong one: dropping "Sounds good" loses an
agreement from a system whose entire job is remembering what was agreed, while
keeping a stray "ok" costs a row that no query will ever rank. The subject line
travels with every chunk into the embedding, so even a three-word reply retrieves
against something. M12 is where this stops being a guess.
"""


@dataclass(frozen=True, slots=True)
class Chunk:
    thread_id: str
    message_id: str
    ordinal: int
    subject: str
    content: str
    participants: tuple[str, ...]
    sent_at: datetime

    @property
    def content_hash(self) -> str:
        """Identity for dedupe: the thread plus the text.

        Scoped to the thread rather than global. A global hash would make two
        unrelated threads that both end in "Sounds good" collide, and the second
        one would silently lose a chunk on insert. Inside a thread, collapsing
        identical text is the intended behaviour -- that is quote leakage the
        cleaner did not catch.
        """
        digest = hashlib.sha256()
        digest.update(self.thread_id.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(self.content.encode("utf-8"))
        return digest.hexdigest()

    def embedding_text(self) -> str:
        """What actually gets embedded.

        The subject and date are prepended here but deliberately not stored in
        `content`. Retrieval should hand the agent the passage that was written,
        not a passage with a header glued on; and the hash must be over the text
        alone, or re-running after a subject-line tweak would look like new
        content. The context still reaches the vector, which is the point --
        "Friday works" means very little without "Re: Q3 planning offsite".
        """
        header = f"Subject: {self.subject}\nDate: {self.sent_at:%Y-%m-%d}"
        if self.participants:
            header += f"\nParticipants: {', '.join(self.participants)}"
        return f"{header}\n\n{self.content}"


_PARAGRAPH = re.compile(r"\n\s*\n")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _split_oversized(paragraph: str) -> list[str]:
    """Break a paragraph that is itself longer than a chunk.

    Sentence boundaries first; a single sentence longer than `MAX_CHARS` is
    almost always machine-generated (a pasted URL, a base64 blob) and gets cut
    bluntly, which is the right treatment for something nobody will read.
    """
    pieces: list[str] = []
    current = ""

    for sentence in _SENTENCE.split(paragraph):
        while len(sentence) > MAX_CHARS:
            pieces.append(sentence[:MAX_CHARS])
            sentence = sentence[MAX_CHARS:]
        if not sentence:
            continue
        if current and len(current) + 1 + len(sentence) > MAX_CHARS:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()

    if current:
        pieces.append(current)
    return pieces


def split(text: str) -> list[str]:
    """Pack paragraphs into chunks, targeting `MAX_CHARS` and bounded by
    `HARD_MAX_CHARS` once overlap is added."""
    if not text.strip():
        return []

    units: list[str] = []
    for paragraph in _PARAGRAPH.split(text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        units.extend(_split_oversized(paragraph) if len(paragraph) > MAX_CHARS else [paragraph])

    chunks: list[str] = []
    current = ""

    for unit in units:
        candidate = f"{current}{SEPARATOR}{unit}" if current else unit
        if len(candidate) <= MAX_CHARS:
            current = candidate
            continue

        if current:
            chunks.append(current)
            # Overlap is taken from the tail of what was just emitted. Cut on a
            # whitespace boundary so the carried fragment starts at a word.
            tail = current[-OVERLAP_CHARS:]
            space = tail.find(" ")
            current = f"{tail[space + 1 :]}{SEPARATOR}{unit}" if space != -1 else unit
        else:
            current = unit

    if current:
        chunks.append(current)

    return [c for c in chunks if len(c) >= MIN_CHARS]


def chunk_message(message: EmailMessage, owner_email: str = "") -> list[Chunk]:
    """Clean and split one message. Returns [] when nothing original remains."""
    body = clean(message.body_text)
    if not body:
        return []

    participants = tuple(
        dict.fromkeys(
            address
            for address in [message.sender, *message.recipients]
            # The owner is on every message in the mailbox, so keeping them adds
            # a constant to every row and filters nothing.
            if address and address != owner_email.lower()
        )
    )

    return [
        Chunk(
            thread_id=message.thread_id,
            message_id=message.id,
            ordinal=ordinal,
            subject=message.subject,
            content=content,
            participants=participants,
            sent_at=message.received_at,
        )
        for ordinal, content in enumerate(split(body))
    ]
