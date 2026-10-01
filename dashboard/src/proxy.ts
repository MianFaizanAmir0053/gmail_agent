/**
 * The sign-in gate, run before every request the matcher lets through.
 *
 * The rule itself is Auth.js's `authorized` callback in `src/auth.ts`: exempt
 * paths pass, and everything else needs the owner's session. On Vercel this
 * runs globally rather than in the function region, which is acceptable
 * because it reads only the session cookie and never proposal content.
 *
 * It is not the only check. Next documents that a matcher change or a moved
 * Server Function can skip the proxy, so every database read also verifies
 * the session (`src/lib/db.ts`).
 */
export { auth as proxy } from "@/auth";

export const config = {
  // Skipped entirely: Next's build output, and the files a phone fetches
  // without a session. Running the gate on those only refreshed the owner's
  // session cookie on responses anyone may fetch. The rule in
  // `src/lib/access.ts` still exempts them, and `/api/auth/*`, should the
  // matcher ever change; `src/lib/proxyMatcher.test.ts` holds the two
  // together. Next requires this to be a literal.
  matcher: [
    "/((?!_next/static/|favicon\\.ico$|icons/|sw\\.js$|manifest\\.webmanifest$).*)",
  ],
};
