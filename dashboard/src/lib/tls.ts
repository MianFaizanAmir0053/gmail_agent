import type { ConnectionOptions } from "node:tls";

/**
 * TLS for the web app's database connection (M16, D5).
 *
 * Supabase accepts plaintext unless told otherwise, and node-postgres sends
 * plaintext unless asked. Without this, the owner's proposals and the
 * `web_reader` password would cross the internet in the clear.
 *
 * Supabase signs its certificates with its own CA. Given that CA
 * (`DATABASE_CA_CERT`), the server's certificate must chain to it. The host
 * name is not checked, as with libpq's `verify-ca`: the shared pooler serves
 * every project, and only Supabase holds that CA's key. Without the CA the
 * connection is still encrypted, as Fly's own connection is. A database on
 * this machine needs none of it.
 */
export function databaseSsl(url: string | undefined, ca: string | undefined): false | ConnectionOptions {
  if (!url) return false;
  if (isLocal(url)) return false;

  const pem = ca?.trim().replace(/\\n/g, "\n");
  if (!pem) return { rejectUnauthorized: false };
  return { ca: pem.endsWith("\n") ? pem : `${pem}\n`, checkServerIdentity: () => undefined };
}

function isLocal(url: string): boolean {
  try {
    return ["localhost", "127.0.0.1", "[::1]"].includes(new URL(url).hostname);
  } catch {
    return false; // unreadable: encrypt rather than guess
  }
}
