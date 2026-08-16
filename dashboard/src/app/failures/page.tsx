import Link from "next/link";
import { num, query } from "@/lib/db";
import { ago, ms } from "@/lib/format";

export const dynamic = "force-dynamic";

type FailedRun = {
  trace_id: string;
  gmail_message_id: string;
  started_at: string;
  duration_ms: number | null;
  error: string | null;
  failed_node: string | null;
};

type NodeErrors = { node: string; errors: string; sample: string };

export default async function FailuresPage() {
  const runs = await query<FailedRun>(`
    SELECT r.trace_id, r.gmail_message_id, r.started_at, r.duration_ms, r.error,
           (SELECT s.node FROM spans s
             WHERE s.trace_id = r.trace_id AND s.status = 'error'
             ORDER BY s.started_at LIMIT 1) AS failed_node
      FROM runs r
     WHERE r.status = 'failed'
     ORDER BY r.started_at DESC
     LIMIT 50
  `);

  // Grouped by node so a recurring failure is obvious as a pattern rather than
  // as fifty individually plausible one-offs.
  const byNode = await query<NodeErrors>(`
    SELECT node, count(*) AS errors, min(error) AS sample
      FROM spans
     WHERE status = 'error' AND started_at > now() - interval '30 days'
     GROUP BY node ORDER BY count(*) DESC
  `);

  return (
    <>
      <h1>Failures</h1>
      <p className="sub">
        A failed run is not terminal — the ledger keeps it retryable, so fixing the cause and
        re-polling is enough.
      </p>

      {byNode.length > 0 && (
        <>
          <h1 style={{ fontSize: 15 }}>By node (30d)</h1>
          <table style={{ marginBottom: 26 }}>
            <thead>
              <tr>
                <th>Node</th>
                <th className="right">Errors</th>
                <th>Sample</th>
              </tr>
            </thead>
            <tbody>
              {byNode.map((row) => (
                <tr key={row.node}>
                  <td className="mono">{row.node}</td>
                  <td className="right mono">{row.errors}</td>
                  <td
                    className="mono"
                    style={{ color: "var(--err)", fontSize: 12, maxWidth: 560 }}
                  >
                    {row.sample?.slice(0, 160)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {runs.length === 0 ? (
        <div className="empty">No failed runs. </div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>Message</th>
              <th>Failed at</th>
              <th className="right">Duration</th>
              <th>Error</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.trace_id}>
                <td>{ago(run.started_at)}</td>
                <td className="mono">
                  <Link href={`/runs/${run.trace_id}`}>{run.gmail_message_id}</Link>
                </td>
                <td className="mono">{run.failed_node ?? "—"}</td>
                <td className="right mono">{ms(num(run.duration_ms))}</td>
                <td
                  className="mono"
                  style={{ color: "var(--err)", fontSize: 12, maxWidth: 460 }}
                >
                  {run.error?.slice(0, 140) ?? "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
