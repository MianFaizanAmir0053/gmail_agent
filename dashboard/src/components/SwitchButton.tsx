"use client";

import { useActionState, useEffect, useState } from "react";

import { switchFromHeader } from "@/app/actions";
import type { Notice } from "@/lib/decisionForm";
import { RESUME_READY_AFTER_MS, switchStep } from "@/lib/switches";

/**
 * Pause or Resume (M17, D6).
 *
 * Pause takes one tap: it is the safe direction. Resume releases every held
 * decision at once, and a Confirm it sends cannot be called back, so it asks
 * first (`switchStep`). The question puts "Keep paused" first, and "Resume
 * now" ignores taps for a moment after it appears: a double tap on Resume
 * never resumes, wherever a narrow screen wraps the buttons. Only a failure is
 * spelled out: the banner shows the rest.
 */
export function SwitchButton({ paused }: { paused: boolean }) {
  const [notice, submit, pending] = useActionState<Notice | null, FormData>(switchFromHeader, null);
  const [asking, setAsking] = useState(false);
  const [ready, setReady] = useState(false);
  const step = switchStep({ paused, asking });

  useEffect(() => {
    if (!asking) return;
    const timer = setTimeout(() => setReady(true), RESUME_READY_AFTER_MS);
    return () => {
      clearTimeout(timer);
      setReady(false);
    };
  }, [asking]);

  const failure = notice && notice.tone !== "ok" && (
    <span className={`note ${notice.tone}`} role="status">
      {notice.message}
    </span>
  );

  if (step === "ask") {
    return (
      <div className="switch">
        <button type="button" className="primary" onClick={() => setAsking(true)}>
          Resume…
        </button>
        {failure}
      </div>
    );
  }

  if (step === "confirm") {
    return (
      <form action={submit} className="switch">
        <input type="hidden" name="kind" value="resume" />
        <button type="button" disabled={pending} onClick={() => setAsking(false)}>
          Keep paused
        </button>
        <span className="note">Held decisions apply at once.</span>
        <button type="submit" className="primary" disabled={pending || !ready}>
          Resume now
        </button>
        {failure}
      </form>
    );
  }

  return (
    <form action={submit} className="switch">
      <input type="hidden" name="kind" value="pause" />
      <button type="submit" disabled={pending}>
        Pause
      </button>
      {failure}
    </form>
  );
}
