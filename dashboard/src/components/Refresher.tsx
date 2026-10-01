"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

/**
 * Re-reads the page every few seconds while a decision is being applied, so
 * "Applying…" turns into the outcome without a manual reload. Idle otherwise:
 * nothing polls while nothing is open.
 *
 * It also re-reads whenever the app comes back into view. A Home Screen app
 * on iPhone resumes from memory, with no reload and no pull-to-refresh, and
 * would otherwise show proposals that were decided elsewhere meanwhile.
 */
export function Refresher({ active, every = 3000 }: { active: boolean; every?: number }) {
  const router = useRouter();

  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => router.refresh(), every);
    return () => clearInterval(timer);
  }, [active, every, router]);

  useEffect(() => {
    const onShow = () => {
      if (document.visibilityState === "visible") router.refresh();
    };
    document.addEventListener("visibilitychange", onShow);
    return () => document.removeEventListener("visibilitychange", onShow);
  }, [router]);

  return null;
}
