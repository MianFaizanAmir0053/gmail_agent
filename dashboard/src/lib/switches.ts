/**
 * The owner's switches as the web app shows them (M17, D5 and D6): whether
 * the agent is paused, and where this month's model spending stands. Read
 * from the `control` row; no framework imports, so `node --test` runs these
 * rules directly.
 */

export type BudgetState = "ok" | "warning" | "exhausted";

export type Switches = { paused: boolean; budgetState: BudgetState };

/** Before the row has been read, or when it cannot be: nothing to say. */
export const NO_SWITCHES: Switches = { paused: false, budgetState: "ok" };

const BUDGET_STATES: readonly string[] = ["ok", "warning", "exhausted"];

/** The `control` row as Postgres returns it, read defensively. */
export function readSwitches(row: { paused?: unknown; budget_state?: unknown } | undefined): Switches {
  if (!row) return NO_SWITCHES;
  const budget = typeof row.budget_state === "string" && BUDGET_STATES.includes(row.budget_state);
  return {
    paused: row.paused === true,
    budgetState: budget ? (row.budget_state as BudgetState) : "ok",
  };
}

export type Banner = { tone: "warn" | "error"; text: string };

/**
 * What the header says, most pressing first. Words only, as the alerts: the
 * month's spend is shown by `/health`, with the bearer.
 */
export function banners(switches: Switches): Banner[] {
  const shown: Banner[] = [];
  if (switches.paused) {
    shown.push({
      tone: "warn",
      text: "Paused: no new mail is read and no decision is applied until you resume. Withdraw still works.",
    });
  }
  if (switches.budgetState === "exhausted") {
    shown.push({
      tone: "error",
      text: "Spending cap reached: mail processing has stopped until next month, or until the cap is raised.",
    });
  } else if (switches.budgetState === "warning") {
    shown.push({ tone: "warn", text: "Model spending is at 80% of this month's cap." });
  }
  return shown;
}

/**
 * Why a queued decision is waiting, if it is held: every one while paused,
 * and an Edit while the cap stops new work, since re-extracting calls a
 * model. A Confirm or a Cancel calls none, and applies at the cap.
 */
export function heldNote(action: string | null | undefined, switches: Switches): string | null {
  if (!action) return null;
  if (switches.paused) return "Paused: this applies when you resume.";
  if (action === "edit" && switches.budgetState === "exhausted") {
    return "Waiting: the spending cap holds edits for now. Withdraw it to confirm or cancel instead.";
  }
  return null;
}
