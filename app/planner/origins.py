"""Where each value came from (M19, D4), and what the owner typed against
what they pasted (D5).

Origins, from most trusted to least:
- **typed:** the owner's typed request, and what the planner wrote from it;
- **calendar:** the owner's calendar's times;
- **contact:** an address among the confirmed contacts;
- **mail:** anything from mail, or from text the owner pasted.

A value an operation computes is as untrusted as the least trusted of its
inputs (`computed`).

The browser marks what was pasted (D5), so typed and pasted are never guessed
from the text. The planner reads the typed request only. Addresses are found
in either with the scrubber's own pattern (`scrub.ADDRESS`), so an address the
scrubber keeps is always found, and are compared as mailboxes (`guest_key`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import IntEnum

from app.policy.participants import guest_key
from app.policy.scrub import ADDRESS

_ADDRESS = re.compile(ADDRESS)


class Origin(IntEnum):
    """Ordered from most trusted to least."""

    TYPED = 0
    CALENDAR = 1
    CONTACT = 2
    MAIL = 3


def computed(*inputs: Origin) -> Origin:
    """The origin of a value computed from `inputs`: the least trusted of them."""
    return max(inputs)


@dataclass(frozen=True, slots=True)
class PastedText:
    """The text the owner pasted, as the reader is handed it. Its origin is mail."""

    text: str


def typed_addresses(typed: str, owner: Iterable[str]) -> frozenset[str]:
    """The guest keys of the addresses in the owner's typed request, less the
    owner's own. A guest is "named by you" only when it is one of these (D5)."""
    mine = {guest_key(address) for address in owner}
    return frozenset(guest_key(found) for found in _ADDRESS.findall(typed)) - mine


def pasted_addresses(pasted: str, *, exclude: Iterable[str]) -> tuple[str, ...]:
    """The addresses in the pasted text, lower-cased and first seen first, less
    any in `exclude`. Each is offered for an Allow, and never placed in a plan
    (D5)."""
    left_out = {guest_key(address) for address in exclude}
    found: dict[str, str] = {}
    for address in _ADDRESS.findall(pasted):
        key = guest_key(address)
        if key not in left_out:
            found.setdefault(key, address.lower())
    return tuple(found.values())
