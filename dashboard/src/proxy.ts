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
  // Next's own build output never reaches the gate. The manifest, the service
  // worker and the icons do, and are exempted by the tested rule instead, so
  // the list of open paths lives in one place.
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
