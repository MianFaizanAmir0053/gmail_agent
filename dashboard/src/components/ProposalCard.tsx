import type { ReactNode } from "react";

import { AllowGuest } from "@/components/AllowGuest";
import { WithdrawButton } from "@/components/WithdrawButton";
import type { CardView } from "@/lib/timeline";

/**
 * One proposal, as the owner decides it: what, when in their own zone, with
 * whom, and anything that should give them pause. `children` holds the
 * decision buttons.
 */
export function ProposalCard({ view, children }: { view: CardView; children?: ReactNode }) {
  return (
    <article className={`proposal${view.applying ? " applying" : ""}`}>
      <header>
        <h2>{view.title}</h2>
        <div className="badges">
          {view.live ? (
            <span className="pill live">live</span>
          ) : (
            <span className="pill dry-run">dry run</span>
          )}
          <span className="pill">{view.invite ? "invite" : "hold"}</span>
          <span className="pill">revision {view.revision}</span>
        </div>
      </header>

      <p className="when">{view.when}</p>
      {view.eventZone && <p className="detail">Event zone: {view.eventZone}</p>}
      {view.attendees.length > 0 && <p className="detail">With {view.attendees.join(", ")}</p>}
      {view.outsideGuests.map((guest) => (
        <div key={guest} className="note warn outside">
          <span>{guest} is not in this email thread.</span>
          {view.canDecide && <AllowGuest messageId={view.messageId} address={guest} />}
        </div>
      ))}
      {view.location && <p className="detail">At {view.location}</p>}

      {view.conflicts.map((conflict) => (
        <p key={conflict} className="note warn">
          ⚠ {conflict}
        </p>
      ))}
      {view.reviewIssues.map((issue) => (
        <p key={issue} className="note warn">
          Reviewer: {issue}
        </p>
      ))}

      <footer>
        {view.applying ? (
          <>
            <span className="applying-label">{view.withdrawing ? "Withdrawing…" : "Applying…"}</span>
            {view.held && <p className="note warn held">{view.held}</p>}
            {view.withdrawDeclined && (
              <p className="note warn held">Already being applied: it can no longer be withdrawn.</p>
            )}
            {view.canWithdraw && view.decisionId && <WithdrawButton decisionId={view.decisionId} />}
          </>
        ) : (
          children
        )}
      </footer>
    </article>
  );
}
