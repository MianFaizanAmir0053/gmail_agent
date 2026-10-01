import "server-only";

import type { Answer, Decision } from "@/lib/decisionForm";
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
 * Fly could not be reached.
 */
export async function postSubscription(subscription: SubscriptionBody): Promise<number> {
  const base = process.env.FLY_API_URL;
  const secret = process.env.WEB_API_SECRET;
  if (!base || !secret) {
    console.error("FLY_API_URL or WEB_API_SECRET is not configured");
    return 502;
  }
  try {
    const response = await fetch(new URL("/api/push-subscriptions", base), {
      method: "POST",
      headers: { Authorization: `Bearer ${secret}`, "Content-Type": "application/json" },
      body: JSON.stringify(subscription),
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    return [204, 409, 422].includes(response.status) ? response.status : 502;
  } catch (error) {
    console.error("subscription not sent:", error instanceof Error ? error.name : "unknown error");
    return 502;
  }
}

export async function postDecision(decision: Decision): Promise<Answer> {
  const base = process.env.FLY_API_URL;
  const secret = process.env.WEB_API_SECRET;
  if (!base || !secret) {
    console.error("FLY_API_URL or WEB_API_SECRET is not configured");
    return { status: "error" };
  }

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
    // The kind of failure only: a message can carry the URL.
    console.error("decision not sent:", error instanceof Error ? error.name : "unknown error");
    return { status: "error" };
  }

  const body: Record<string, unknown> = await response.json().catch(() => ({}));
  switch (response.status) {
    case 202:
      return { status: "queued" };
    case 409:
      return {
        status: "stale",
        current_revision: typeof body.current_revision === "number" ? body.current_revision : null,
      };
    case 404:
      return { status: "not_found" };
    case 422:
      return {
        status: "invalid",
        detail: typeof body.detail === "string" ? body.detail : undefined,
      };
    default:
      console.error("decision refused with status", response.status);
      return { status: "error" };
  }
}
