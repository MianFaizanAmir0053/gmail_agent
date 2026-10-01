"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

/**
 * Re-reads the page every few seconds while a decision is being applied, so
 * "Applying…" turns into the outcome without a manual reload. Idle otherwise:
 * nothing polls while nothing is open.
 */
export function Refresher({ active, every = 3000 }: { active: boolean; every?: number }) {
  const router = useRouter();

  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => router.refresh(), every);
    return () => clearInterval(timer);
  }, [active, every, router]);

  return null;
}
