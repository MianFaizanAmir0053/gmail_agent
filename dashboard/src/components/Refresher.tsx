"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

/**
 * Re-reads the page every `every` milliseconds while a decision is open, so
 * "Applying…" turns into the outcome without a manual reload. Idle when
 * `every` is null: nothing polls while nothing is open. The page chooses the
 * pace (`refreshEvery` in `src/lib/timeline.ts`).
 *
 * It also re-reads whenever the app comes back into view. A Home Screen app
 * on iPhone resumes from memory, with no reload and no pull-to-refresh, and
 * would otherwise show proposals that were decided elsewhere meanwhile.
 */
export function Refresher({ every }: { every: number | null }) {
  const router = useRouter();

  useEffect(() => {
    if (every === null) return;
    const timer = setInterval(() => router.refresh(), every);
    return () => clearInterval(timer);
  }, [every, router]);

  useEffect(() => {
    const onShow = () => {
      if (document.visibilityState === "visible") router.refresh();
    };
    document.addEventListener("visibilitychange", onShow);
    return () => document.removeEventListener("visibilitychange", onShow);
  }, [router]);

  return null;
}
