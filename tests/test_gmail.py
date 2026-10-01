from __future__ import annotations

import base64
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from googleapiclient.errors import HttpError

from app.google.gmail import (
    HISTORY_TYPES,
    METADATA_HEADERS,
    RETRY_FOR,
    SYNC_FIELDS,
    SYNC_HEADERS,
    CursorExpiredError,
    GmailClient,
    MessageGoneError,
    epoch_window,
    extract_body,
    is_outage,
    to_email_message,
    to_message_meta,
)
from app.mail import quota


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _part(mime: str, text: str) -> dict[str, Any]:
    return {"mimeType": mime, "body": {"data": _b64(text)}}


def test_extracts_simple_plain_text() -> None:
    assert extract_body(_part("text/plain", "Can we meet Tuesday?")) == "Can we meet Tuesday?"


def test_prefers_plain_text_over_html() -> None:
    payload: dict[str, Any] = {
        "mimeType": "multipart/alternative",
        "parts": [_part("text/plain", "plain wins"), _part("text/html", "<p>html loses</p>")],
    }
    assert extract_body(payload) == "plain wins"


def test_falls_back_to_html_when_no_plain_part() -> None:
    payload: dict[str, Any] = {
        "mimeType": "multipart/alternative",
        "parts": [_part("text/html", "<p>Meet at <b>3pm</b></p>")],
    }
    assert extract_body(payload) == "Meet at 3pm"


def test_html_fallback_drops_script_and_style() -> None:
    html = "<style>p{color:red}</style><p>Real text</p><script>alert(1)</script>"
    payload: dict[str, Any] = {"parts": [_part("text/html", html)]}
    body = extract_body(payload)
    assert "Real text" in body
    assert "alert" not in body
    assert "color:red" not in body


def test_numeric_and_named_entities_are_decoded() -> None:
    """A hand-written table covered five named entities and left `&#128206;`
    sitting in the body, where it went on to be embedded verbatim."""
    payload: dict[str, Any] = {
        "parts": [_part("text/html", "<p>&#128206; Attached &amp; signed &#8212; done</p>")]
    }
    assert extract_body(payload) == "\N{PAPERCLIP} Attached & signed \N{EM DASH} done"


def test_finds_deeply_nested_parts() -> None:
    payload: dict[str, Any] = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "application/pdf", "body": {"attachmentId": "x"}},
            {"mimeType": "multipart/alternative", "parts": [_part("text/plain", "buried")]},
        ],
    }
    assert extract_body(payload) == "buried"


def test_attachment_only_message_yields_empty_body() -> None:
    payload: dict[str, Any] = {
        "mimeType": "multipart/mixed",
        "parts": [{"mimeType": "image/png", "body": {"attachmentId": "x"}}],
    }
    assert extract_body(payload) == ""


def test_malformed_base64_does_not_raise() -> None:
    payload: dict[str, Any] = {"mimeType": "text/plain", "body": {"data": "!!!not-base64!!!"}}
    assert extract_body(payload) == ""


def test_undecodable_bytes_are_replaced_not_fatal() -> None:
    raw = base64.urlsafe_b64encode(b"caf\xe9 meeting").decode()  # latin-1 in a utf-8 world
    payload: dict[str, Any] = {"mimeType": "text/plain", "body": {"data": raw}}
    assert "meeting" in extract_body(payload)


def test_to_email_message_maps_headers_and_time() -> None:
    message: dict[str, Any] = {
        "id": "18c0f2a",
        "threadId": "18c0f2a",
        "internalDate": "1786000000000",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": "Sync Tuesday?"},
                {"name": "From", "value": "Sara Ahmed <Sara@Example.com>"},
                {"name": "To", "value": "me@example.com"},
                {"name": "Cc", "value": "Bilal <bilal@example.com>, ops@example.com"},
            ],
            "body": {"data": _b64("Can we meet next Tuesday at 3?")},
        },
    }

    email = to_email_message(message)

    assert email.id == "18c0f2a"
    assert email.subject == "Sync Tuesday?"
    assert email.sender == "sara@example.com"  # normalised to lowercase
    assert email.recipients == ["me@example.com", "bilal@example.com", "ops@example.com"]
    assert email.body_text == "Can we meet next Tuesday at 3?"
    assert email.received_at.tzinfo is not None
    assert email.received_at == datetime.fromtimestamp(1786000000, tz=UTC)


def test_missing_headers_do_not_raise() -> None:
    message: dict[str, Any] = {
        "id": "a",
        "threadId": "a",
        "internalDate": "0",
        "payload": {"mimeType": "text/plain", "body": {"data": _b64("hi")}},
    }
    email = to_email_message(message)
    assert email.subject == ""
    assert email.sender == ""
    assert email.recipients == []


# --- metadata-only reads (M15) ---------------------------------------------


@dataclass
class _Request:
    result: dict[str, Any]

    def execute(self) -> dict[str, Any]:
        return self.result


@dataclass
class _Threads:
    pages: list[dict[str, Any]]
    thread: dict[str, Any]
    list_calls: list[dict[str, Any]] = field(default_factory=list)
    get_calls: list[dict[str, Any]] = field(default_factory=list)

    def list(self, **kwargs: Any) -> _Request:
        self.list_calls.append(kwargs)
        return _Request(self.pages[len(self.list_calls) - 1])

    def get(self, **kwargs: Any) -> _Request:
        self.get_calls.append(kwargs)
        return _Request(self.thread)


@dataclass
class _Users:
    threads_api: _Threads
    profile: dict[str, Any]

    def threads(self) -> _Threads:
        return self.threads_api

    def getProfile(self, **kwargs: Any) -> _Request:  # noqa: N802 -- Google's name
        return _Request(self.profile)


@dataclass
class _Service:
    users_api: _Users

    def users(self) -> _Users:
        return self.users_api


def _client(
    pages: list[dict[str, Any]] | None = None, thread: dict[str, Any] | None = None
) -> tuple[GmailClient, _Threads]:
    threads = _Threads(pages=pages or [{}], thread=thread or {"messages": []})
    service = _Service(_Users(threads, {"emailAddress": "Me.Owner@Gmail.com", "historyId": "1"}))
    return GmailClient(service), threads


def test_thread_listing_follows_every_page() -> None:
    """A capped walk would quietly undercount a busy fortnight."""
    client, threads = _client(
        pages=[
            {"threads": [{"id": "t1"}, {"id": "t2"}], "nextPageToken": "p2"},
            {"threads": [{"id": "t3"}], "nextPageToken": "p3"},
            {"threads": [{"id": "t4"}]},
        ]
    )

    assert client.thread_ids("after:1 before:2") == ["t1", "t2", "t3", "t4"]
    assert [call.get("pageToken") for call in threads.list_calls] == [None, "p2", "p3"]


def test_thread_metadata_never_asks_for_bodies() -> None:
    client, threads = _client(thread={"messages": []})

    client.thread_metadata("t1")

    assert threads.get_calls[0]["format"] == "metadata"
    assert list(threads.get_calls[0]["metadataHeaders"]) == list(METADATA_HEADERS)


def test_message_meta_keeps_only_the_named_headers() -> None:
    message = {
        "id": "m1",
        "threadId": "t1",
        "labelIds": ["INBOX", "CATEGORY_PERSONAL"],
        "internalDate": str(int(datetime(2026, 10, 6, 9, 30, tzinfo=UTC).timestamp() * 1000)),
        "payload": {
            "headers": [
                {"name": "From", "value": "Sara <sara@example.com>"},
                {"name": "DKIM-Signature", "value": "v=1; a=rsa-sha256"},
                {"name": "Subject", "value": "Contract"},
                {"name": "Auto-Submitted", "value": "no"},
            ]
        },
    }

    meta = to_message_meta(message)

    assert meta.internal_date == datetime(2026, 10, 6, 9, 30, tzinfo=UTC)
    assert meta.label_ids == frozenset({"INBOX", "CATEGORY_PERSONAL"})
    assert dict(meta.headers) == {
        "From": "Sara <sara@example.com>",
        "Subject": "Contract",
        "Auto-Submitted": "no",
    }


def test_the_profile_address_is_normalised_to_lower_case() -> None:
    client, _ = _client()
    assert client.profile_address() == "me.owner@gmail.com"


# --- calls for the mail sync (M20) -------------------------------------------


def _http_error(status: int, reason: str = "") -> HttpError:
    errors = [{"reason": reason, "domain": "usageLimits"}] if reason else []
    content = json.dumps({"error": {"code": status, "message": "x", "errors": errors}})
    return HttpError(SimpleNamespace(status=status, reason="x"), content.encode())


@dataclass
class _Scripted:
    """A request that replays its outcomes, one per attempt: a dict is
    returned, an exception raised. The last outcome repeats."""

    outcomes: list[Any]
    attempts: int = 0

    def execute(self) -> Any:
        outcome = self.outcomes[min(self.attempts, len(self.outcomes) - 1)]
        self.attempts += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@dataclass
class FakeService:
    """The discovery client's shape, for every method the sync calls. Each
    call is written down; `reply` scripts what its next call sees."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    requests: list[_Scripted] = field(default_factory=list)
    replies: dict[str, list[list[Any]]] = field(default_factory=lambda: defaultdict(list))

    def reply(self, method: str, *outcomes: Any) -> None:
        self.replies[method].append(list(outcomes))

    def request(self, method: str, kwargs: dict[str, Any]) -> _Scripted:
        self.calls.append((method, kwargs))
        queued = self.replies[method]
        request = _Scripted(queued.pop(0) if queued else [{}])
        self.requests.append(request)
        return request

    def users(self) -> _FakeUsers:
        return _FakeUsers(self)


@dataclass
class _FakeUsers:
    service: FakeService

    def messages(self) -> _FakeResource:
        return _FakeResource(self.service, "messages")

    def history(self) -> _FakeResource:
        return _FakeResource(self.service, "history")

    def threads(self) -> _FakeResource:
        return _FakeResource(self.service, "threads")

    def getProfile(self, **kwargs: Any) -> _Scripted:  # noqa: N802 -- Google's name
        return self.service.request("getProfile", kwargs)


@dataclass
class _FakeResource:
    service: FakeService
    name: str

    def get(self, **kwargs: Any) -> _Scripted:
        return self.service.request(f"{self.name}.get", kwargs)

    def list(self, **kwargs: Any) -> _Scripted:
        return self.service.request(f"{self.name}.list", kwargs)


@dataclass
class RecordingPacer:
    spent: list[tuple[str, str | None]] = field(default_factory=list)

    def spend(self, method: str, *, share: str | None = None) -> None:
        self.spent.append((method, share))


@dataclass
class FakeClock:
    now: float = 0.0
    slept: list[float] = field(default_factory=list)

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _sync_client(
    service: Any, pacer: RecordingPacer | None = None, clock: FakeClock | None = None
) -> GmailClient:
    clock = clock or FakeClock()
    return GmailClient(
        service, pacer=pacer or RecordingPacer(), share="sync", sleep=clock.sleep, clock=clock
    )


SEEDED: dict[str, Any] = {
    "id": "m1",
    "threadId": "t1",
    "labelIds": ["INBOX", "UNREAD", "CATEGORY_PERSONAL"],
    "internalDate": str(int(datetime(2026, 10, 1, 9, 0, tzinfo=UTC).timestamp() * 1000)),
    # What a server that ignored the field mask might still send.
    "snippet": "Your code is 482913",
    "payload": {
        "headers": [
            {"name": "From", "value": "Sara <Sara@Example.com>"},
            {"name": "To", "value": "me@example.com"},
            {"name": "Subject", "value": "Your code is 482913"},
            {"name": "List-Unsubscribe", "value": "<https://example.com/u?id=1>"},
            {"name": "Precedence", "value": "list"},
        ],
        "body": {"data": _b64("Your code is 482913")},
    },
}


def test_metadata_fetches_ask_for_the_field_mask_and_exactly_six_headers() -> None:
    service = FakeService()
    service.reply("messages.get", SEEDED)

    _sync_client(service).message_metadata("m1")

    method, kwargs = service.calls[0]
    assert method == "messages.get"
    assert kwargs["format"] == "metadata"
    assert kwargs["fields"] == "id,threadId,labelIds,internalDate,payload/headers"
    assert list(kwargs["metadataHeaders"]) == [
        "From",
        "To",
        "Cc",
        "List-Unsubscribe",
        "Auto-Submitted",
        "Precedence",
    ]
    assert "Subject" not in SYNC_HEADERS
    assert kwargs["fields"] == SYNC_FIELDS


def test_a_seeded_subject_snippet_and_body_never_leave_the_client() -> None:
    service = FakeService()
    service.reply("messages.get", SEEDED)

    meta = _sync_client(service).message_metadata("m1")

    assert "Subject" not in meta.headers
    assert "482913" not in repr(meta)
    assert meta.headers["List-Unsubscribe"].startswith("<https://")
    assert meta.label_ids == frozenset({"INBOX", "UNREAD", "CATEGORY_PERSONAL"})


@pytest.mark.parametrize("fetch", ["message_metadata", "get_message"])
def test_a_fetch_404_is_message_gone_and_is_not_retried(fetch: str) -> None:
    service = FakeService()
    service.reply("messages.get", _http_error(404))

    with pytest.raises(MessageGoneError):
        getattr(_sync_client(service), fetch)("m1")

    assert service.requests[0].attempts == 1


def test_message_gone_is_a_lookup_error_so_no_retry_policy_retries_it() -> None:
    """LangGraph's default rule never retries a LookupError either."""
    assert issubclass(MessageGoneError, LookupError)


def test_a_history_404_is_an_expired_cursor() -> None:
    service = FakeService()
    service.reply("history.list", _http_error(404))

    with pytest.raises(CursorExpiredError):
        _sync_client(service).history_page("1234")


def test_history_is_read_after_the_cursor_with_the_four_record_types() -> None:
    service = FakeService()
    service.reply(
        "history.list",
        {
            "history": [
                {
                    "id": "1240",
                    "messagesAdded": [{"message": {"id": "m1", "threadId": "t1"}}],
                },
                {
                    "id": "1241",
                    "labelsAdded": [
                        {"message": {"id": "m2", "threadId": "t2"}, "labelIds": ["TRASH"]}
                    ],
                    "labelsRemoved": [
                        {"message": {"id": "m2", "threadId": "t2"}, "labelIds": ["INBOX"]}
                    ],
                },
                {"id": "1242", "messagesDeleted": [{"message": {"id": "m3", "threadId": "t3"}}]},
            ],
            "nextPageToken": "next",
            "historyId": "1300",
        },
    )

    page = _sync_client(service).history_page("1234", "token")

    _, kwargs = service.calls[0]
    assert kwargs["startHistoryId"] == "1234"
    assert kwargs["pageToken"] == "token"
    assert list(kwargs["historyTypes"]) == list(HISTORY_TYPES)
    assert set(HISTORY_TYPES) == {"messageAdded", "messageDeleted", "labelAdded", "labelRemoved"}
    assert [record.id for record in page.records] == ["1240", "1241", "1242"]
    assert page.next_page_token == "next"
    assert page.history_id == "1300"
    (added,) = page.records[0].changes
    assert (added.kind, added.message_id, added.thread_id) == ("added", "m1", "t1")
    labels = page.records[1].changes
    assert [(c.kind, c.label_ids) for c in labels] == [
        ("labels_added", frozenset({"TRASH"})),
        ("labels_removed", frozenset({"INBOX"})),
    ]
    assert page.records[2].changes[0].kind == "deleted"


def test_an_empty_history_page_still_carries_the_mailboxs_id() -> None:
    service = FakeService()
    service.reply("history.list", {"historyId": "1300"})

    page = _sync_client(service).history_page("1300")

    assert page.records == ()
    assert page.next_page_token is None
    assert page.history_id == "1300"


def test_windows_are_epoch_seconds_never_dates() -> None:
    """Gmail reads `after:2026/10/01` as Pacific midnight. A second's overlap
    at each end costs nothing; a second's gap could lose a message."""
    start = datetime(2026, 9, 30, 0, 0, 30, tzinfo=UTC)
    end = datetime(2026, 10, 1, 0, 0, 30, 500000, tzinfo=UTC)

    window = epoch_window(start, end)

    assert window == f"after:{int(start.timestamp()) - 1} before:{int(end.timestamp()) + 1}"
    assert "/" not in window


def test_listing_a_window_follows_every_page() -> None:
    service = FakeService()
    service.reply("messages.list", {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "p2"})
    service.reply("messages.list", {"messages": [{"id": "c"}]})
    start = datetime(2026, 9, 30, tzinfo=UTC)
    end = datetime(2026, 10, 1, tzinfo=UTC)

    ids = _sync_client(service).message_ids("-in:chats", after=start, before=end)

    assert ids == ["a", "b", "c"]
    queries = [kwargs["q"] for _, kwargs in service.calls]
    assert queries == [f"-in:chats {epoch_window(start, end)}"] * 2
    assert [kwargs.get("pageToken") for _, kwargs in service.calls] == [None, "p2"]


def test_the_profile_gives_the_address_and_the_history_id_in_one_call() -> None:
    service = FakeService()
    service.reply("getProfile", {"emailAddress": "Me@Example.com", "historyId": "77"})

    profile = _sync_client(service).profile()

    assert (profile.address, profile.history_id) == ("me@example.com", "77")
    assert len(service.calls) == 1


def test_every_call_is_priced_by_method_and_charged_to_the_clients_share() -> None:
    service = FakeService()
    pacer = RecordingPacer()
    client = _sync_client(service, pacer)
    service.reply("getProfile", {"emailAddress": "me@example.com", "historyId": "1"})
    service.reply("history.list", {"historyId": "1"})
    service.reply("messages.get", SEEDED)
    service.reply("messages.list", {"messages": [{"id": "a"}]})
    service.reply("messages.get", SEEDED)
    service.reply("threads.list", {"threads": [{"id": "t1"}]})
    service.reply("threads.get", {"messages": []})

    client.profile()
    client.history_page("1")
    client.message_metadata("m1")
    client.message_ids("in:sent")
    client.get_message("m1")
    client.thread_ids("after:1")
    client.thread_metadata("t1")

    assert pacer.spent == [
        ("getProfile", "sync"),
        ("history.list", "sync"),
        ("messages.get", "sync"),
        ("messages.list", "sync"),
        ("messages.get", "sync"),
        ("threads.list", "sync"),
        ("threads.get", "sync"),
    ]


def test_a_client_built_without_a_pacer_uses_the_shared_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pipeline's client is built without one (`app/graph/runner.py`), and
    its fetches must count against the same minute as the sync's."""
    shared = RecordingPacer()
    monkeypatch.setattr(quota, "PACER", shared)
    service = FakeService()
    service.reply("messages.get", SEEDED)

    GmailClient(service).message_metadata("m1")

    assert shared.spent == [("messages.get", None)]


@pytest.mark.parametrize("status", [429, 500, 503])
def test_rate_limits_and_server_errors_are_retried_for_thirty_seconds_in_all(status: int) -> None:
    """So a message's run stays well inside the platform's kill timeout."""
    service = FakeService()
    service.reply("messages.get", _http_error(status))
    clock = FakeClock()

    with pytest.raises(HttpError):
        _sync_client(service, clock=clock).get_message("m1")

    assert service.requests[0].attempts > 2
    assert sum(clock.slept) <= RETRY_FOR == 30.0


def test_a_rate_limit_that_clears_costs_a_pause_not_a_failure() -> None:
    service = FakeService()
    service.reply("messages.get", _http_error(429), _http_error(503), SEEDED)
    clock = FakeClock()

    meta = _sync_client(service, clock=clock).message_metadata("m1")

    assert meta.id == "m1"
    assert service.requests[0].attempts == 3
    assert 0 < sum(clock.slept) < 10


def test_a_403_rate_limit_is_retried_but_a_403_refusal_is_not() -> None:
    service = FakeService()
    service.reply("messages.get", _http_error(403, "userRateLimitExceeded"), SEEDED)
    service.reply("messages.get", _http_error(403, "insufficientPermissions"))
    client = _sync_client(service)

    assert client.message_metadata("m1").id == "m1"
    with pytest.raises(HttpError):
        client.message_metadata("m2")
    assert service.requests[1].attempts == 1


def test_a_client_error_is_not_retried() -> None:
    service = FakeService()
    service.reply("messages.get", _http_error(400))

    with pytest.raises(HttpError):
        _sync_client(service).message_metadata("bad id")

    assert service.requests[0].attempts == 1


@pytest.mark.parametrize(
    ("error", "outage"),
    [
        (_http_error(500), True),
        (_http_error(429), True),
        (_http_error(403, "rateLimitExceeded"), True),
        (_http_error(401), True),
        (TimeoutError(), True),
        (ConnectionResetError(), True),
        (_http_error(400), False),
        (_http_error(403, "insufficientPermissions"), False),
        (ValueError("bad internalDate"), False),
    ],
)
def test_an_outage_is_told_apart_from_one_messages_failure(error: Exception, outage: bool) -> None:
    """An outage blames no message (D3): it stops the pass and charges no strike."""
    assert is_outage(error) is outage


def test_the_real_client_library_sends_the_mask_and_the_six_headers() -> None:
    """The discovery client itself, from its bundled document: the mask and the
    headers reach the URL, and a 429 is retried by this client."""
    from googleapiclient.discovery import build
    from googleapiclient.http import HttpMockSequence

    @dataclass
    class Recording:
        inner: Any
        uris: list[str] = field(default_factory=list)

        def request(self, uri: str, *args: Any, **kwargs: Any) -> Any:
            self.uris.append(uri)
            return self.inner.request(uri, *args, **kwargs)

    http = Recording(
        HttpMockSequence(
            [
                ({"status": "429"}, b'{"error": {"code": 429, "message": "slow down"}}'),
                ({"status": "200"}, json.dumps(SEEDED).encode()),
            ]
        )
    )
    service = build("gmail", "v1", http=http, static_discovery=True)
    clock = FakeClock()

    meta = _sync_client(service, clock=clock).message_metadata("m1")

    assert meta.id == "m1"
    assert len(http.uris) == 2
    query = parse_qs(urlsplit(http.uris[-1]).query)
    assert query["fields"] == ["id,threadId,labelIds,internalDate,payload/headers"]
    assert query["format"] == ["metadata"]
    assert query["metadataHeaders"] == list(SYNC_HEADERS)
