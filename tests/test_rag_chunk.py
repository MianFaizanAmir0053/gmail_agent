from __future__ import annotations

from datetime import UTC, datetime

from app.contracts import EmailMessage
from app.rag.chunk import HARD_MAX_CHARS, MIN_CHARS, chunk_message, split


def message(body: str, **overrides: object) -> EmailMessage:
    defaults: dict[str, object] = {
        "id": "m1",
        "thread_id": "t1",
        "subject": "Q3 planning",
        "body_text": body,
        "sender": "sara@example.com",
        "recipients": ["me@example.com", "bilal@example.com"],
        "received_at": datetime(2026, 8, 12, 9, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return EmailMessage(**defaults)


# --- splitting --------------------------------------------------------------


def test_a_short_message_is_one_chunk() -> None:
    """Cutting a four-line email in half produces two halves that answer nothing."""
    assert split("Thursday at 3 works. Room 2 is booked.") == [
        "Thursday at 3 works. Room 2 is booked."
    ]


def test_long_text_splits_on_paragraph_boundaries() -> None:
    paragraphs = [f"Paragraph {i}. " + "x" * 400 for i in range(6)]
    chunks = split("\n\n".join(paragraphs))
    assert len(chunks) > 1
    assert all(len(c) <= HARD_MAX_CHARS for c in chunks)


def test_consecutive_chunks_overlap() -> None:
    chunks = split("\n\n".join(f"Paragraph {i} " + "word " * 100 for i in range(4)))
    assert len(chunks) >= 2
    tail = chunks[0][-80:].split()
    assert any(word in chunks[1] for word in tail)


def test_a_single_oversized_paragraph_is_split_on_sentences() -> None:
    paragraph = " ".join(f"Sentence number {i} about scheduling." for i in range(200))
    chunks = split(paragraph)
    assert len(chunks) > 1
    assert all(len(c) <= HARD_MAX_CHARS for c in chunks)


def test_nothing_exceeds_the_hard_ceiling() -> None:
    """`MAX_CHARS` is a packing target; overlap is prepended after packing, so
    the bound that actually holds is the one that includes it."""
    for width in (40, 300, 900):
        chunks = split("\n\n".join(f"Paragraph {i} " + "word " * width for i in range(6)))
        assert chunks
        assert all(len(c) <= HARD_MAX_CHARS for c in chunks)


def test_bare_acknowledgements_are_dropped() -> None:
    assert split("ok") == []
    assert len("ok") < MIN_CHARS


def test_a_short_but_meaningful_reply_survives() -> None:
    """Dropping this loses an agreement, which is the expensive mistake."""
    assert split("Sounds good") == ["Sounds good"]


# --- identity ---------------------------------------------------------------


def test_the_same_message_hashes_identically() -> None:
    """This is what makes a second ingestion run free."""
    first = chunk_message(message("Let's meet Thursday at 3pm in room 2."))
    second = chunk_message(message("Let's meet Thursday at 3pm in room 2."))
    assert [c.content_hash for c in first] == [c.content_hash for c in second]


def test_identical_text_in_different_threads_does_not_collide() -> None:
    """A global content hash would silently drop the second thread's chunk."""
    a = chunk_message(message("Sounds good, see you then.", thread_id="t1"))
    b = chunk_message(message("Sounds good, see you then.", thread_id="t2", id="m2"))
    assert a[0].content_hash != b[0].content_hash


def test_identical_text_within_a_thread_does_collide() -> None:
    """Quote leakage the cleaner missed should collapse, not duplicate."""
    a = chunk_message(message("Sounds good, see you then.", id="m1"))
    b = chunk_message(message("Sounds good, see you then.", id="m2"))
    assert a[0].content_hash == b[0].content_hash


def test_the_subject_reaches_the_vector_but_not_the_stored_content() -> None:
    chunk = chunk_message(message("Friday works."))[0]
    assert chunk.content == "Friday works."
    assert "Q3 planning" in chunk.embedding_text()
    assert "Friday works." in chunk.embedding_text()


def test_a_subject_change_does_not_change_the_hash() -> None:
    """Otherwise re-ingesting after any header edit looks like new content."""
    a = chunk_message(message("Friday works.", subject="Q3 planning"))[0]
    b = chunk_message(message("Friday works.", subject="Re: Q3 planning"))[0]
    assert a.content_hash == b.content_hash


# --- metadata ---------------------------------------------------------------


def test_the_owner_is_stripped_from_participants() -> None:
    """They are on every message in the mailbox, so they filter nothing."""
    chunk = chunk_message(message("Friday works."), owner_email="me@example.com")[0]
    assert "me@example.com" not in chunk.participants
    assert set(chunk.participants) == {"sara@example.com", "bilal@example.com"}


def test_participants_keep_their_order_without_duplicates() -> None:
    chunk = chunk_message(
        message("Friday works.", recipients=["sara@example.com", "bilal@example.com"])
    )[0]
    assert chunk.participants == ("sara@example.com", "bilal@example.com")


def test_a_message_with_no_original_text_produces_no_chunks() -> None:
    assert chunk_message(message("On Mon, someone wrote:\n> old news\n")) == []


def test_ordinals_are_sequential() -> None:
    body = "\n\n".join(f"Paragraph {i} " + "word " * 120 for i in range(5))
    chunks = chunk_message(message(body))
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
