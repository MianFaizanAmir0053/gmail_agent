"use client";

import { useActionState, useState } from "react";

import { askFromChat } from "@/app/actions";
import {
  afterNotice,
  type ChatNotice,
  chatParts,
  inputKind,
  keyToSend,
  remarkInput,
  type Sent,
} from "@/lib/chat";

/**
 * The request box (M19, D1 and D5). Beside its text it keeps a mark for each
 * character: whether it arrived as typing, by the input event's own type and
 * text (`remarkInput`). The request is sent as runs of typed and pasted
 * text, so the planner reads only what was typed.
 *
 * The page makes the request's key at the first send (`keyToSend`). A resend
 * of the same words keeps it, so a send that timed out finds the turn already
 * stored; other words get a key of their own. The box stays locked while a
 * turn is open.
 */
export function ChatBox({ locked }: { locked: boolean }) {
  const [text, setText] = useState("");
  const [marks, setMarks] = useState<boolean[]>([]);
  const [sent, setSent] = useState<Sent>(null);
  const [notice, submit, pending] = useActionState<ChatNotice | null, FormData>(
    askFromChat,
    null,
  );
  const [seen, setSeen] = useState<ChatNotice | null>(null);

  // A new answer from Fly: once the turn is stored, or the key refused, the
  // next send is a new request. Adjusted while rendering, as React advises,
  // rather than in an effect.
  if (notice !== seen) {
    setSeen(notice);
    setSent(afterNotice(sent, notice));
    if (notice?.tone === "ok") {
      setText("");
      setMarks([]);
    }
  }

  function ask(data: FormData) {
    const parts = JSON.stringify(chatParts(text, marks));
    const sending = keyToSend(sent, parts, () => crypto.randomUUID());
    setSent(sending.sent);
    data.set("key", sending.key);
    data.set("parts", parts);
    submit(data);
  }

  const disabled = locked || pending;
  return (
    <form action={ask} className="chatbox">
      <textarea
        aria-label="Your request"
        value={text}
        disabled={disabled}
        rows={3}
        maxLength={18_000}
        // No form restore on reload or Back: restored text arrives with no
        // event, and would otherwise sit in the box unmarked.
        autoComplete="off"
        placeholder="Am I free tomorrow at 3pm?"
        onChange={(event) => {
          const after = event.target.value;
          const native = event.nativeEvent as InputEvent;
          setMarks(
            remarkInput(
              text,
              marks,
              after,
              inputKind(native),
              event.target.selectionEnd,
              native.data,
            ),
          );
          setText(after);
        }}
      />
      <div className="chatbox-row">
        <button type="submit" disabled={disabled || !text.trim()}>
          Ask
        </button>
        {locked && <span className="muted">Waiting for the answer…</span>}
        {notice && (
          <span className={`note ${notice.tone}`} role="status">
            {notice.message}
          </span>
        )}
      </div>
    </form>
  );
}
