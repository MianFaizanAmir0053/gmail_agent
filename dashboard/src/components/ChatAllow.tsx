"use client";

import { useActionState } from "react";

import { allowFromChat } from "@/app/actions";
import type { Notice } from "@/lib/decisionForm";

/**
 * Allow for an address found in text the owner pasted (M19, D5). It is never
 * placed on a card: once allowed, it is a confirmed contact the owner can
 * name in a new request.
 */
export function ChatAllow({ address }: { address: string }) {
  const [notice, submit, pending] = useActionState<Notice | null, FormData>(allowFromChat, null);

  return (
    <form action={submit} className="allow">
      <input type="hidden" name="address" value={address} />
      <span className="mono">{address}</span>
      <button type="submit" disabled={pending}>
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
