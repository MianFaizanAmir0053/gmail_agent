"use client";

import { useActionState } from "react";

import { withdrawFromCard } from "@/app/actions";
import { useSettling } from "@/components/StableTaps";
import type { Notice } from "@/lib/decisionForm";

/**
 * Withdraw on a queued card (M17, D6). The worker carries the request out
 * before anything else, even while paused, unless the decision is already
 * being applied; the card re-reads and says which.
 */
export function WithdrawButton({ decisionId }: { decisionId: string }) {
  const [notice, submit, pending] = useActionState<Notice | null, FormData>(withdrawFromCard, null);
  const settling = useSettling();

  return (
    <form action={submit} className="withdraw">
      <input type="hidden" name="decision_id" value={decisionId} />
      <button type="submit" disabled={pending || settling}>
        Withdraw
      </button>
      {notice && notice.tone !== "ok" && (
        <span className={`note ${notice.tone}`} role="status">
          {notice.message}
        </span>
      )}
    </form>
  );
}
