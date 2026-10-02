/**
 * The owner's switches as the web app shows them (M17, D5 and D6): whether
 * the agent is paused, and where this month's model spending stands. Read
 * from the `control` row; no framework imports, so `node --test` runs these
 * rules directly.
 */

/** `unpriced`: a model in use has no price, which stops new work as the cap does. */
export type BudgetState = "ok" | "warning" | "exhausted" | "unpriced";

export type Switches = { paused: boolean; budgetState: BudgetState };

/** Before the row has been read, or when it cannot be: nothing to say. */
export const NO_SWITCHES: Switches = { paused: false, budgetState: "ok" };

const BUDGET_STATES: readonly string[] = ["ok", "warning", "exhausted", "unpriced"];

/** The `control` row as Postgres returns it, read defensively. */
export function readSwitches(row: { paused?: unknown; budget_state?: unknown } | undefined): Switches {
  if (!row) return NO_SWITCHES;
  const budget = typeof row.budget_state === "string" && BUDGET_STATES.includes(row.budget_state);
  return {
    paused: row.paused === true,
    budgetState: budget ? (row.budget_state as BudgetState) : "ok",
  };
}

/**
 * The switches as `/api/switches` returns them, read defensively: null for
 * anything else, so the header keeps what it already shows.
 */
export function parseSwitches(body: unknown): Switches | null {
  if (typeof body !== "object" || body === null) return null;
  const { paused, budgetState } = body as { paused?: unknown; budgetState?: unknown };
  if (typeof paused !== "boolean") return null;
  if (typeof budgetState !== "string" || !BUDGET_STATES.includes(budgetState)) return null;
  return { paused, budgetState: budgetState as BudgetState };
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
  } else if (switches.budgetState === "unpriced") {
    shown.push({
      tone: "error",
      text: "A model in use has no price: mail processing has stopped until it is priced.",
    });
  } else if (switches.budgetState === "warning") {
    shown.push({ tone: "warn", text: "Model spending is at 80% of this month's cap." });
  }
  return shown;
}

/**
 * Why a queued decision is waiting, if it is held: every one while paused,
 * and an Edit while new work is stopped -- the cap, or a model with no price
 * -- since re-extracting calls a model. A Confirm or a Cancel calls none, and
 * still applies.
 */
export function heldNote(action: string | null | undefined, switches: Switches): string | null {
  if (!action) return null;
  if (switches.paused) return "Paused: this applies when you resume.";
  if (action === "edit" && switches.budgetState === "exhausted") {
    return "Waiting: the spending cap holds edits for now. Withdraw it to confirm or cancel instead.";
  }
  if (action === "edit" && switches.budgetState === "unpriced") {
    return "Waiting: a model in use has no price, so edits are held. Withdraw it to confirm or cancel instead.";
  }
  return null;
}

/**
 * How long "Resume now" ignores taps after the question appears: longer
 * than a double tap, so one can never resume, wherever the buttons land on a
 * narrow screen.
 */
export const RESUME_READY_AFTER_MS = 600;

/**
 * What the header's switch shows (M17, D6). Pause takes one tap: it is the
 * safe direction. Resume releases every held decision at once, and a Confirm
 * it sends cannot be called back, so it asks first:
 * - `pause`: the Pause button;
 * - `ask`: Resume, which only opens the question;
 * - `confirm`: the question, with "Keep paused" and "Resume now", which
 *   answers only after `RESUME_READY_AFTER_MS`.
 */
export function switchStep({ paused, asking }: { paused: boolean; asking: boolean }): "pause" | "ask" | "confirm" {
  if (!paused) return "pause";
  return asking ? "confirm" : "ask";
}
