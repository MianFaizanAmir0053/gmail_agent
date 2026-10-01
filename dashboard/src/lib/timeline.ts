/**
 * What a proposal card shows, worked out apart from React.
 *
 * No framework imports, so `node --test` runs these rules directly. The page
 * and the card only render what `cardView` decides.
 */

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
};

/**
 * What one card shows. Total: whatever the stored payload holds, it returns a
 * view rather than throwing, because one odd row must not take the whole
 * timeline down with it.
 */
export function cardView(row: ProposalRow, ownerZone: string): CardView {
  const card = row.payload;
  // Deciding needs the content: the owner would otherwise approve something
  // they cannot see. Retention never clears an open proposal, so this holds
  // only for a row nobody expected.
  const pending = row.status === "pending" && card !== null;
  const token = row.args_hash ? cardToken(row.args_hash, row.dry_run, row.generation) : null;
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
    dryRun: row.dry_run,
    invite: row.action_type === "calendar_invite",
    applying: row.status === "deciding",
    canDecide: pending,
    canEdit: pending && row.revision <= LAST_EDITABLE_REVISION,
    canConfirm: pending && token !== null,
    token,
    live: !row.dry_run,
  };
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
export function layoutKey(rows: Pick<ProposalRow, "message_id" | "revision" | "status">[]): string {
  return rows.map((row) => `${row.message_id}:${row.revision}:${row.status}`).join("|");
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

/** True while a decision is open, so the page re-reads until it settles. */
export function shouldRefresh(rows: Pick<ProposalRow, "status">[]): boolean {
  return rows.some((row) => row.status === "deciding");
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
