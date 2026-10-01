"use client";

import { useActionState, useState } from "react";

import { decideFromCard } from "@/app/actions";
import { useSettling } from "@/components/StableTaps";
import { MAX_CORRECTION_CHARS, type Notice } from "@/lib/decisionForm";

/**
 * Confirm, Edit and Cancel for one card. The card's revision travels with
 * every decision, so a tap on a card that has since changed is refused by
 * Fly rather than applied to a version the owner has not seen.
 *
 * A card that changes keeps its edit box and the owner's words, beside the
 * notice saying it changed, so nothing typed is lost. The box closes for
 * good once no edit is left: the last revision offers only Confirm and
 * Cancel.
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
  // Held here rather than in the textarea: React resets a form's own fields
  // after every action, and a correction refused by Fly would be lost.
  const [correction, setCorrection] = useState("");
  const settling = useSettling();
  const locked = pending || settling;

  return (
    <form action={submit} className="decide">
      <input type="hidden" name="message_id" value={messageId} />
      <input type="hidden" name="revision" value={revision} />

      {editing && canEdit ? (
        <>
          <textarea
            name="correction"
            required
            maxLength={MAX_CORRECTION_CHARS}
            rows={2}
            value={correction}
            onChange={(event) => setCorrection(event.target.value)}
            placeholder='What should change? For example "4pm, not 3pm".'
          />
          <button type="submit" name="action" value="edit" disabled={locked} className="primary">
            Send edit
          </button>
          <button type="button" onClick={() => setEditing(false)} disabled={pending}>
            Back
          </button>
        </>
      ) : (
        <>
          <button type="submit" name="action" value="confirm" disabled={locked} className="primary">
            Confirm
          </button>
          {canEdit && (
            <button type="button" onClick={() => setEditing(true)} disabled={pending}>
              Edit
            </button>
          )}
          <button type="submit" name="action" value="cancel" disabled={locked}>
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
