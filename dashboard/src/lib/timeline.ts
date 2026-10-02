/**
 * What a proposal card shows, worked out apart from React.
 *
 * No framework imports, so `node --test` runs these rules directly. The page
 * and the card only render what `cardView` decides.
 */

import { guestKey } from "./guests.ts";
import { heldNote, NO_SWITCHES, type Switches } from "./switches.ts";

export const LAST_EDITABLE_REVISION = 2;
/** `MAX_REVISIONS` in app/graph/nodes.py: two edits, so none at revision 3. */

export type ProposalStatus = "pending" | "deciding" | "decided" | "failed";

export type CardPayload = {
  title?: string | null;
  start_utc?: string | null;
  end_utc?: string | null;
  timezone?: string | null;
  attendees?: string[] | null;
  location?: string | null;
  conflicts?: string[] | null;
  review_issues?: string[] | null;
  /** Guests not in the email's thread when it parked (M17, D4). */
  outside_guests?: string[] | null;
};

export type ProposalRow = {
  message_id: string;
  revision: number;
  status: ProposalStatus;
  final_status: string | null;
  action_type: string;
  dry_run: boolean;
  /** The keyed hash a Confirm binds (M17, D2); null when nothing can run. */
  args_hash: string | null;
  /** Goes up whenever the proposal returns to the owner (M17, D2). */
  generation: number;
  /** Null once retention has cleared it. */
  payload: CardPayload | null;
  parked_at: string | Date;
  /** The open decision, while one is queued: what Withdraw names (M17, D6). */
  decision_id?: string | null;
  decision_action?: string | null;
  /** The owner asked for it to be withdrawn, and the worker has not yet answered. */
  withdraw_requested?: boolean | null;
  /** The worker declined a withdraw: the decision was already being applied. */
  withdraw_declined?: boolean | null;
};

export type CardView = {
  messageId: string;
  revision: number;
  title: string;
  when: string;
  eventZone: string | null;
  attendees: string[];
  location: string | null;
  conflicts: string[];
  reviewIssues: string[];
  /** Guests outside the thread the owner has not allowed: each needs an Allow
   * before a Confirm is accepted (M17, D4). */
  outsideGuests: string[];
  dryRun: boolean;
  invite: boolean;
  /** A decision is open: the worker is applying it. */
  applying: boolean;
  canDecide: boolean;
  canEdit: boolean;
  /** Only a proposal with something to bind can be confirmed. */
  canConfirm: boolean;
  /** What the card's Confirm carries: what the owner is looking at. */
  token: string | null;
  /** Runs for real: `DRY_RUN` was off when it was made. */
  live: boolean;
  /** The queued decision, which Withdraw names. */
  decisionId: string | null;
  /** A queued decision nothing has yet been asked about (M17, D6). */
  canWithdraw: boolean;
  withdrawing: boolean;
  withdrawDeclined: boolean;
  /** Why the queued decision waits, when something holds it. */
  held: string | null;
};

/**
 * What one card shows. Total: whatever the stored payload holds, it returns a
 * view rather than throwing, because one odd row must not take the whole
 * timeline down with it. `allowed` holds the guest keys of contacts the owner
 * has allowed: those are no longer outside.
 */
export function cardView(
  row: ProposalRow,
  ownerZone: string,
  allowed: ReadonlySet<string> = new Set(),
  switches: Switches = NO_SWITCHES,
): CardView {
  const card = row.payload;
  // Deciding needs the content: the owner would otherwise approve something
  // they cannot see. Retention never clears an open proposal, so this holds
  // only for a row nobody expected.
  const pending = row.status === "pending" && card !== null;
  const token = row.args_hash ? cardToken(row.args_hash, row.dry_run, row.generation) : null;
  const applying = row.status === "deciding";
  // A sweep is the operator's, never the owner's tap: nothing to withdraw.
  const decisionId =
    applying && row.decision_id && row.decision_action !== "sweep" ? row.decision_id : null;
  const withdrawing = decisionId !== null && row.withdraw_requested === true;
  const withdrawDeclined = decisionId !== null && row.withdraw_declined === true;
  return {
    messageId: row.message_id,
    revision: row.revision,
    title: card === null ? "(cleared)" : text(card.title) || "(untitled)",
    when: formatWindow(text(card?.start_utc), text(card?.end_utc), ownerZone),
    eventZone: text(card?.timezone),
    attendees: words(card?.attendees),
    location: text(card?.location),
    conflicts: words(card?.conflicts),
    reviewIssues: words(card?.review_issues),
    outsideGuests: words(card?.outside_guests).filter((guest) => !allowed.has(guestKey(guest))),
    dryRun: row.dry_run,
    invite: row.action_type === "calendar_invite",
    applying,
    canDecide: pending,
    canEdit: pending && row.revision <= LAST_EDITABLE_REVISION,
    canConfirm: pending && token !== null,
    token,
    live: !row.dry_run,
    decisionId,
    canWithdraw: decisionId !== null && !withdrawing && !withdrawDeclined,
    withdrawing,
    withdrawDeclined,
    held: decisionId !== null && !withdrawing ? heldNote(row.decision_action, switches) : null,
  };
}

/** The guest keys of every open card's outside guests: what to look up. */
export function outsideGuestKeys(rows: Pick<ProposalRow, "payload">[]): string[] {
  const keys = new Set<string>();
  for (const row of rows) {
    for (const guest of words(row.payload?.outside_guests)) keys.add(guestKey(guest));
  }
  return [...keys].sort();
}

/**
 * The token a card's Confirm carries, as `app/channel/decide.py` builds and
 * checks it: the keyed hash's first 12 characters, the mode, the generation.
 */
export function cardToken(argsHash: string, dryRun: boolean, generation: number): string {
  return `${argsHash.slice(0, 12)}-${dryRun ? "dry" : "live"}-${generation}`;
}

/**
 * What decides where each card's buttons sit: which cards are open, in what
 * order, at which revision, and whether each is applying. The page holds its
 * buttons still for a moment whenever this changes, so a tap meant for one
 * card cannot land on another that moved into its place.
 */
export function layoutKey(
  rows: (Pick<ProposalRow, "message_id" | "revision" | "status"> & {
    outside?: number;
    /** What an applying card's footer holds: its height moves the cards below. */
    footer?: string;
  })[],
): string {
  return rows
    .map(
      (row) =>
        `${row.message_id}:${row.revision}:${row.status}:${row.outside ?? 0}:${row.footer ?? ""}`,
    )
    .join("|");
}

/** What an applying card's footer shows, as `layoutKey` wants it. */
export function footerKey(
  view: Pick<CardView, "canWithdraw" | "withdrawing" | "withdrawDeclined" | "held">,
): string {
  return [
    view.canWithdraw ? "w" : "",
    view.withdrawing ? "r" : "",
    view.withdrawDeclined ? "d" : "",
    view.held ? "h" : "",
  ].join("");
}

/** "Fri 02 Oct, 16:00 – 17:00" in `zone`; the end's day is named only when it differs. */
export function formatWindow(start: string | null, end: string | null, zone: string): string {
  const from = start ? parts(start, zone) : null;
  if (!from) return "time unknown";
  const to = end ? parts(end, zone) : null;
  if (!to) return `${from.day}, ${from.time}`;
  return from.day === to.day
    ? `${from.day}, ${from.time} – ${to.time}`
    : `${from.day}, ${from.time} – ${to.day}, ${to.time}`;
}

/** `OWNER_TIMEZONE`, or UTC when it is missing or not a zone. */
export function ownerZone(configured: string | undefined): string {
  if (!configured) return "UTC";
  try {
    new Intl.DateTimeFormat("en-GB", { timeZone: configured });
    return configured;
  } catch {
    return "UTC";
  }
}

/** How often the page re-reads while a decision is being applied. */
export const REFRESH_EVERY_MS = 3_000;

/** How often it re-reads while every open decision is held (M17, D6). */
export const HELD_REFRESH_EVERY_MS = 30_000;

/**
 * How often the page re-reads, in milliseconds, or null for not at all.
 *
 * It re-reads every few seconds while a decision is open, so "Applying…"
 * turns into its outcome. A pause, or the spending cap on an Edit, can hold
 * every open decision for days, and each read costs the database. So when
 * all of them are held, it re-reads every thirty seconds instead. A withdraw
 * still waiting for the worker's answer keeps the faster pace: the worker
 * answers within a tick, paused or not.
 */
export function refreshEvery(
  rows: Pick<ProposalRow, "status" | "decision_action" | "withdraw_requested">[],
  switches: Switches,
): number | null {
  const open = rows.filter((row) => row.status === "deciding");
  if (open.length === 0) return null;
  const moving = open.some(
    (row) => row.withdraw_requested === true || heldNote(row.decision_action, switches) === null,
  );
  return moving ? REFRESH_EVERY_MS : HELD_REFRESH_EVERY_MS;
}

function text(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function words(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

/** A day and a time in `zone`, or null for a time that cannot be read. */
function parts(iso: string, zone: string): { day: string; time: string } | null {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  const pieces = new Intl.DateTimeFormat("en-GB", {
    timeZone: zone,
    weekday: "short",
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(date);
  const get = (type: Intl.DateTimeFormatPartTypes) =>
    pieces.find((piece) => piece.type === type)?.value ?? "";
  return {
    day: `${get("weekday")} ${get("day")} ${get("month")}`,
    time: `${get("hour")}:${get("minute")}`,
  };
}
