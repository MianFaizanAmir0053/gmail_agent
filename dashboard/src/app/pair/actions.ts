"use server";

import { auth } from "@/auth";
import { isOwnerSession, isPairingEnabled } from "@/lib/access";
import { issuePairingCode } from "@/lib/fly";
import { formatWindow, ownerZone } from "@/lib/timeline";

export type PairingAnswer = { ok: true; code: string; validUntil: string } | { ok: false; message: string };

/**
 * "Show a pairing code" on `/pair` (M16, D4).
 *
 * The owner is checked here, first. A Server Function is a POST to the page's
 * route, and Next documents that the proxy can miss one, so this check is the
 * one that holds. Each code ends the one before it, on Fly.
 */
export async function showPairingCode(_previous: PairingAnswer | null): Promise<PairingAnswer> {
  const session = await auth();
  if (!isOwnerSession(session?.user?.email, process.env.OWNER_EMAIL)) {
    return { ok: false, message: "Sign in as the owner first." };
  }
  if (!isPairingEnabled(process.env.PAIRING_ENABLED)) {
    return { ok: false, message: "Pairing is off." };
  }

  const issued = await issuePairingCode("web");
  if (!issued) {
    return { ok: false, message: "No code could be issued. Try again in a moment." };
  }
  const zone = ownerZone(process.env.OWNER_TIMEZONE);
  return { ok: true, code: issued.code, validUntil: `${formatWindow(issued.expiresAt, null, zone)} (${zone})` };
}
