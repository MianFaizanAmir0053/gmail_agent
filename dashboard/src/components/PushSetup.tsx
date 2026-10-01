"use client";

import { useEffect, useState } from "react";

import { applicationServerKey, pushSetupState, type PushSetupState } from "@/lib/push";

/**
 * Registers the service worker and keeps this browser's push subscription
 * known to Fly. Every time the app opens it re-posts the subscription, so one
 * Fly deleted after a 410 is replaced as soon as the owner comes back.
 *
 * Permission is asked for only on a tap: iOS refuses to ask otherwise, and an
 * unprompted request is how a browser learns to block a site.
 */
export function PushSetup({ publicKey }: { publicKey: string | null }) {
  const [state, setState] = useState<PushSetupState | "checking" | "error">("checking");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!("serviceWorker" in navigator)) return "unsupported" as const;
      const registration = await navigator.serviceWorker.register("/sw.js");
      const hasPush = "PushManager" in window && "Notification" in window;
      const existing = hasPush ? await registration.pushManager.getSubscription() : null;
      if (existing) await post(existing);
      return pushSetupState({
        userAgent: navigator.userAgent,
        standalone: (navigator as Navigator & { standalone?: boolean }).standalone === true,
        hasPush,
        permission: hasPush ? Notification.permission : "default",
        subscribed: existing !== null,
      });
    })()
      .then((next) => !cancelled && setState(next))
      .catch(() => !cancelled && setState("error"));
    return () => {
      cancelled = true;
    };
  }, []);

  async function turnOn() {
    if (!publicKey) return setState("error");
    try {
      const registration = await navigator.serviceWorker.ready;
      const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: applicationServerKey(publicKey),
      });
      await post(subscription);
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

async function post(subscription: PushSubscription): Promise<void> {
  const response = await fetch("/api/push-subscription", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(subscription),
  });
  if (!response.ok) throw new Error(`subscription not stored: ${response.status}`);
}
