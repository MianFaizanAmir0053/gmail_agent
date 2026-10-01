import { query } from "@/lib/db";
import type { ProposalRow } from "@/lib/timeline";

/**
 * The timeline's reads. They go through `query`, so they need the owner's
 * session, and in production they run as `web_reader`, which can read these
 * two tables and write nothing.
 */

export type DecisionRow = {
  id: string;
  message_id: string;
  revision: number;
  action: string;
  via: string;
  outcome: string | null;
  reason: string | null;
  decided_at: string | Date;
  title: string | null;
};

/** How many open proposals the timeline shows at once. */
export const OPEN_SHOWN = 50;

/**
 * Proposals waiting for the owner, or being applied: newest first, at most
 * `OPEN_SHOWN` of them, with how many are open in all, so the page can say
 * when some are not shown.
 *
 * The order is total -- ties broken by message id -- because the page
 * re-reads every few seconds while a decision is applied. An order that
 * shuffled tied cards on each read would move them under the owner's thumb,
 * and a tap meant for one card could land on another's button.
 */
export async function openProposals(): Promise<{ rows: ProposalRow[]; total: number }> {
  const rows = await query<ProposalRow & { total: string }>(
    `
    SELECT message_id, revision, status, final_status, action_type, dry_run,
           payload, parked_at, count(*) OVER () AS total
      FROM proposals
     WHERE status IN ('pending', 'deciding')
     ORDER BY parked_at DESC, message_id
     LIMIT $1
    `,
    [OPEN_SHOWN],
  );
  return { rows, total: Number(rows[0]?.total ?? 0) };
}

/** The latest decisions, from every channel, with what became of them. */
export async function recentDecisions(limit = 20): Promise<DecisionRow[]> {
  return query<DecisionRow>(
    `
    SELECT d.id::text, d.message_id, d.revision, d.action, d.via, d.outcome,
           d.reason, d.decided_at, p.payload->>'title' AS title
      FROM decisions d
      JOIN proposals p USING (message_id)
     ORDER BY d.decided_at DESC, d.id DESC
     LIMIT $1
    `,
    [limit],
  );
}
