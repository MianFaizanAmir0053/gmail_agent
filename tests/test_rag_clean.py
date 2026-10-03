"""Cleaning is the step whose failure mode is silent, so it gets the most tests."""

from __future__ import annotations

from app.rag.clean import (
    clean,
    normalise_whitespace,
    split_quoted,
    strip_quoted,
    strip_signature,
)

# --- quoted replies ---------------------------------------------------------


def test_split_quoted_gives_what_strip_quoted_keeps_and_what_it_drops() -> None:
    """The two halves a guest's source is read from (M18, D5)."""
    body = (
        "Works for me.\n> an interleaved line\nMore of mine.\n\n"
        "On Tue, 12 Aug 2026 at 09:14, Ayesha <a@example.com> wrote:\n> older text\n"
    )

    written, quoted = split_quoted(body)

    assert written == strip_quoted(body)
    assert "Works for me." in written and "More of mine." in written
    assert "an interleaved line" in quoted and "older text" in quoted
    assert "an interleaved line" not in written and "Works for me." not in quoted


def test_a_body_with_nothing_quoted_splits_into_itself_and_nothing() -> None:
    assert split_quoted("Thursday at 3pm.") == ("Thursday at 3pm.", "")


def test_truncates_at_the_attribution_line() -> None:
    body = (
        "Friday at 3 works for me.\n\n"
        "On Tue, 12 Aug 2026 at 09:14, Ayesha Malik <ayesha@example.com> wrote:\n"
        "> Can we move the review?\n"
        "> I have a conflict on Thursday.\n"
    )
    assert clean(body) == "Friday at 3 works for me."


def test_attribution_may_wrap_across_lines() -> None:
    """Clients wrap this line at unpredictable widths, so the pattern spans newlines."""
    body = "Yes.\n\nOn Tue, 12 Aug 2026 at 09:14,\nAyesha Malik <ayesha@example.com>\nwrote:\n\nold"
    assert clean(body) == "Yes."


def test_removes_interleaved_quoted_lines() -> None:
    body = "> your point about latency\nAgreed, let's benchmark it.\n> and about cost\nAlso agreed."
    assert clean(body) == "Agreed, let's benchmark it.\nAlso agreed."


def test_outlook_original_message_divider() -> None:
    body = "Approved.\n\n-----Original Message-----\nFrom: someone\nSubject: whatever\nbody"
    assert clean(body) == "Approved."


def test_pasted_header_block() -> None:
    body = "See below.\n\nFrom: Bilal Ahmed\nSent: Monday, 11 August 2026 14:02\nTo: me\n\nold text"
    assert clean(body) == "See below."


def test_from_alone_is_not_enough_to_cut() -> None:
    """`From:` appears in ordinary prose. Cutting on it alone eats real bodies."""
    body = "The quote is From: the annual report, page 12.\n\nHappy to walk through it."
    assert "annual report" in clean(body)
    assert "walk through it" in clean(body)


def test_a_fully_quoted_body_cleans_to_nothing() -> None:
    """A legitimate empty result: this message contributed no new text."""
    body = "On Tue, 12 Aug 2026, someone wrote:\n> everything here is old\n"
    assert clean(body) == ""


# --- signatures -------------------------------------------------------------


def test_standard_delimiter_cuts_the_signature() -> None:
    body = "Let's do Thursday.\n\n--\nSara Iqbal\nHead of Platform\n+92 300 1234567"
    assert clean(body) == "Let's do Thursday."


def test_mobile_tagline() -> None:
    assert clean("On my way.\n\nSent from my iPhone") == "On my way."


def test_trailing_contact_block_without_a_delimiter() -> None:
    body = "Confirming Tuesday 10am.\n\nSara Iqbal\nsara@example.com\n+92 300 1234567"
    assert clean(body) == "Confirming Tuesday 10am.\n\nSara Iqbal"


def test_a_long_final_sentence_is_not_mistaken_for_a_signature() -> None:
    """The heuristic stops at anything that reads like prose, whatever it contains."""
    body = (
        "Quick note.\n\n"
        "Please forward the deck to sara@example.com before the call so she has "
        "time to read it properly beforehand."
    )
    assert "forward the deck" in strip_signature(body)


def test_signature_heuristic_cannot_run_away_up_the_message() -> None:
    body = "\n".join(f"contact line {i} +92 300 111222{i}" for i in range(20))
    kept = strip_signature(body)
    assert kept.count("\n") >= 10


def test_a_one_line_sign_off_with_contact_details() -> None:
    """Found by reading real output: the whole block wrapped onto one long line,
    which the line-length heuristic reads as prose."""
    body = (
        "Happy to share more or jump on a call.\n\n"
        "Best regards, Faizan Amir +92-303-0649009 faizan@example.com example.dev"
    )
    assert clean(body) == "Happy to share more or jump on a call."


def test_a_closing_in_the_middle_does_not_truncate_the_message() -> None:
    body = (
        "Regards to the team.\n\n"
        + "There is a great deal still to say about the schedule. " * 6
        + "\n\nThe review is on Thursday."
    )
    assert "Thursday" in clean(body)


# --- boilerplate ------------------------------------------------------------


def test_confidentiality_notice_and_unsubscribe() -> None:
    body = (
        "Invoice attached.\n\n"
        "This email and any attachments are confidential and intended solely for "
        "the addressee.\n"
        "Unsubscribe from these emails at any time."
    )
    assert clean(body) == "Invoice attached."


# --- normalisation ----------------------------------------------------------


def test_zero_width_characters_do_not_change_the_text() -> None:
    """Marketing mail is full of them, and they would defeat content-hash dedupe."""
    assert (
        normalise_whitespace("Meet\N{ZERO WIDTH SPACE}ing at\N{NO-BREAK SPACE}3") == "Meeting at 3"
    )


def test_blank_runs_collapse() -> None:
    assert normalise_whitespace("a\n\n\n\n\nb") == "a\n\nb"


def test_table_indentation_is_dropped() -> None:
    """Flattened HTML tables indent every value by twenty spaces of nothing."""
    assert normalise_whitespace("Order #\n                    ORD-123") == "Order #\nORD-123"


def test_unmonitored_inbox_footer() -> None:
    body = "We have received your application.\n\nPlease do not reply to this email."
    assert clean(body) == "We have received your application."


# --- order ------------------------------------------------------------------


def test_quotes_are_stripped_before_signatures() -> None:
    """A signature inside a quoted block must vanish with the quote, not survive it."""
    body = "Works for me.\n\nOn Mon, someone wrote:\n> Proposing Thursday\n> --\n> Their Name\n"
    assert strip_quoted(body).strip() == "Works for me."
