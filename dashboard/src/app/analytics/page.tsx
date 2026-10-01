import Link from "next/link";
import { num, query } from "@/lib/db";
import { ago, cost, ms } from "@/lib/format";

export const dynamic = "force-dynamic";

type Run = {
  trace_id: string;
  gmail_message_id: string;
  status: string;
  started_at: string;
  duration_ms: number | null;
  total_cost_usd: string | null;
  error: string | null;
  spans: number;
};

type Totals = {
  runs: string;
  successes: string;
  failures: string;
  awaiting: string;
  cost: string | null;
};

export default async function RunsPage({
  searchParams,
}: {
  searchParams: Promise<{ status?: string }>;
}) {
  const { status } = await searchParams;

  const [totals] = await query<Totals>(`
    SELECT count(*) AS runs,
           count(*) FILTER (WHERE status = 'success') AS successes,
           count(*) FILTER (WHERE status = 'failed') AS failures,
           count(*) FILTER (WHERE status = 'awaiting_approval') AS awaiting,
           sum(total_cost_usd) AS cost
      FROM runs
     WHERE started_at > now() - interval '30 days'
  `);

  const runs = await query<Run>(
    `
    SELECT r.trace_id, r.gmail_message_id, r.status, r.started_at, r.duration_ms,
           r.total_cost_usd, r.error,
           (SELECT count(*) FROM spans s WHERE s.trace_id = r.trace_id) AS spans
      FROM runs r
     WHERE ($1::text IS NULL OR r.status = $1)
     ORDER BY r.started_at DESC
     LIMIT 100
    `,
    [status ?? null],
  );

  const filters = ["all", "success", "awaiting_approval", "failed"];

  return (
    <>
      <h1>Runs</h1>
      <p className="sub">One row per message processed. Last 100.</p>

      <div className="cards">
        <Stat label="Runs (30d)" value={totals?.runs ?? "0"} />
        <Stat label="Succeeded" value={totals?.successes ?? "0"} />
        <Stat label="Awaiting" value={totals?.awaiting ?? "0"} />
        <Stat label="Failed" value={totals?.failures ?? "0"} />
        <Stat label="Cost (30d)" value={cost(num(totals?.cost))} />
      </div>

      <div style={{ marginBottom: 14, display: "flex", gap: 6 }}>
        {filters.map((filter) => {
          const active = (status ?? "all") === filter;
          return (
            <Link
              key={filter}
              href={filter === "all" ? "/analytics" : `/analytics?status=${filter}`}
              className="pill"
              style={{
                borderColor: active ? "var(--accent)" : "var(--border)",
                color: active ? "var(--accent)" : "var(--muted)",
              }}
            >
              {filter}
            </Link>
          );
        })}
      </div>

      {runs.length === 0 ? (
        <div className="empty">
          No runs yet. Process some mail with <code>.\tasks.ps1 poll</code>.
        </div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Started</th>
              <th>Message</th>
              <th>Status</th>
              <th className="right">Nodes</th>
              <th className="right">Duration</th>
              <th className="right">Cost</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.trace_id}>
                <td>{ago(run.started_at)}</td>
                <td className="mono">
                  <Link href={`/runs/${run.trace_id}`}>{run.gmail_message_id}</Link>
                </td>
                <td>
                  <span className={`pill ${run.status}`}>{run.status}</span>
                </td>
                <td className="right mono">{run.spans}</td>
                <td className="right mono">{ms(num(run.duration_ms))}</td>
                <td className="right mono">{cost(num(run.total_cost_usd))}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="card">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
    </div>
  );
}
