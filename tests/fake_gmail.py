"""A fake Gmail mailbox, shaped like the discovery client, for the mail sync's
tests (M20).

It keeps messages and the history records their changes leave, answers
`history.list` after a cursor, lists by the few search terms the sync uses --
an unknown term fails the test rather than matching everything -- and fetches
messages with a subject, a snippet and a body the sync must never keep.
Failures are scripted per call. Nothing here touches the network.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from googleapiclient.errors import HttpError

SECRET = "482913"
"""In every fetched message's subject, snippet and body."""


def http_error(status: int, reason: str = "") -> HttpError:
    errors = [{"reason": reason, "domain": "global"}] if reason else []
    content = json.dumps({"error": {"code": status, "message": "x", "errors": errors}})
    return HttpError(SimpleNamespace(status=status, reason="x"), content.encode())


@dataclass
class FakeMessage:
    id: str
    thread_id: str
    labels: set[str]
    internal_at: datetime
    headers: dict[str, str]


_IN = {"chats": "CHAT", "drafts": "DRAFT", "sent": "SENT", "inbox": "INBOX"}


def _matches(message: FakeMessage, terms: list[str]) -> bool:
    if message.labels & {"SPAM", "TRASH"}:  # Gmail's default: never listed
        return False
    seconds = message.internal_at.timestamp()
    for term in terms:
        negate = term.startswith("-")
        key, _, value = term.lstrip("-").partition(":")
        if key == "after":
            found = seconds > int(value)
        elif key == "before":
            found = seconds < int(value)
        elif key == "is" and value == "unread":
            found = "UNREAD" in message.labels
        elif key == "category" and value != "primary":
            found = f"CATEGORY_{value.upper()}" in message.labels
        elif key == "in" and value in _IN:
            found = _IN[value] in message.labels
        else:
            raise AssertionError(f"the fake mailbox does not understand {term!r}")
        if found == negate:
            return False
    return True


@dataclass
class _Request:
    run: Any

    def execute(self) -> Any:
        return self.run()


@dataclass
class FakeMailbox:
    address: str = "me@example.com"
    messages: dict[str, FakeMessage] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    history_id: int = 1000
    expired_below: int = 0
    """A cursor below this answers 404, as Gmail does after about a week."""

    page_size: int = 500
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    failures: dict[str, list[BaseException | None]] = field(
        default_factory=lambda: defaultdict(list)
    )

    # --- the mailbox changing -------------------------------------------------

    def put(
        self,
        message_id: str,
        *,
        labels: set[str],
        at: datetime,
        sender: str = "Sara <sara@example.com>",
        to: str | None = None,
        thread_id: str | None = None,
        **headers: str,
    ) -> FakeMessage:
        """A message already in the mailbox: no history record."""
        named = {"From": sender, "To": to or self.address, "Subject": f"Code {SECRET}"}
        named |= {name.replace("_", "-"): value for name, value in headers.items()}
        message = FakeMessage(message_id, thread_id or f"t-{message_id}", set(labels), at, named)
        self.messages[message_id] = message
        return message

    def deliver(self, message_id: str, **kwargs: Any) -> FakeMessage:
        """A message arriving (or being sent): a `messageAdded` record."""
        message = self.put(message_id, **kwargs)
        self._record("messagesAdded", message)
        return message

    def relabel(
        self, message_id: str, *, add: set[str] | None = None, remove: set[str] | None = None
    ) -> None:
        message = self.messages[message_id]
        if add:
            message.labels |= add
            self._record("labelsAdded", message, sorted(add))
        if remove:
            message.labels -= remove
            self._record("labelsRemoved", message, sorted(remove))

    def delete(self, message_id: str) -> None:
        message = self.messages.pop(message_id)
        self._record("messagesDeleted", message)

    def expire(self) -> None:
        """Gmail stops keeping the history up to now."""
        self.expired_below = self.history_id + 1

    def fail(self, key: str, *errors: BaseException | None) -> None:
        """The next calls of `key` -- "history.list", "messages.list",
        "getProfile" or "messages.get:<id>" -- raise these, one each. A None
        lets its call through: `fail("getProfile", None, error)` answers the
        run's own profile read and fails the one after."""
        self.failures[key].extend(errors)

    def _record(self, kind: str, message: FakeMessage, labels: list[str] | None = None) -> None:
        self.history_id += 1
        entry: dict[str, Any] = {"message": {"id": message.id, "threadId": message.thread_id}}
        if labels is not None:
            entry["labelIds"] = labels
        self.history.append({"id": str(self.history_id), kind: [entry]})

    # --- what the sync asked ----------------------------------------------------

    def asked(self, method: str) -> list[dict[str, Any]]:
        return [kwargs for name, kwargs in self.calls if name == method]

    def fetched(self) -> list[str]:
        return [kwargs["id"] for kwargs in self.asked("messages.get")]

    # --- the discovery client's shape -----------------------------------------

    def users(self) -> _Users:
        return _Users(self)

    def _profile(self, **kwargs: Any) -> _Request:
        return self._call(
            "getProfile",
            kwargs,
            lambda: {"emailAddress": self.address, "historyId": str(self.history_id)},
        )

    def _call(self, method: str, kwargs: dict[str, Any], answer: Any, key: str = "") -> _Request:
        self.calls.append((method, kwargs))

        def run() -> Any:
            queued = self.failures.get(key or method)
            error = queued.pop(0) if queued else None
            if error is not None:
                raise error
            return answer()

        return _Request(run)

    def _get(self, **kwargs: Any) -> _Request:
        message_id = kwargs["id"]

        def answer() -> dict[str, Any]:
            message = self.messages.get(message_id)
            if message is None:
                raise http_error(404)
            # A server that ignored the field mask would send all of this.
            return {
                "id": message.id,
                "threadId": message.thread_id,
                "labelIds": sorted(message.labels),
                "internalDate": str(int(message.internal_at.timestamp() * 1000)),
                "snippet": f"Your code is {SECRET}",
                "payload": {
                    "headers": [{"name": k, "value": v} for k, v in message.headers.items()],
                    "body": {"data": SECRET},
                },
            }

        return self._call("messages.get", kwargs, answer, key=f"messages.get:{message_id}")

    def _list(self, **kwargs: Any) -> _Request:
        def answer() -> dict[str, Any]:
            terms = str(kwargs["q"]).split()
            found = sorted(
                (m for m in self.messages.values() if _matches(m, terms)),
                key=lambda m: (m.internal_at, m.id),
                reverse=True,
            )
            start = int(kwargs.get("pageToken") or 0)
            page = found[start : start + self.page_size]
            response: dict[str, Any] = {
                "messages": [{"id": m.id, "threadId": m.thread_id} for m in page]
            }
            if start + self.page_size < len(found):
                response["nextPageToken"] = str(start + self.page_size)
            return response

        return self._call("messages.list", kwargs, answer)

    def _history(self, **kwargs: Any) -> _Request:
        def answer() -> dict[str, Any]:
            after = int(kwargs["startHistoryId"])
            if after < self.expired_below:
                raise http_error(404)
            records = [record for record in self.history if int(record["id"]) > after]
            start = int(kwargs.get("pageToken") or 0)
            page = records[start : start + self.page_size]
            response: dict[str, Any] = {"historyId": str(self.history_id)}
            if page:
                response["history"] = page
            if start + self.page_size < len(records):
                response["nextPageToken"] = str(start + self.page_size)
            return response

        return self._call("history.list", kwargs, answer)


@dataclass
class _Users:
    """`service.users()`. Its resources are named as Gmail's are, which would
    clash with the mailbox's own `messages` and `history` fields."""

    box: FakeMailbox

    def messages(self) -> Any:
        return SimpleNamespace(get=self.box._get, list=self.box._list)

    def history(self) -> Any:
        return SimpleNamespace(list=self.box._history)

    def getProfile(self, **kwargs: Any) -> _Request:  # noqa: N802 -- Google's name
        return self.box._profile(**kwargs)
