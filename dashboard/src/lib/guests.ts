/**
 * Guests outside the thread (M17, D4), and where each guest came from (M18,
 * D5), as the card shows them.
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

/** Where a guest came from (M18, D5), as `Source` in `app/policy/participants.py`. */
export type Source = "thread" | "allowed" | "email" | "quoted" | "absent";

/** What the card says of each source, in the words of `SOURCE_WORDS` in
 * `app/policy/participants.py`. */
export const SOURCE_WORDS: Record<Source, string> = {
  thread: "in the thread",
  allowed: "an allowed contact",
  email: "named in the email",
  quoted: "named in a quoted or forwarded section",
  absent: "not found in the email",
};

/** The two marked as warnings, beside Allow: the email did not write the
 * address itself, or the model made it up. */
export const WARNINGS: ReadonlySet<Source> = new Set<Source>(["quoted", "absent"]);

/** Noted on a card whose email quotes or forwards older mail, as
 * `QUOTED_SECTION` in `app/telegram/cards.py`: a time's or a place's source
 * is not traced. */
export const QUOTED_SECTION =
  "This email quotes or forwards older mail. A time or place taken from that part is not marked.";

const NAMED: ReadonlySet<string> = new Set(["email", "quoted", "absent"]);

/**
 * A guest's source as the card shows it, as `card_source` in
 * `app/policy/participants.py` decides it: in the thread while no check has
 * found them outside it; else an allowed contact; else where the email named
 * them. Null for a payload parked before M18, which records no sources.
 */
export function cardSource(
  guest: string,
  card: { guest_sources?: unknown; thread_guests?: unknown; outside_guests?: unknown },
  allowed: ReadonlySet<string>,
): Source | null {
  const sources = card.guest_sources;
  if (typeof sources !== "object" || sources === null || Array.isArray(sources)) return null;
  const where: unknown = Object.hasOwn(sources, guest)
    ? (sources as Record<string, unknown>)[guest]
    : null;
  if (typeof where !== "string" || !NAMED.has(where)) return null;
  const key = guestKey(guest);
  if (keys(card.thread_guests).has(key) && !keys(card.outside_guests).has(key)) return "thread";
  if (allowed.has(key)) return "allowed";
  return where as Source;
}

/** The strings in a payload's list, and nothing else: a payload is data. */
export function words(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function keys(value: unknown): Set<string> {
  return new Set(words(value).map(guestKey));
}
