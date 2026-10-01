import { redirect } from "next/navigation";
import { Pool } from "pg";
import { cache } from "react";

import { auth } from "@/auth";
import { isOwnerSession } from "@/lib/access";
import { databaseSsl } from "@/lib/tls";

/**
 * Server components query Postgres directly.
 *
 * No API layer in between: this is a single-user internal tool reading tables
 * the agent already owns, and an HTTP hop would add serialisation, a second
 * place for types to drift, and nothing else.
 *
 * The pool is cached on globalThis because Next's dev server re-evaluates
 * modules on every hot reload, and a fresh pool per reload exhausts Postgres
 * connections within a few minutes of editing. It is not exported: every read
 * goes through `query`, which checks the session.
 *
 * Every connection off this machine is encrypted (`src/lib/tls.ts`). An
 * `sslmode` in `DATABASE_URL` would override that, so the URL carries none.
 */
const globalForPg = globalThis as unknown as { pool?: Pool };

const pool =
  globalForPg.pool ??
  new Pool({
    connectionString: process.env.DATABASE_URL,
    ssl: databaseSsl(process.env.DATABASE_URL, process.env.DATABASE_CA_CERT),
    max: 4,
    idleTimeoutMillis: 30_000,
  });

if (process.env.NODE_ENV !== "production") globalForPg.pool = pool;

/** One session lookup per request, however many queries a page makes. */
const currentSession = cache(() => auth());

export async function query<T>(text: string, params: unknown[] = []): Promise<T[]> {
  // The proxy already gates every page. This check is the one that holds when
  // the proxy is skipped: Next documents that a matcher change or a moved
  // Server Function can bypass it, and any read would then be open.
  const session = await currentSession();
  if (!isOwnerSession(session?.user?.email, process.env.OWNER_EMAIL)) {
    redirect("/api/auth/signin");
  }

  const result = await pool.query(text, params);
  return result.rows as T[];
}

/** Postgres NUMERIC arrives as a string; parseFloat here, once, rather than at
 *  every call site where forgetting produces silent string concatenation. */
export function num(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  const parsed = typeof value === "number" ? value : Number.parseFloat(String(value));
  return Number.isFinite(parsed) ? parsed : null;
}
