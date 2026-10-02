/**
 * The decision a card's form submits, and what to tell the owner about the
 * answer. No framework imports, so `node --test` runs them directly.
 *
 * The rules mirror Fly's own (`app/web_api.py`, `app/channel/decide.py`),
 * which still decide: this only spares a round trip for what can never pass.
 */

import { isAddress } from "./guests.ts";

export const MAX_CORRECTION_CHARS = 2000;

export type DecisionAction = "confirm" | "edit" | "cancel";

export type Decision = {
  message_id: string;
  revision: number;
  action: DecisionAction;
  correction: string;
  /** A Confirm's token: what the card showed (M17, D2). */
  token?: string;
};

export type Parsed = { ok: true; value: Decision } | { ok: false; error: string };

const ACTIONS: readonly string[] = ["confirm", "edit", "cancel"];

/** `app/channel/decide.py`'s token: hash prefix, mode, generation. */
const TOKEN = /^[0-9a-f]{12}-(dry|live)-[1-9][0-9]{0,8}$/;

export function parseDecisionForm(data: FormData): Parsed {
  const messageId = String(data.get("message_id") ?? "");
  const revisionText = String(data.get("revision") ?? "");
  const action = String(data.get("action") ?? "");
  const correction = String(data.get("correction") ?? "").trim();
  const token = String(data.get("token") ?? "");

  if (!messageId || messageId.length > 128) return { ok: false, error: "No such proposal." };
  if (!/^[1-9]\d*$/.test(revisionText)) return { ok: false, error: "No such revision." };
  if (!ACTIONS.includes(action)) return { ok: false, error: "Not a decision." };
  if (action === "edit" && !correction) {
    return { ok: false, error: "Say what to change." };
  }
  if (correction.length > MAX_CORRECTION_CHARS) {
    return { ok: false, error: `Keep the correction under ${MAX_CORRECTION_CHARS} characters.` };
  }
  if (action === "confirm" && !TOKEN.test(token)) {
    return { ok: false, error: "This card is out of date. Reload the page." };
  }
  return {
    ok: true,
    value: {
      message_id: messageId,
      revision: Number(revisionText),
      action: action as DecisionAction,
      correction: action === "edit" ? correction : "",
      ...(action === "confirm" ? { token } : {}),
    },
  };
}

export type Answer = {
  status: "queued" | "stale" | "not_found" | "not_ready" | "outside" | "invalid" | "error";
  current_revision?: number | null;
  detail?: string;
};

export type Notice = { tone: "ok" | "warn" | "error"; message: string };

/**
 * Words for the owner. Never a raw server error: those stay in the logs. A
 * refusal's detail is shown as it comes, because Fly writes only its own fixed
 * phrases there (`app/channel/decide.py`), such as "no edits left at this
 * revision", and never the input.
 */
export function describeAnswer(answer: Answer): Notice {
  switch (answer.status) {
    case "queued":
      return { tone: "ok", message: "Applying…" };
    case "stale":
      return { tone: "warn", message: "This proposal changed. The latest version is shown." };
    case "not_found":
      return { tone: "warn", message: "That proposal is no longer waiting for a decision." };
    case "not_ready":
      return { tone: "warn", message: "This proposal is still being prepared. Try again in a minute." };
    case "outside":
      return {
        tone: "warn",
        message: "Some guests are not in this email thread. Allow or remove them first.",
      };
    case "invalid":
      return { tone: "warn", message: answer.detail || "That decision was not accepted." };
    default:
      return { tone: "error", message: "The decision could not be sent. Try again in a moment." };
  }
}


/** An Allow from a card: one guest outside the thread (M17, D4). */
export type Allow = { address: string; message_id: string };

export function parseAllowForm(
  data: FormData,
): { ok: true; value: Allow } | { ok: false; error: string } {
  const address = String(data.get("address") ?? "").trim();
  const messageId = String(data.get("message_id") ?? "");
  if (!messageId || messageId.length > 128) return { ok: false, error: "No such proposal." };
  if (!isAddress(address)) return { ok: false, error: "That is not an email address." };
  return { ok: true, value: { address, message_id: messageId } };
}

export type AllowAnswer = "allowed" | "invalid" | "error";

/** Withdraw on a queued card (M17, D6): it names the decision, not the proposal. */
export type Withdraw = { decision_id: number };

export function parseWithdrawForm(
  data: FormData,
): { ok: true; value: Withdraw } | { ok: false; error: string } {
  const id = String(data.get("decision_id") ?? "");
  if (!/^[1-9]\d{0,14}$/.test(id)) return { ok: false, error: "No such decision." };
  return { ok: true, value: { decision_id: Number(id) } };
}

export type WithdrawAnswer = "requested" | "settled" | "not_found" | "error";

export function describeWithdraw(answer: WithdrawAnswer): Notice {
  switch (answer) {
    case "requested":
      return { tone: "ok", message: "Withdrawing…" };
    case "settled":
      // Applied, withdrawn, returned or expired: the card shows which.
      return { tone: "warn", message: "That decision is no longer queued." };
    case "not_found":
      return { tone: "warn", message: "That decision no longer exists." };
    default:
      return { tone: "error", message: "The request could not be sent. Try again in a moment." };
  }
}

/** Pause or Resume, from the header (M17, D6). */
export type Switch = "pause" | "resume";

export function parseSwitchForm(
  data: FormData,
): { ok: true; value: Switch } | { ok: false; error: string } {
  const kind = String(data.get("kind") ?? "");
  if (kind !== "pause" && kind !== "resume") return { ok: false, error: "Not a switch." };
  return { ok: true, value: kind };
}

export type SwitchAnswer = "done" | "error";

export function describeSwitch(kind: Switch, answer: SwitchAnswer): Notice {
  if (answer === "done") {
    return kind === "pause"
      ? { tone: "ok", message: "Paused. Nothing new runs until you resume." }
      : { tone: "ok", message: "Resumed." };
  }
  return {
    tone: "error",
    message: `The agent could not be ${kind === "pause" ? "paused" : "resumed"}. Try again in a moment.`,
  };
}

export function describeAllow(answer: AllowAnswer, address: string): Notice {
  switch (answer) {
    case "allowed":
      return { tone: "ok", message: `Allowed ${address}. Confirm when ready.` };
    case "invalid":
      return { tone: "warn", message: "That is not an email address." };
    default:
      return { tone: "error", message: "The guest could not be allowed. Try again in a moment." };
  }
}
