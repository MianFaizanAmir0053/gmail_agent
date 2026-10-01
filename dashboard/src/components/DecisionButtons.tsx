"use client";

import { useActionState, useState } from "react";

import { decideFromCard } from "@/app/actions";
import { MAX_CORRECTION_CHARS, type Notice } from "@/lib/decisionForm";

/**
 * Confirm, Edit and Cancel for one card. The card's revision travels with
 * every decision, so a tap on a card that has since changed is refused by
 * Fly rather than applied to a version the owner has not seen.
 */
export function DecisionButtons({
  messageId,
  revision,
  canEdit,
}: {
  messageId: string;
  revision: number;
  canEdit: boolean;
}) {
  const [notice, submit, pending] = useActionState<Notice | null, FormData>(decideFromCard, null);
  const [editing, setEditing] = useState(false);

  return (
    <form action={submit} className="decide">
      <input type="hidden" name="message_id" value={messageId} />
      <input type="hidden" name="revision" value={revision} />

      {editing ? (
        <>
          <textarea
            name="correction"
            required
            maxLength={MAX_CORRECTION_CHARS}
            rows={2}
            placeholder='What should change? For example "4pm, not 3pm".'
          />
          <button type="submit" name="action" value="edit" disabled={pending} className="primary">
            Send edit
          </button>
          <button type="button" onClick={() => setEditing(false)} disabled={pending}>
            Back
          </button>
        </>
      ) : (
        <>
          <button type="submit" name="action" value="confirm" disabled={pending} className="primary">
            Confirm
          </button>
          {canEdit && (
            <button type="button" onClick={() => setEditing(true)} disabled={pending}>
              Edit
            </button>
          )}
          <button type="submit" name="action" value="cancel" disabled={pending}>
            Cancel
          </button>
        </>
      )}

      {notice && (
        <p className={`note ${notice.tone}`} role="status">
          {notice.message}
        </p>
      )}
    </form>
  );
}
