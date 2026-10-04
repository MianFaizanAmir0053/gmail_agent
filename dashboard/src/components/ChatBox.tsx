"use client";

import { useActionState, useRef, useState } from "react";

import { askFromChat } from "@/app/actions";
import { chatParts, remark } from "@/lib/chat";
import type { Notice } from "@/lib/decisionForm";

/**
 * The request box (M19, D1 and D5). Beside its text it keeps a mark for each
 * character: whether it arrived by a paste or a drop. The request is sent as
 * runs of typed and pasted text, so the planner reads only what was typed.
 *
 * The page makes the request's key. It is kept until Fly has the turn, so a
 * send that timed out and is sent again finds the turn already stored. The
 * box stays locked while a turn is open.
 */
export function ChatBox({ locked }: { locked: boolean }) {
  const [text, setText] = useState("");
  const [marks, setMarks] = useState<boolean[]>([]);
  const [key, setKey] = useState("");
  const [notice, submit, pending] = useActionState<Notice | null, FormData>(askFromChat, null);
  const [seen, setSeen] = useState<Notice | null>(null);
  const pastedNext = useRef(false);

  // A new answer from Fly: once the turn is stored, the box empties for the
  // next request, which gets a key of its own. Adjusted while rendering, as
  // React advises, rather than in an effect.
  if (notice !== seen) {
    setSeen(notice);
    if (notice?.tone === "ok") {
      setText("");
      setMarks([]);
      setKey("");
    }
  }

  function ask(data: FormData) {
    // Made at the first send, in the browser, and kept for any resend.
    const sending = key || crypto.randomUUID();
    if (!key) setKey(sending);
    data.set("key", sending);
    data.set("parts", JSON.stringify(chatParts(text, marks)));
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
        placeholder="Am I free tomorrow at 3pm?"
        onPaste={() => {
          pastedNext.current = true;
        }}
        onDrop={() => {
          pastedNext.current = true;
        }}
        onChange={(event) => {
          const after = event.target.value;
          setMarks(remark(text, marks, after, pastedNext.current));
          pastedNext.current = false;
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
