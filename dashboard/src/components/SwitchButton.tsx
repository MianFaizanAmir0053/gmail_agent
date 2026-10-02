"use client";

import { useActionState } from "react";

import { switchFromHeader } from "@/app/actions";
import type { Notice } from "@/lib/decisionForm";

/**
 * Pause or Resume (M17, D6). Either is undone by the other, so neither asks
 * first. Only a failure is spelled out: the banner shows the rest.
 */
export function SwitchButton({ paused }: { paused: boolean }) {
  const [notice, submit, pending] = useActionState<Notice | null, FormData>(switchFromHeader, null);

  return (
    <form action={submit} className="switch">
      <input type="hidden" name="kind" value={paused ? "resume" : "pause"} />
      <button type="submit" disabled={pending} className={paused ? "primary" : undefined}>
        {paused ? "Resume" : "Pause"}
      </button>
      {notice && notice.tone !== "ok" && (
        <span className={`note ${notice.tone}`} role="status">
          {notice.message}
        </span>
      )}
    </form>
  );
}
