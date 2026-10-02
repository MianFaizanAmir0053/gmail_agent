import { unstable_rethrow } from "next/navigation";

import { query } from "@/lib/db";
import { readSwitches, type Switches } from "@/lib/switches";
import type { ProposalRow } from "@/lib/timeline";

/**
 * The timeline's reads. They go through `query`, so they need the owner's
 * session, and in production they run as `web_reader`, which can read these
 * tables and write nothing.
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
  // The open decision, if any, rides along for Withdraw (M17, D6): there is
  // at most one per proposal, so the join adds no rows.
  const rows = await query<ProposalRow & { total: string }>(
    `
    SELECT p.message_id, p.revision, p.status, p.final_status, p.action_type, p.dry_run,
           p.args_hash, p.generation, p.payload, p.parked_at,
           d.id::text AS decision_id, d.action AS decision_action,
           d.withdraw_requested_at IS NOT NULL AS withdraw_requested,
           EXISTS (SELECT 1 FROM audit_log a
                    WHERE a.kind = 'withdraw_declined' AND a.decision_id = d.id)
               AS withdraw_declined,
           count(*) OVER () AS total
      FROM proposals p
      LEFT JOIN decisions d ON d.message_id = p.message_id AND d.outcome IS NULL
     WHERE p.status IN ('pending', 'deciding')
     ORDER BY p.parked_at DESC, p.message_id
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


/**
 * Which of these guest keys the owner has allowed (M17, D4), read as
 * `web_reader`, which may read `confirmed_contacts` and write nothing.
 */
export async function allowedContacts(keys: string[]): Promise<Set<string>> {
  if (keys.length === 0) return new Set();
  const rows = await query<{ address: string }>(
    "SELECT address FROM confirmed_contacts WHERE address = ANY($1)",
    [keys],
  );
  return new Set(rows.map((row) => row.address));
}

/**
 * The owner's switches (M17, D5 and D6), read as `web_reader`, which may read
 * `control` and write nothing. Pause and Resume go through Fly.
 */
export async function switches(): Promise<Switches> {
  const rows = await query<{ paused: boolean; budget_state: string }>(
    "SELECT paused, budget_state FROM control WHERE id = 1",
  );
  // Fly refuses to run without the row. Read as "running", the header would
  // say nothing was wrong.
  if (rows.length === 0) throw new Error("the control row is missing: run the migrations");
  return readSwitches(rows[0]);
}

/**
 * `switches()`, or null when they cannot be read. The header and the
 * timeline both read them, and neither may fail because of them. Next's own
 * signals, such as the redirect to sign in, still pass through.
 */
export async function switchesOrNull(): Promise<Switches | null> {
  try {
    return await switches();
  } catch (error) {
    unstable_rethrow(error);
    console.error("switches not read:", error instanceof Error ? error.name : "unknown error");
    return null;
  }
}
