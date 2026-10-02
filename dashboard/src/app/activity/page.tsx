import { activityLine, type ActivityRow } from "@/lib/activity";
import { query } from "@/lib/db";
import { ago, when } from "@/lib/format";

export const dynamic = "force-dynamic";

/** How many entries the page shows (M17, D7). */
const SHOWN = 100;

/**
 * The audit log (M17, D7): every attempt to act, refusals included, and every
 * change to the owner's switches, newest first. Read as `web_reader`, which
 * may read `audit_log` and write nothing.
 */
export default async function ActivityPage() {
  const rows = await query<ActivityRow>(
    `
    SELECT a.id::text, a.at, a.kind, a.tool, a.dry_run, a.decision_id::text,
           a.message_id, a.reason, p.payload->>'title' AS title
      FROM audit_log a
      LEFT JOIN proposals p ON p.message_id = a.message_id
     ORDER BY a.at DESC, a.id DESC
     LIMIT $1
    `,
    [SHOWN],
  );

  return (
    <>
      <h1>Activity</h1>
      <p className="sub">
        The latest {SHOWN} entries in the audit log: every attempt to act, refusals included, and every
        change to the switches. The log holds no email content; a proposal&apos;s title is read from the
        proposal while it is kept.
      </p>

      {rows.length === 0 ? (
        <div className="empty">Nothing recorded yet.</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>What</th>
              <th>Proposal</th>
              <th>Decision</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const line = activityLine(row);
              return (
                <tr key={row.id}>
                  <td title={when(row.at)}>{ago(row.at)}</td>
                  <td>
                    <span className={line.tone ? `note ${line.tone}` : undefined}>{line.what}</span>
                  </td>
                  <td>{row.message_id ? (row.title ?? "(cleared)") : ""}</td>
                  <td className="mono muted">{row.decision_id ?? ""}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </>
  );
}
