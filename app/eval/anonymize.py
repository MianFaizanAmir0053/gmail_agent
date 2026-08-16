"""Turn real email into committable fixtures.

Real messages carry other people's names, addresses, and business detail, so
they cannot go in a public repo. Scrubbing must be **consistent**: the same real
person maps to the same fake person in every fixture, or threading and
attendee-matching cases stop making sense once anonymised.

The mapping file is itself sensitive -- it is a real-identity lookup table --
so it lives in `data/` and is gitignored alongside the raw mail.

    python -m app.eval.anonymize            # data/raw_emails -> data/fixtures_draft
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Final

from app.contracts import EmailMessage

RAW_DIR = Path("data/raw_emails")
DRAFT_DIR = Path("data/fixtures_draft")
MAP_PATH = Path("data/anonymization-map.json")

_FIRST_NAMES: Final = (
    "sara",
    "bilal",
    "ayesha",
    "hina",
    "dana",
    "omar",
    "nadia",
    "yusuf",
    "farah",
    "imran",
    "zara",
    "tariq",
    "mona",
    "kabir",
    "leila",
    "sami",
)
_DOMAINS: Final = ("example.com", "example.org", "example.net")

_EMAIL_RE: Final = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE: Final = re.compile(r"(?<!\w)(\+?\d[\d\s().-]{7,}\d)(?!\w)")
_URL_RE: Final = re.compile(r"https?://[^\s>)\]]+")


class Anonymizer:
    """Allocates stable pseudonyms and rewrites text to use them."""

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self._map: dict[str, str] = dict(mapping or {})

    @property
    def mapping(self) -> dict[str, str]:
        return dict(self._map)

    # --- allocation -------------------------------------------------------

    def map_email(self, address: str) -> str:
        key = address.strip().lower()
        if not key:
            return ""
        if key not in self._map:
            index = sum(1 for k in self._map if "@" in k)
            local = _FIRST_NAMES[index % len(_FIRST_NAMES)]
            suffix = index // len(_FIRST_NAMES)
            local = f"{local}{suffix}" if suffix else local
            self._map[key] = f"{local}@{_DOMAINS[index % len(_DOMAINS)]}"
        return self._map[key]

    def map_name(self, name: str) -> str:
        key = name.strip()
        if not key:
            return ""
        if key.lower() not in self._map:
            index = sum(1 for k in self._map if "@" not in k)
            self._map[key.lower()] = _FIRST_NAMES[index % len(_FIRST_NAMES)].capitalize()
        return self._map[key.lower()]

    # --- rewriting --------------------------------------------------------

    def scrub(self, text: str) -> str:
        """Replace addresses, phone numbers, URLs, and known names.

        Addresses are mapped first so that a name appearing inside an address
        is not partially rewritten. Known names are then replaced longest-first,
        so "Sara Ahmed" is handled before "Sara".
        """
        text = _EMAIL_RE.sub(lambda m: self.map_email(m.group(0)), text)
        text = _PHONE_RE.sub("+00 000 0000000", text)
        text = _URL_RE.sub("https://example.com/link", text)

        names = sorted((k for k in self._map if "@" not in k), key=len, reverse=True)
        for name in names:
            text = re.sub(re.escape(name), self._map[name], text, flags=re.IGNORECASE)
        return text

    def anonymize(self, message: EmailMessage) -> EmailMessage:
        return EmailMessage(
            id=self._map_id(message.id),
            thread_id=self._map_id(message.thread_id),
            subject=self.scrub(message.subject),
            body_text=self.scrub(message.body_text),
            sender=self.map_email(message.sender),
            recipients=[self.map_email(r) for r in message.recipients],
            received_at=message.received_at,
        )

    def _map_id(self, value: str) -> str:
        key = f"id:{value}"
        if key not in self._map:
            self._map[key] = f"fx{sum(1 for k in self._map if k.startswith('id:')) + 1:03d}"
        return self._map[key]


def load_mapping(path: Path = MAP_PATH) -> dict[str, str]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def save_mapping(mapping: dict[str, str], path: Path = MAP_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    if not RAW_DIR.exists():
        raise SystemExit(f"No raw email at {RAW_DIR.resolve()}. Run app.eval.fetch first.")

    anonymizer = Anonymizer(load_mapping())
    DRAFT_DIR.mkdir(parents=True, exist_ok=True)

    written = 0
    for path in sorted(RAW_DIR.glob("*.json")):
        original = EmailMessage.model_validate_json(path.read_text(encoding="utf-8"))
        scrubbed = anonymizer.anonymize(original)
        out = DRAFT_DIR / f"{scrubbed.id}.json"
        out.write_text(
            json.dumps(scrubbed.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        written += 1

    save_mapping(anonymizer.mapping)
    print(f"Anonymised {written} messages into {DRAFT_DIR}/")
    print("Now label each by hand and move it into data/fixtures/ as a full fixture.")
    print(f"Mapping written to {MAP_PATH} -- gitignored, contains real identities.")


if __name__ == "__main__":
    main()
