import { DecisionButtons } from "@/components/DecisionButtons";
import { ProposalCard } from "@/components/ProposalCard";
import { PushSetup } from "@/components/PushSetup";
import { Refresher } from "@/components/Refresher";
import { StableTaps } from "@/components/StableTaps";
import { ago } from "@/lib/format";
import { allowedContacts, openProposals, recentDecisions, switchesOrNull } from "@/lib/proposals";
import { NO_SWITCHES } from "@/lib/switches";
import {
  cardView,
  footerKey,
  guestKeys,
  layoutKey,
  ownerZone,
  refreshEvery,
  sourcesKey,
} from "@/lib/timeline";

export const dynamic = "force-dynamic";

/**
 * The timeline (M16, D5): what is waiting for the owner, then what was
 * decided. Decisions are queued and applied by the worker on Fly, so a card
 * being decided shows "Applying…" and the page re-reads until it settles.
 *
 * Nothing above the cards appears late: the push set-up sits below them, so
 * its arrival after load cannot push a card's buttons out from under a tap.
 */
export default async function TimelinePage() {
  const zone = ownerZone(process.env.OWNER_TIMEZONE);
  // The switches only explain why a card waits: unreadable, the timeline
  // still shows, without those notes.
  const [{ rows: open, total }, decisions, read] = await Promise.all([
    openProposals(),
    recentDecisions(),
    switchesOrNull(),
  ]);
  const state = read ?? NO_SWITCHES;
  const allowed = await allowedContacts(guestKeys(open));

  return (
    <>
      <Refresher every={refreshEvery(open, state)} />

      <h1>Waiting for you</h1>
      <p className="sub">Times are shown in {zone}.</p>

      {open.length === 0 ? (
        <div className="empty">Nothing is waiting for a decision.</div>
      ) : (
        <StableTaps
          layoutKey={layoutKey(
            open.map((row) => {
              const view = cardView(row, zone, allowed, state);
              return {
                ...row,
                outside: view.outsideGuests.length,
                footer: footerKey(view),
                sources: sourcesKey(view),
              };
            }),
          )}
        >
          <div className="proposals">
            {open.map((row) => {
              const view = cardView(row, zone, allowed, state);
              return (
                <ProposalCard key={row.message_id} view={view}>
                  {view.canDecide && (
                    <DecisionButtons
                      messageId={view.messageId}
                      revision={view.revision}
                      canEdit={view.canEdit}
                      token={view.token}
                    />
                  )}
                </ProposalCard>
              );
            })}
          </div>
        </StableTaps>
      )}
      {total > open.length && (
        <p className="sub">
          Showing the newest {open.length} of {total}. The rest appear as these are decided.
        </p>
      )}

      {/* Trimmed: Vercel keeps a line break pasted with the value. */}
      <PushSetup publicKey={process.env.NEXT_PUBLIC_VAPID_PUBLIC_KEY?.trim() || null} />

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
