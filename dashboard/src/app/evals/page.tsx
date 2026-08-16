import { Bars } from "@/components/Bars";
import { num, query } from "@/lib/db";
import { pct, when } from "@/lib/format";

export const dynamic = "force-dynamic";

/**
 * Eval history comes from Postgres, published by `app.eval.publish`.
 *
 * An earlier version read `../results/*.json` directly. Turbopack correctly
 * objected -- a filesystem path escaping the project traces the whole
 * repository into the deployment -- and the deeper problem was worse: a
 * separately deployed dashboard has no repository to read. The JSON files are
 * still the committed evidence; this is a published copy for querying.
 */
type EvalRun = {
  id: string;
  ran_at: string;
  extractor: string;
  fixtures: number;
  exact_match: string;
  is_meeting_f1: string | null;
  attendees_f1: string | null;
  fields: Record<string, { accuracy: number; correct: number; total: number }>;
  failures: { id: string; tags: string[]; wrong_fields: string[] }[];
};

export default async function EvalsPage() {
  const runs = await query<EvalRun>(
    "SELECT * FROM eval_runs ORDER BY ran_at ASC, id ASC LIMIT 60",
  );

  const real = runs.filter((r) => !r.extractor.startsWith("always_"));
  const floor = runs.find((r) => r.extractor === "always_no");
  const latest = real.at(-1);

  return (
    <>
      <h1>Evaluation</h1>
      <p className="sub">
        Exact match means every applicable field correct. A half-right calendar event is still a
        wrong calendar event to whoever has to attend it.
      </p>

      <div className="cards">
        <Card label="Latest" value={pct(num(latest?.exact_match ?? null))} />
        <Card label="Majority-class floor" value={pct(num(floor?.exact_match ?? null))} />
        <Card label="Fixtures" value={String(latest?.fixtures ?? 0)} />
        <Card label="Runs recorded" value={String(runs.length)} />
      </div>

      {runs.length === 0 ? (
        <div className="empty">
          No eval history published. Run <code>.\tasks.ps1 eval --extractor gemini</code>, then{" "}
          <code>python -m app.eval.publish</code>.
        </div>
      ) : (
        <>
          <div className="chart" style={{ marginBottom: 26 }}>
            <div
              className="label"
              style={{ color: "var(--muted)", fontSize: 11, marginBottom: 12 }}
            >
              EXACT MATCH OVER TIME
            </div>
            <Bars
              points={runs.map((run) => ({
                label: run.extractor.replace("always_", "≡"),
                value: num(run.exact_match),
                title: `${run.extractor} — ${pct(num(run.exact_match))} on ${run.fixtures} fixtures (${when(run.ran_at)})`,
              }))}
              format={(v) => pct(v)}
              color="var(--ok)"
            />
          </div>

          <h1 style={{ fontSize: 15 }}>All runs</h1>
          <table style={{ marginBottom: 26 }}>
            <thead>
              <tr>
                <th>When</th>
                <th>Extractor</th>
                <th className="right">Fixtures</th>
                <th className="right">Exact match</th>
                <th className="right">is_meeting F1</th>
              </tr>
            </thead>
            <tbody>
              {[...runs].reverse().map((run) => (
                <tr key={run.id}>
                  <td>{when(run.ran_at)}</td>
                  <td className="mono">{run.extractor}</td>
                  <td className="right mono">{run.fixtures}</td>
                  <td className="right mono">{pct(num(run.exact_match))}</td>
                  <td className="right mono">{pct(num(run.is_meeting_f1))}</td>
                </tr>
              ))}
            </tbody>
          </table>

          {latest && (
            <>
              <h1 style={{ fontSize: 15 }}>Latest run, by field</h1>
              <table style={{ marginBottom: 22 }}>
                <thead>
                  <tr>
                    <th>Field</th>
                    <th className="right">Accuracy</th>
                    <th className="right">Correct</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(latest.fields).map(([field, stats]) => (
                    <tr key={field}>
                      <td className="mono">{field}</td>
                      <td className="right mono">{pct(stats.accuracy)}</td>
                      <td className="right mono">
                        {stats.correct}/{stats.total}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>

              {latest.failures.length > 0 && (
                <>
                  <h1 style={{ fontSize: 15 }}>Remaining failures</h1>
                  <table>
                    <thead>
                      <tr>
                        <th>Fixture</th>
                        <th>Tags</th>
                        <th>Wrong fields</th>
                      </tr>
                    </thead>
                    <tbody>
                      {latest.failures.map((failure) => (
                        <tr key={failure.id}>
                          <td className="mono">{failure.id}</td>
                          <td style={{ color: "var(--muted)", fontSize: 12 }}>
                            {failure.tags.join(", ")}
                          </td>
                          <td className="mono" style={{ color: "var(--warn)" }}>
                            {failure.wrong_fields.join(", ")}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </>
              )}
            </>
          )}
        </>
      )}

      <p className="note">
        The floor matters as much as the headline: most mail is not a meeting, so an extractor that
        never says yes already scores respectably while being useless.
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
