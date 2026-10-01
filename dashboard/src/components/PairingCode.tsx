"use client";

import { useActionState } from "react";

import { showPairingCode, type PairingAnswer } from "@/app/pair/actions";

/**
 * Asks Fly for a pairing code and shows it. Every tap issues a new code and
 * ends the one before it, so the code on screen is always the live one.
 */
export function PairingCode() {
  const [answer, ask, pending] = useActionState<PairingAnswer | null, FormData>(showPairingCode, null);

  return (
    <form action={ask} className="pairing">
      {answer?.ok && (
        <>
          <p className="pairing-code mono" aria-label="Pairing code">
            {answer.code}
          </p>
          <p className="note">
            Valid until {answer.validUntil}. It works once, and five wrong tries end it.
          </p>
        </>
      )}
      {answer && !answer.ok && (
        <p className="note error" role="status">
          {answer.message}
        </p>
      )}
      <button type="submit" disabled={pending}>
        {answer?.ok ? "Show a new code" : "Show a pairing code"}
      </button>
    </form>
  );
}
