from __future__ import annotations

from datetime import UTC, datetime

from app.contracts import EmailMessage
from app.eval.anonymize import Anonymizer


def _message(**overrides: object) -> EmailMessage:
    base: dict[str, object] = {
        "id": "18c0f2a",
        "thread_id": "18c0f2a",
        "subject": "Lunch with Sara Ahmed",
        "body_text": "Hi, ping me at sara.ahmed@realcorp.co.uk or +92 300 1234567.",
        "sender": "sara.ahmed@realcorp.co.uk",
        "recipients": ["me@realmail.com"],
        "received_at": datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
    }
    return EmailMessage.model_validate(base | overrides)


def test_same_address_maps_to_same_pseudonym() -> None:
    anonymizer = Anonymizer()
    first = anonymizer.map_email("sara.ahmed@realcorp.co.uk")
    second = anonymizer.map_email("SARA.AHMED@RealCorp.co.uk")
    assert first == second


def test_different_addresses_map_differently() -> None:
    anonymizer = Anonymizer()
    assert anonymizer.map_email("a@x.com") != anonymizer.map_email("b@x.com")


def test_mapping_survives_a_round_trip() -> None:
    first = Anonymizer()
    original = first.map_email("sara.ahmed@realcorp.co.uk")

    resumed = Anonymizer(first.mapping)
    assert resumed.map_email("sara.ahmed@realcorp.co.uk") == original


def test_body_loses_real_addresses() -> None:
    anonymizer = Anonymizer()
    scrubbed = anonymizer.scrub("write to sara.ahmed@realcorp.co.uk today")
    assert "realcorp" not in scrubbed
    assert "@example." in scrubbed


def test_phone_numbers_are_removed() -> None:
    scrubbed = Anonymizer().scrub("call +92 300 1234567 when you land")
    assert "1234567" not in scrubbed


def test_urls_are_replaced() -> None:
    scrubbed = Anonymizer().scrub("see https://internal.realcorp.co.uk/deck?id=99")
    assert "realcorp" not in scrubbed


def test_registered_names_are_replaced_in_body() -> None:
    anonymizer = Anonymizer()
    anonymizer.map_name("Sara Ahmed")
    assert "Sara Ahmed" not in anonymizer.scrub("Lunch with Sara Ahmed on Friday")


def test_longer_names_replaced_before_shorter_ones() -> None:
    """'Sara' must not eat the 'Sara' inside 'Sara Ahmed' and strand 'Ahmed'."""
    anonymizer = Anonymizer()
    anonymizer.map_name("Sara")
    anonymizer.map_name("Sara Ahmed")
    assert "Ahmed" not in anonymizer.scrub("Sara Ahmed will join")


def test_anonymize_message_scrubs_every_surface() -> None:
    anonymizer = Anonymizer()
    anonymizer.map_name("Sara Ahmed")

    scrubbed = anonymizer.anonymize(_message())
    blob = scrubbed.model_dump_json()

    for leak in ("realcorp", "realmail", "Sara Ahmed", "1234567", "18c0f2a"):
        assert leak not in blob


def test_timestamps_are_preserved() -> None:
    original = _message()
    assert Anonymizer().anonymize(original).received_at == original.received_at


def test_sender_and_recipients_stay_consistent_with_body() -> None:
    anonymizer = Anonymizer()
    scrubbed = anonymizer.anonymize(_message())
    assert scrubbed.sender in scrubbed.body_text
