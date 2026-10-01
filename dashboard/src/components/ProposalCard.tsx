import type { ReactNode } from "react";

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
          {view.dryRun && <span className="pill dry-run">dry run</span>}
          <span className="pill">{view.invite ? "invite" : "hold"}</span>
          <span className="pill">revision {view.revision}</span>
        </div>
      </header>

      <p className="when">{view.when}</p>
      {view.eventZone && <p className="detail">Event zone: {view.eventZone}</p>}
      {view.attendees.length > 0 && <p className="detail">With {view.attendees.join(", ")}</p>}
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

      <footer>{view.applying ? <span className="applying-label">Applying…</span> : children}</footer>
    </article>
  );
}
