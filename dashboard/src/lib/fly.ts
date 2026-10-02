import "server-only";

import { isPairingCode } from "@/lib/access";
import { failureKind, flyConfig } from "@/lib/flyConfig";
import type {
  Allow,
  AllowAnswer,
  Answer,
  Decision,
  Switch,
  SwitchAnswer,
  WithdrawAnswer,
} from "@/lib/decisionForm";
import type { SubscriptionBody } from "@/lib/push";

/**
 * The web app's only write path: Fly's API (M16, D4).
 *
 * Server-side only. `WEB_API_SECRET` and `FLY_API_URL` live in the server
 * environment, and the `server-only` import makes a client component that
 * pulls this file in fail the build rather than ship the secret.
 */

const TIMEOUT_MS = 10_000;

/**
 * Store or refresh this browser's push subscription on Fly. Returns the HTTP
 * status to pass back to the browser: 204, 409 (too many), 422, or 502 when
 * Fly could not be reached or refused the secret.
 */
export function postSubscription(subscription: SubscriptionBody): Promise<number> {
  return sendSubscription("POST", subscription);
}

/** Forget a subscription this browser replaced. Same statuses. */
export function deleteSubscription(endpoint: string): Promise<number> {
  return sendSubscription("DELETE", { endpoint });
}

async function sendSubscription(method: "POST" | "DELETE", body: unknown): Promise<number> {
  const fly = flyConfig(process.env);
  if (fly === null) {
    console.error("FLY_API_URL or WEB_API_SECRET is not configured");
    return 502;
  }
  const { base, secret } = fly;
  try {
    const response = await fetch(new URL("/api/push-subscriptions", base), {
      method,
      headers: { Authorization: `Bearer ${secret}`, "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    if (response.status !== 204) {
      // The status alone: a 401 means the two secrets differ, not an outage.
      console.error(`subscription ${method} answered`, response.status);
    }
    return [204, 409, 422].includes(response.status) ? response.status : 502;
  } catch (error) {
    console.error("subscription not sent:", failureKind(error));
    return 502;
  }
}

export async function postDecision(decision: Decision): Promise<Answer> {
  const fly = flyConfig(process.env);
  if (fly === null) {
    console.error("FLY_API_URL or WEB_API_SECRET is not configured");
    return { status: "error" };
  }
  const { base, secret } = fly;

  let response: Response;
  try {
    response = await fetch(new URL("/api/decisions", base), {
      method: "POST",
      headers: { Authorization: `Bearer ${secret}`, "Content-Type": "application/json" },
      body: JSON.stringify(decision),
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch (error) {
    // The kind of failure and its code only: a message can carry the URL.
    console.error("decision not sent:", failureKind(error));
    return { status: "error" };
  }

  const body: Record<string, unknown> = await response.json().catch(() => ({}));
  switch (response.status) {
    case 202:
      return { status: "queued" };
    case 409:
      if (body.status === "not_ready") return { status: "not_ready" };
      return {
        status: "stale",
        current_revision: typeof body.current_revision === "number" ? body.current_revision : null,
      };
    case 404:
      return { status: "not_found" };
    case 422:
      if (body.status === "outside") return { status: "outside" };
      return {
        status: "invalid",
        detail: typeof body.detail === "string" ? body.detail : undefined,
      };
    default:
      console.error("decision refused with status", response.status);
      return { status: "error" };
  }
}

/** Allow a guest outside the thread (M17, D4). */
export async function postContact(allow: Allow): Promise<AllowAnswer> {
  const response = await postToFly("/api/contacts", allow);
  if (response === null) return "error";
  if (response.status === 204) return "allowed";
  if (response.status === 422) return "invalid";
  console.error("contact answered", response.status);
  return "error";
}

/** Pause or resume the agent (M17, D6). */
export async function postSwitch(kind: Switch): Promise<SwitchAnswer> {
  const response = await postToFly(`/api/${kind}`, {});
  if (response === null) return "error";
  if (response.status === 204) return "done";
  console.error(kind, "answered", response.status);
  return "error";
}

/** Ask for a queued decision to be withdrawn (M17, D6). The worker carries it out. */
export async function postWithdraw(decisionId: number): Promise<WithdrawAnswer> {
  const response = await postToFly("/api/decisions/withdraw", { decision_id: decisionId });
  if (response === null) return "error";
  switch (response.status) {
    case 202:
      return "requested";
    case 409:
      return "settled";
    case 404:
      return "not_found";
    default:
      console.error("withdraw answered", response.status);
      return "error";
  }
}

/** A pairing code as Fly issued it (M16, D4): shown to the owner once. */
export type IssuedPairingCode = { code: string; expiresAt: string };

/**
 * Ask Fly for a pairing code, which ends any earlier one. Null when Fly could
 * not be reached, refused the secret, or has pairing switched off; the log
 * names the status, never the code.
 */
export async function issuePairingCode(issuedTo: string): Promise<IssuedPairingCode | null> {
  const response = await postToFly("/api/pairing/codes", { issued_to: issuedTo });
  if (response === null) return null;
  if (response.status !== 201) {
    console.error("pairing code request answered", response.status);
    return null;
  }
  const body: Record<string, unknown> = await response.json().catch(() => ({}));
  if (!isPairingCode(body.code) || typeof body.expires_at !== "string") {
    console.error("pairing code answer was not a code");
    return null;
  }
  return { code: body.code, expiresAt: body.expires_at };
}

/**
 * Redeem a pairing code on Fly. True only when Fly accepted it. A refusal
 * never says why: wrong, expired, spent and absent codes look the same.
 */
export async function redeemPairingCode(code: string): Promise<boolean> {
  const response = await postToFly("/api/pairing/redeem", { code });
  if (response === null) return false;
  // A 403 is a code that did not work: an answer, not a fault.
  if (response.status !== 204 && response.status !== 403) {
    console.error("pairing redeem answered", response.status);
  }
  return response.status === 204;
}

/** POST a JSON body to Fly with the secret. Null when it could not be sent. */
async function postToFly(path: string, body: unknown): Promise<Response | null> {
  const fly = flyConfig(process.env);
  if (fly === null) {
    console.error("FLY_API_URL or WEB_API_SECRET is not configured");
    return null;
  }
  try {
    return await fetch(new URL(path, fly.base), {
      method: "POST",
      headers: { Authorization: `Bearer ${fly.secret}`, "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch (error) {
    // The kind of failure and its code only: a message can carry the URL.
    console.error(path, "not sent:", failureKind(error));
    return null;
  }
}
