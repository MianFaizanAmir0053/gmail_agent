/**
 * Guests outside the thread (M17, D4), as the card shows them.
 *
 * No framework imports, so `node --test` runs these directly. Fly still
 * decides: a Confirm with a guest nobody allowed is refused there.
 */

/** The longest address the mail standards allow, as `app/policy/contacts.py`. */
export const MAX_ADDRESS_CHARS = 320;

/** What is trimmed before an address is compared: ASCII space and a byte-order
 * mark, the set `SPACE` in `app/policy/participants.py` names. */
const EDGES = /^[ \t\r\n\f\v﻿]+|[ \t\r\n\f\v﻿]+$/g;

function trimmed(address: string): string {
  return address.replace(EDGES, "");
}

/**
 * One spelling per mailbox, as `guest_key` in `app/policy/participants.py`:
 * lower case and exact, except that Gmail ignores dots and `+tags` and reads
 * `googlemail.com` as `gmail.com`.
 */
export function guestKey(address: string): string {
  const lower = trimmed(address).toLowerCase();
  const at = lower.indexOf("@");
  const local = at < 0 ? lower : lower.slice(0, at);
  const domain = at < 0 ? "" : lower.slice(at + 1);
  if (domain === "gmail.com" || domain === "googlemail.com") {
    return `${(local.split("+", 1)[0] ?? "").replaceAll(".", "")}@gmail.com`;
  }
  return `${local}@${domain}`;
}

/** Shaped like one address, as `app/policy/contacts.py` checks it: printable
 * ASCII, no space, none of `<>,;`. */
export function isAddress(value: string): boolean {
  const address = trimmed(value);
  const at = address.indexOf("@");
  if (at <= 0 || address.length > MAX_ADDRESS_CHARS) return false;
  const domain = address.slice(at + 1);
  return domain.includes(".") && /^[!-~]+$/.test(address) && !/[<>,;]/.test(address);
}
