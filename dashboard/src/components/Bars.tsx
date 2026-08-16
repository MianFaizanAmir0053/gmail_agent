/**
 * Charts are hand-rolled SVG rendered on the server.
 *
 * A charting library would pull a client bundle and a hydration boundary for
 * two charts that are, structurally, a list of rectangles. This renders as part
 * of the HTML, needs no JavaScript in the browser at all, and there is no
 * version of it that breaks because a dependency changed its API.
 */

type Point = { label: string; value: number | null; title?: string };

export function Bars({
  points,
  height = 150,
  format = (v: number) => v.toFixed(2),
  color = "var(--accent)",
}: {
  points: Point[];
  height?: number;
  format?: (value: number) => string;
  color?: string;
}) {
  if (points.length === 0) {
    return <p className="note">No data yet.</p>;
  }

  const values = points.map((p) => p.value ?? 0);
  const max = Math.max(...values, 0.000001);
  const width = 100 / points.length;

  return (
    <div>
      <svg
        viewBox={`0 0 100 ${height}`}
        preserveAspectRatio="none"
        style={{ width: "100%", height, display: "block" }}
        role="img"
      >
        {points.map((point, index) => {
          const value = point.value ?? 0;
          const barHeight = Math.max((value / max) * (height - 18), value > 0 ? 2 : 0);
          return (
            <rect
              key={point.label}
              x={index * width + width * 0.18}
              y={height - barHeight}
              width={width * 0.64}
              height={barHeight}
              fill={color}
              rx={0.6}
            >
              <title>{point.title ?? `${point.label}: ${format(value)}`}</title>
            </rect>
          );
        })}
      </svg>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: `repeat(${points.length}, 1fr)`,
          marginTop: 8,
        }}
      >
        {points.map((point) => (
          <div
            key={point.label}
            style={{
              textAlign: "center",
              fontSize: 10,
              color: "var(--muted)",
              overflow: "hidden",
              whiteSpace: "nowrap",
            }}
          >
            {point.label}
          </div>
        ))}
      </div>
    </div>
  );
}
