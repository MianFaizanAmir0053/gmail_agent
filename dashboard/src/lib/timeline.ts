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
};

export function cardView(row: ProposalRow, ownerZone: string): CardView {
  const card = row.payload;
  const pending = row.status === "pending";
  return {
    messageId: row.message_id,
    revision: row.revision,
    title: card === null ? "(cleared)" : card.title || "(untitled)",
    when: formatWindow(card?.start_utc ?? null, card?.end_utc ?? null, ownerZone),
    eventZone: card?.timezone ?? null,
    attendees: card?.attendees ?? [],
    location: card?.location ?? null,
    conflicts: card?.conflicts ?? [],
    reviewIssues: card?.review_issues ?? [],
    dryRun: row.dry_run,
    invite: row.action_type === "calendar_invite",
    applying: row.status === "deciding",
    canDecide: pending,
    canEdit: pending && row.revision <= LAST_EDITABLE_REVISION,
  };
}

/** "Fri 02 Oct, 16:00 – 17:00" in `zone`; the end's day is named only when it differs. */
export function formatWindow(start: string | null, end: string | null, zone: string): string {
  if (!start) return "time unknown";
  const from = parts(start, zone);
  if (!end) return `${from.day}, ${from.time}`;
  const to = parts(end, zone);
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

function parts(iso: string, zone: string): { day: string; time: string } {
  const pieces = new Intl.DateTimeFormat("en-GB", {
    timeZone: zone,
    weekday: "short",
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(iso));
  const get = (type: Intl.DateTimeFormatPartTypes) =>
    pieces.find((piece) => piece.type === type)?.value ?? "";
  return {
    day: `${get("weekday")} ${get("day")} ${get("month")}`,
    time: `${get("hour")}:${get("minute")}`,
  };
}
