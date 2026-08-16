export function money(value: number | null): string {
  if (value === null) return "—";
  return value < 0.01 && value > 0 ? `$${value.toFixed(6)}` : `$${value.toFixed(4)}`;
}

/** Distinguishes "free" from "not priced".
 *
 *  An unpriced model records NULL rather than 0, so rendering it as $0.0000
 *  here would re-introduce exactly the lie the storage layer avoids. */
export function cost(value: number | null): string {
  return value === null ? "unpriced" : money(value);
}

export function ms(value: number | null): string {
  if (value === null) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(1)}s` : `${Math.round(value)}ms`;
}

export function tokens(value: number | null): string {
  if (!value) return "0";
  return value >= 1000 ? `${(value / 1000).toFixed(1)}k` : String(value);
}

export function when(value: Date | string | null): string {
  if (!value) return "—";
  const date = typeof value === "string" ? new Date(value) : value;
  return date.toLocaleString(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function ago(value: Date | string | null): string {
  if (!value) return "—";
  const date = typeof value === "string" ? new Date(value) : value;
  const seconds = (Date.now() - date.getTime()) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function pct(value: number | null): string {
  return value === null ? "—" : `${(value * 100).toFixed(1)}%`;
}
