import { ProposalCard } from "@/components/ProposalCard";
import { Refresher } from "@/components/Refresher";
import { ago } from "@/lib/format";
import { openProposals, recentDecisions } from "@/lib/proposals";
import { cardView, ownerZone, shouldRefresh } from "@/lib/timeline";

export const dynamic = "force-dynamic";

/**
 * The timeline (M16, D5): what is waiting for the owner, then what was
 * decided. Decisions are queued and applied by the worker on Fly, so a card
 * being decided shows "Applying…" and the page re-reads until it settles.
 */
export default async function TimelinePage() {
  const zone = ownerZone(process.env.OWNER_TIMEZONE);
  const [open, decisions] = await Promise.all([openProposals(), recentDecisions()]);

  return (
    <>
      <Refresher active={shouldRefresh(open)} />

      <h1>Waiting for you</h1>
      <p className="sub">Times are shown in {zone}.</p>

      {open.length === 0 ? (
        <div className="empty">Nothing is waiting for a decision.</div>
      ) : (
        <div className="proposals">
          {open.map((row) => (
            <ProposalCard key={row.message_id} view={cardView(row, zone)} />
          ))}
        </div>
      )}

      <h1 className="section">Recent decisions</h1>
      {decisions.length === 0 ? (
        <div className="empty">No decisions yet.</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>Proposal</th>
              <th>Decision</th>
              <th>From</th>
              <th>Outcome</th>
            </tr>
          </thead>
          <tbody>
            {decisions.map((decision) => (
              <tr key={decision.id}>
                <td>{ago(decision.decided_at)}</td>
                <td>
                  {decision.title ?? "(cleared)"}{" "}
                  <span className="mono muted">r{decision.revision}</span>
                </td>
                <td>{decision.action}</td>
                <td>{decision.via}</td>
                <td>
                  <span className={`pill ${outcomeClass(decision.outcome)}`}>
                    {decision.outcome ?? "applying"}
                  </span>
                  {decision.reason && <span className="muted"> {decision.reason}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

function outcomeClass(outcome: string | null): string {
  switch (outcome) {
    case null:
      return "running";
    case "created":
    case "skipped":
    case "reparked":
      return "ok";
    case "failed":
      return "failed";
    default:
      return "";
  }
}
