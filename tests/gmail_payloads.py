"""Gmail `users.messages.get` responses built from `data/injection/` cases.

A case gives its message as `body` (one `text/plain` part) or as `message`, a
MIME tree; see that folder's README. The response has Gmail's shape --
`mimeType`, `filename`, `headers` and a base64url `body.data` on each part --
so it goes through the same preparation a real message does.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "data" / "injection"


def load_cases(group: str | None = None) -> list[dict[str, Any]]:
    cases = [json.loads(path.read_text("utf-8")) for path in sorted(FIXTURES_DIR.glob("*.json"))]
    return [case for case in cases if group is None or case["group"] == group]


def gmail_response(case: dict[str, Any]) -> dict[str, Any]:
    """The `users.messages.get` response for a case, with `format=full`."""
    tree = case.get("message") or {"mime": "text/plain", "text": case["body"]}
    payload = _part(tree)
    payload["headers"] = [
        {"name": "From", "value": "Sender <sender@example.com>"},
        {"name": "To", "value": "owner@example.com"},
        {"name": "Subject", "value": case["subject"]},
        *payload["headers"],
    ]
    return {
        "id": case["id"],
        "threadId": case["id"],
        "labelIds": ["INBOX", "UNREAD"],
        "internalDate": "1791000000000",
        "payload": payload,
    }


def _part(node: dict[str, Any]) -> dict[str, Any]:
    mime = node["mime"]
    headers = [{"name": "Content-Type", "value": f'{mime}; charset="UTF-8"'}]
    if "disposition" in node:
        filename = node.get("filename", "")
        headers.append(
            {
                "name": "Content-Disposition",
                "value": f'{node["disposition"]}; filename="{filename}"',
            }
        )
    part: dict[str, Any] = {
        "mimeType": mime,
        "filename": node.get("filename", ""),
        "headers": headers,
    }
    if "parts" in node:
        part["body"] = {"size": 0}
        part["parts"] = [_part(child) for child in node["parts"]]
    else:
        raw = node["text"].encode("utf-8")
        part["body"] = {"size": len(raw), "data": base64.urlsafe_b64encode(raw).decode("ascii")}
    return part
