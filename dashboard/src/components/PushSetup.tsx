"use client";

import { useEffect, useState } from "react";

import {
  applicationServerKey,
  madeWithOtherKey,
  pushSetupState,
  type PushSetupState,
} from "@/lib/push";

/**
 * Registers the service worker and keeps this browser's push subscription
 * known to Fly. Every time the app opens or comes back into view it re-posts
 * the subscription, so one Fly deleted after a 410 is stored again as soon as
 * the owner comes back. A subscription made with VAPID keys since replaced is
 * dropped and made again with the current ones.
 *
 * Permission is asked for only on a tap: iOS refuses to ask otherwise, and an
 * unprompted request is how a browser learns to block a site.
 */
export function PushSetup({ publicKey }: { publicKey: string | null }) {
  const [state, setState] = useState<PushSetupState | "checking" | "error">("checking");

  useEffect(() => {
    let cancelled = false;
    const sync = () => {
      syncSubscription(publicKey)
        .then((next) => !cancelled && setState(next))
        .catch(() => !cancelled && setState("error"));
    };
    const onShow = () => {
      if (document.visibilityState === "visible") sync();
    };
    sync();
    document.addEventListener("visibilitychange", onShow);
    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", onShow);
    };
  }, [publicKey]);

  async function turnOn() {
    if (!publicKey) return setState("error");
    try {
      const registration = await navigator.serviceWorker.ready;
      const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: applicationServerKey(publicKey),
      });
      await send("POST", subscription);
      setState("on");
    } catch {
      setState(Notification.permission === "denied" ? "denied" : "error");
    }
  }

  switch (state) {
    case "off":
      return (
        <div className="push">
          <button type="button" onClick={turnOn}>
            Turn on notifications
          </button>
          <span className="muted"> to hear when a proposal needs you.</span>
        </div>
      );
    case "install-first":
      return (
        <p className="push note">
          For notifications on iPhone: tap Share, then <b>Add to Home Screen</b>, and open mailagent
          from your Home Screen.
        </p>
      );
    case "denied":
      return <p className="push note warn">Notifications are blocked for this site in your browser settings.</p>;
    case "error":
      return <p className="push note warn">Notifications could not be set up on this device.</p>;
    default:
      return null;
  }
}

/** Bring Fly up to date with this browser's subscription. Returns what to show. */
async function syncSubscription(publicKey: string | null): Promise<PushSetupState> {
  if (!("serviceWorker" in navigator)) return "unsupported";
  const registration = await navigator.serviceWorker.register("/sw.js");
  const hasPush = "PushManager" in window && "Notification" in window;
  let subscription = hasPush ? await registration.pushManager.getSubscription() : null;

  if (subscription && publicKey && madeWithOtherKey(subscription.options?.applicationServerKey, publicKey)) {
    // Made before the keys were rotated: every push to it is refused for good.
    const replaced = subscription.endpoint;
    await subscription.unsubscribe();
    await send("DELETE", { endpoint: replaced }).catch(() => undefined);
    // Some browsers subscribe only from a tap; the button then offers it.
    subscription = await registration.pushManager
      .subscribe({ userVisibleOnly: true, applicationServerKey: applicationServerKey(publicKey) })
      .catch(() => null);
  }

  if (subscription) {
    // A failed re-post is not a failed set-up: the copy Fly holds still works.
    await send("POST", subscription).catch((error: unknown) => {
      console.warn("push subscription not re-sent:", error instanceof Error ? error.message : error);
    });
  }

  return pushSetupState({
    userAgent: navigator.userAgent,
    standalone: (navigator as Navigator & { standalone?: boolean }).standalone === true,
    hasPush,
    permission: hasPush ? Notification.permission : "default",
    subscribed: subscription !== null,
  });
}

async function send(method: "POST" | "DELETE", body: PushSubscription | { endpoint: string }): Promise<void> {
  const response = await fetch("/api/push-subscription", {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`subscription not stored: ${response.status}`);
}
