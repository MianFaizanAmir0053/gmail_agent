import { Bars } from "@/components/Bars";
import { num, query } from "@/lib/db";
import { cost, ms, tokens } from "@/lib/format";

export const dynamic = "force-dynamic";

type Day = { day: string; runs: string; cost: string | null };
type NodeRow = {
  node: string;
  model: string | null;
  calls: string;
  avg_ms: string | null;
  p95_ms: string | null;
  input_tokens: string;
  output_tokens: string;
  thinking_tokens: string;
  cached_tokens: string;
  cost: string | null;
};
type Unpriced = { model: string; spans: string };

export default async function CostsPage() {
  const days = await query<Day>(`
    SELECT to_char(date_trunc('day', started_at), 'DD Mon') AS day,
           count(*) AS runs,
           sum(total_cost_usd) AS cost
      FROM runs
     WHERE started_at > now() - interval '14 days'
     GROUP BY date_trunc('day', started_at)
     ORDER BY date_trunc('day', started_at)
  `);

  const nodes = await query<NodeRow>(`
    SELECT node, max(model) AS model, count(*) AS calls,
           round(avg(latency_ms)) AS avg_ms,
           round(percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)) AS p95_ms,
           sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
           sum(thinking_tokens) AS thinking_tokens, sum(cached_tokens) AS cached_tokens,
           sum(cost_usd) AS cost
      FROM spans
     WHERE started_at > now() - interval '30 days'
     GROUP BY node
     ORDER BY sum(cost_usd) DESC NULLS LAST, count(*) DESC
  `);

  // Spans whose model has no rate record NULL, so they are absent from every
  // total rather than counted as free. Surfacing them is the difference between
  // a number that is complete and one that merely looks complete.
  const unpriced = await query<Unpriced>(`
    SELECT model, count(*) AS spans
      FROM spans
     WHERE model IS NOT NULL AND cost_usd IS NULL
     GROUP BY model ORDER BY count(*) DESC
  `);

  const [totals] = await query<{ cost: string | null; runs: string }>(`
    SELECT sum(total_cost_usd) AS cost, count(*) AS runs
      FROM runs WHERE started_at > now() - interval '30 days'
  `);

  const totalCost = num(totals?.cost) ?? 0;
  const runCount = Number(totals?.runs ?? 0);
  const perHundred = runCount > 0 ? (totalCost / runCount) * 100 : null;

  return (
    <>
      <h1>Costs</h1>
      <p className="sub">Derived from stored token counts, so history survives a rate change.</p>

      <div className="cards">
        <Card label="Spend (30d)" value={cost(totalCost)} />
        <Card label="Runs (30d)" value={String(runCount)} />
        <Card label="Per 100 emails" value={perHundred === null ? "—" : cost(perHundred)} />
      </div>

      <div className="chart" style={{ marginBottom: 26 }}>
        <div className="label" style={{ color: "var(--muted)", fontSize: 11, marginBottom: 12 }}>
          SPEND PER DAY (14d)
        </div>
        <Bars
          points={days.map((d) => ({
            label: d.day,
            value: num(d.cost) ?? 0,
            title: `${d.day}: ${cost(num(d.cost))} across ${d.runs} runs`,
          }))}
          format={(v) => cost(v)}
        />
      </div>

      <h1 style={{ fontSize: 15 }}>By node</h1>
      <p className="sub">Where the money and the latency actually go.</p>

      {nodes.length === 0 ? (
        <div className="empty">No spans recorded yet.</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Node</th>
              <th>Model</th>
              <th className="right">Calls</th>
              <th className="right">Avg</th>
              <th className="right">p95</th>
              <th className="right">In</th>
              <th className="right">Out</th>
              <th className="right">Think</th>
              <th className="right">Cost</th>
            </tr>
          </thead>
          <tbody>
            {nodes.map((node) => (
              <tr key={node.node}>
                <td className="mono">{node.node}</td>
                <td className="mono" style={{ color: "var(--muted)", fontSize: 12 }}>
                  {node.model ?? "—"}
                </td>
                <td className="right mono">{node.calls}</td>
                <td className="right mono">{ms(num(node.avg_ms))}</td>
                <td className="right mono">{ms(num(node.p95_ms))}</td>
                <td className="right mono">{tokens(num(node.input_tokens))}</td>
                <td className="right mono">{tokens(num(node.output_tokens))}</td>
                <td className="right mono">{tokens(num(node.thinking_tokens))}</td>
                <td className="right mono">{node.model ? cost(num(node.cost)) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {unpriced.length > 0 && (
        <p className="note warn">
          ⚠ Unpriced models in traces:{" "}
          {unpriced.map((u) => `${u.model} (${u.spans} spans)`).join(", ")}. Their spans are
          excluded from every total above — add rates to <code>app/obs/pricing.py</code>.
        </p>
      )}

      <p className="note">
        Rates are hand-entered and are not reported by the API. Reconcile against the provider&apos;s
        billing page before quoting these figures anywhere.
      </p>
    </>
  );
}

function Card({ label, value }: { label: string; value: string }) {
  return (
    <div className="card">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
    </div>
  );
}
