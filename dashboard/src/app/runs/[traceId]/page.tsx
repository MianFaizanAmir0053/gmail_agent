import Link from "next/link";
import { notFound } from "next/navigation";
import { num, query } from "@/lib/db";
import { cost, ms, tokens, when } from "@/lib/format";

export const dynamic = "force-dynamic";

type Run = {
  trace_id: string;
  gmail_message_id: string;
  status: string;
  started_at: string;
  ended_at: string | null;
  duration_ms: number | null;
  total_cost_usd: string | null;
  error: string | null;
};

type Span = {
  id: string;
  node: string;
  model: string | null;
  status: string;
  latency_ms: number;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  thinking_tokens: number;
  cost_usd: string | null;
  error: string | null;
  input_redacted: unknown;
};

export default async function TracePage({
  params,
}: {
  params: Promise<{ traceId: string }>;
}) {
  const { traceId } = await params;

  const [run] = await query<Run>("SELECT * FROM runs WHERE trace_id = $1", [traceId]);
  if (!run) notFound();

  const spans = await query<Span>(
    "SELECT * FROM spans WHERE trace_id = $1 ORDER BY started_at, id",
    [traceId],
  );

  const slowest = Math.max(...spans.map((s) => s.latency_ms), 1);

  return (
    <>
      <p className="sub" style={{ marginBottom: 6 }}>
        <Link href="/analytics">← Runs</Link>
      </p>
      <h1 style={{ fontFamily: "var(--mono)" }}>{run.gmail_message_id}</h1>
      <p className="sub">
        <span className={`pill ${run.status}`}>{run.status}</span> · {when(run.started_at)} ·{" "}
        {ms(num(run.duration_ms))} · {cost(num(run.total_cost_usd))}
      </p>

      {run.error && (
        <div style={{ marginBottom: 20 }}>
          <pre style={{ color: "var(--err)" }}>{run.error}</pre>
        </div>
      )}

      <div className="chart" style={{ marginBottom: 22 }}>
        {spans.map((span) => (
          <div className="trace-node" key={span.id}>
            <div className="mono" style={{ fontSize: 13 }}>
              {span.node}
            </div>
            <div>
              <div
                className={`bar ${span.status === "error" ? "error" : ""}`}
                style={{ width: `${Math.max((span.latency_ms / slowest) * 100, 1)}%` }}
              />
            </div>
            <div
              className="mono"
              style={{ textAlign: "right", fontSize: 12, color: "var(--muted)" }}
            >
              {ms(span.latency_ms)}
            </div>
          </div>
        ))}
      </div>

      <table>
        <thead>
          <tr>
            <th>Node</th>
            <th>Model</th>
            <th className="right">In</th>
            <th className="right">Out</th>
            <th className="right">Think</th>
            <th className="right">Cached</th>
            <th className="right">Cost</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {spans.map((span) => (
            <tr key={span.id}>
              <td className="mono">{span.node}</td>
              <td className="mono" style={{ color: "var(--muted)", fontSize: 12 }}>
                {span.model ?? "—"}
              </td>
              <td className="right mono">{tokens(span.input_tokens)}</td>
              <td className="right mono">{tokens(span.output_tokens)}</td>
              <td className="right mono">{tokens(span.thinking_tokens)}</td>
              <td className="right mono">{tokens(span.cached_tokens)}</td>
              <td className="right mono">{span.model ? cost(num(span.cost_usd)) : "—"}</td>
              <td>
                <span className={`pill ${span.status}`}>{span.status}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {spans.some((s) => s.error) && (
        <>
          <h1 style={{ marginTop: 28, fontSize: 15 }}>Errors</h1>
          {spans
            .filter((s) => s.error)
            .map((span) => (
              <pre key={span.id} style={{ marginBottom: 10, color: "var(--err)" }}>
                {span.node}: {span.error}
              </pre>
            ))}
        </>
      )}

      <h1 style={{ marginTop: 28, fontSize: 15 }}>Payloads</h1>
      <p className="sub">
        Redacted at write time — addresses, phone numbers, and URLs never reach the database.
      </p>
      <pre>{JSON.stringify(spans.map((s) => ({ [s.node]: s.input_redacted })), null, 2)}</pre>
    </>
  );
}
