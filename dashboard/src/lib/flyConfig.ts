/**
 * Fly's address and secret as the web app's server reads them, and how a
 * failed request to Fly is named in the log. No framework imports, so
 * `node --test` runs these directly.
 */

/**
 * `FLY_API_URL` and `WEB_API_SECRET`, trimmed, or null when either is
 * missing. Vercel's value box keeps a pasted line break, and a secret ending
 * in one makes every request fail: `fetch` refuses the header, and the log
 * says only TypeError.
 */
export function flyConfig(env: Record<string, string | undefined>): { base: string; secret: string } | null {
  const base = env.FLY_API_URL?.trim();
  const secret = env.WEB_API_SECRET?.trim();
  return base && secret ? { base, secret } : null;
}

/**
 * Why a request to Fly failed, for the log: the error's kind and, when it has
 * one, its code -- the network layer's (ECONNREFUSED, a certificate error)
 * or the error's own (ERR_INVALID_URL). Never the message, which can carry
 * the address.
 */
export function failureKind(error: unknown): string {
  if (!(error instanceof Error)) return "unknown error";
  const code = codeOf(error) ?? codeOf((error as Error & { cause?: unknown }).cause);
  return code ? `${error.name} (${code})` : error.name;
}

function codeOf(value: unknown): string | null {
  if (typeof value !== "object" || value === null || !("code" in value)) return null;
  const code = (value as { code: unknown }).code;
  return typeof code === "string" && /^[A-Z0-9_]+$/.test(code) ? code : null;
}
