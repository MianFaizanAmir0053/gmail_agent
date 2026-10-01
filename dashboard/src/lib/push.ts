/**
 * Turning on push in the browser, worked out apart from React (M16, D5).
 * No framework imports, so `node --test` runs these directly.
 */

/** The VAPID public key `.\tasks.ps1 vapid` prints, as the bytes
 *  `PushManager.subscribe` wants for `applicationServerKey`. */
export function applicationServerKey(base64url: string): Uint8Array<ArrayBuffer> {
  const base64 = base64url.replace(/-/g, "+").replace(/_/g, "/");
  const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
  const binary = atob(padded);
  const bytes = new Uint8Array(new ArrayBuffer(binary.length));
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

export type PushSetupState = "install-first" | "off" | "on" | "denied" | "unsupported";

export type PushContext = {
  userAgent: string;
  /** `navigator.standalone`: opened from the Home Screen on iOS. */
  standalone: boolean;
  hasPush: boolean;
  permission: "default" | "granted" | "denied";
  subscribed: boolean;
};

/** What the page should offer. iOS gives push only to a web app added to the
 *  Home Screen, so an iPhone in a Safari tab is asked to install first. */
export function pushSetupState(context: PushContext): PushSetupState {
  const iPhone = /iPhone|iPad|iPod/.test(context.userAgent);
  if (!context.hasPush) {
    return iPhone && !context.standalone ? "install-first" : "unsupported";
  }
  if (context.subscribed && context.permission === "granted") return "on";
  if (context.permission === "denied") return "denied";
  return "off";
}

export type SubscriptionBody = {
  endpoint: string;
  keys: { p256dh: string; auth: string };
};

/** The part of a `PushSubscription` Fly stores, or null for anything else. */
export function subscriptionBody(value: unknown): SubscriptionBody | null {
  if (typeof value !== "object" || value === null) return null;
  const { endpoint, keys } = value as { endpoint?: unknown; keys?: unknown };
  if (typeof endpoint !== "string" || typeof keys !== "object" || keys === null) return null;
  const { p256dh, auth } = keys as { p256dh?: unknown; auth?: unknown };
  if (typeof p256dh !== "string" || typeof auth !== "string") return null;
  return { endpoint, keys: { p256dh, auth } };
}
