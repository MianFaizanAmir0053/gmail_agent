/**
 * The Activity page's words (M17, D7): one line per audit entry. No framework
 * imports, so `node --test` runs these rules directly.
 *
 * The audit log holds no content: kinds, tools, outcomes and fixed phrases,
 * with ids. What an entry was about is shown by its proposal's title, read
 * from the proposal while retention keeps it.
 */

export type ActivityRow = {
  id: string;
  at: string | Date;
  kind: string;
  tool: string | null;
  dry_run: boolean | null;
  decision_id: string | null;
  message_id: string | null;
  reason: string | null;
  /** The proposal's title, while it is kept. */
  title: string | null;
  /** Whether the message has a proposal row at all. */
  has_proposal: boolean;
};

/**
 * What an entry was about: its proposal's title while it is kept, and
 * "(cleared)" once retention has removed it. A message that never had a
 * proposal, such as one poll skipped as too costly to read, shows nothing.
 */
export function proposalLabel(row: Pick<ActivityRow, "message_id" | "title" | "has_proposal">): string {
  if (!row.message_id || !row.has_proposal) return "";
  return row.title ?? "(cleared)";
}

/** Every kind `app/policy/audit.py` may write. A test holds the two lists together. */
export const KINDS = [
  "action_approved",
  "action_executed",
  "action_refused",
  "action_failed",
  "decision_withdrawn",
  "withdraw_declined",
  "proposal_expired",
  "paused",
  "resumed",
  "budget_warning",
  "budget_exhausted",
  "budget_ok",
  "budget_unpriced",
  "message_too_costly",
  "contact_allowed",
  "contact_removed",
  "write_unconfirmed",
] as const;

const TOOLS: Record<string, string> = {
  "calendar.freebusy": "a calendar lookup",
  "calendar.create_hold": "a calendar hold",
  "calendar.create_invite": "a calendar invite",
};

export type ActivityLine = { what: string; tone: "ok" | "warn" | "error" | null };

/**
 * One entry in words. Total: a kind added on Fly before this list learns it
 * is shown as it is, rather than taking the page down.
 */
export function activityLine(
  row: Pick<ActivityRow, "kind" | "tool" | "dry_run" | "reason">,
): ActivityLine {
  const tool = (row.tool && TOOLS[row.tool]) || "an action";
  const why = row.reason ? `: ${row.reason}` : "";
  switch (row.kind) {
    case "action_approved":
      return { what: `Approved ${tool}`, tone: null };
    case "action_executed":
      return { what: row.dry_run ? `Ran ${tool} as a dry run` : `Ran ${tool}`, tone: "ok" };
    case "action_refused":
      return { what: `Refused ${tool}${why}`, tone: "warn" };
    case "action_failed":
      return { what: `Could not run ${tool}${why}`, tone: "error" };
    case "decision_withdrawn":
      return { what: "Withdrawn by the owner", tone: null };
    case "withdraw_declined":
      return { what: "Withdraw declined: already being applied", tone: "warn" };
    case "proposal_expired":
      return { what: `Proposal expired${why}`, tone: "warn" };
    case "paused":
      return { what: `Paused${row.reason ? `, ${row.reason}` : ""}`, tone: "warn" };
    case "resumed":
      return { what: `Resumed${row.reason ? `, ${row.reason}` : ""}`, tone: "ok" };
    case "budget_warning":
      return { what: "Model spending reached 80% of the cap", tone: "warn" };
    case "budget_exhausted":
      return { what: "Spending cap reached: new work stopped", tone: "error" };
    case "budget_ok":
      return { what: "Model spending back under 80% of the cap", tone: "ok" };
    case "budget_unpriced":
      return { what: "A model in use has no price: new work stopped", tone: "error" };
    case "message_too_costly":
      return { what: "A message was too costly to read", tone: "warn" };
    case "contact_allowed":
      return { what: "Allowed a guest outside the thread", tone: null };
    case "contact_removed":
      return { what: "Removed a confirmed contact", tone: null };
    case "write_unconfirmed":
      return { what: "A calendar write could not be confirmed", tone: "error" };
    default:
      return { what: row.kind, tone: null };
  }
}
