"use client";

import { useActionState } from "react";

import { allowFromCard } from "@/app/actions";
import { useSettling } from "@/components/StableTaps";
import type { Notice } from "@/lib/decisionForm";

/**
 * Allow for one guest outside the thread (M17, D4). Kept until the owner
 * removes it, on the command line; the card re-reads, and the guest is no
 * longer marked.
 */
export function AllowGuest({ messageId, address }: { messageId: string; address: string }) {
  const [notice, submit, pending] = useActionState<Notice | null, FormData>(allowFromCard, null);
  const settling = useSettling();

  return (
    <form action={submit} className="allow">
      <input type="hidden" name="message_id" value={messageId} />
      <input type="hidden" name="address" value={address} />
      <button type="submit" disabled={pending || settling}>
        Allow
      </button>
      {notice && (
        <span className={`note ${notice.tone}`} role="status">
          {notice.message}
        </span>
      )}
    </form>
  );
}
