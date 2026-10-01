/**
 * The decision a card's form submits, and what to tell the owner about the
 * answer. No framework imports, so `node --test` runs them directly.
 *
 * The rules mirror Fly's own (`app/web_api.py`, `app/channel/decide.py`),
 * which still decide: this only spares a round trip for what can never pass.
 */

export const MAX_CORRECTION_CHARS = 2000;

export type DecisionAction = "confirm" | "edit" | "cancel";

export type Decision = {
  message_id: string;
  revision: number;
  action: DecisionAction;
  correction: string;
};

export type Parsed = { ok: true; value: Decision } | { ok: false; error: string };

const ACTIONS: readonly string[] = ["confirm", "edit", "cancel"];

export function parseDecisionForm(data: FormData): Parsed {
  const messageId = String(data.get("message_id") ?? "");
  const revisionText = String(data.get("revision") ?? "");
  const action = String(data.get("action") ?? "");
  const correction = String(data.get("correction") ?? "").trim();

  if (!messageId || messageId.length > 128) return { ok: false, error: "No such proposal." };
  if (!/^[1-9]\d*$/.test(revisionText)) return { ok: false, error: "No such revision." };
  if (!ACTIONS.includes(action)) return { ok: false, error: "Not a decision." };
  if (action === "edit" && !correction) {
    return { ok: false, error: "Say what to change." };
  }
  if (correction.length > MAX_CORRECTION_CHARS) {
    return { ok: false, error: `Keep the correction under ${MAX_CORRECTION_CHARS} characters.` };
  }
  return {
    ok: true,
    value: {
      message_id: messageId,
      revision: Number(revisionText),
      action: action as DecisionAction,
      correction: action === "edit" ? correction : "",
    },
  };
}

export type Answer = {
  status: "queued" | "stale" | "not_found" | "invalid" | "error";
  current_revision?: number | null;
  detail?: string;
};

export type Notice = { tone: "ok" | "warn" | "error"; message: string };

/** Words for the owner. Never a raw server error: those stay in the logs. */
export function describeAnswer(answer: Answer): Notice {
  switch (answer.status) {
    case "queued":
      return { tone: "ok", message: "Applying…" };
    case "stale":
      return { tone: "warn", message: "This proposal changed. The latest version is shown." };
    case "not_found":
      return { tone: "warn", message: "That proposal is no longer waiting for a decision." };
    case "invalid":
      return { tone: "warn", message: answer.detail || "That decision was not accepted." };
    default:
      return { tone: "error", message: "The decision could not be sent. Try again in a moment." };
  }
}
