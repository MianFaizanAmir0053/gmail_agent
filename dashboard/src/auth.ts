import NextAuth from "next-auth";
import Google from "next-auth/providers/google";

import { admitsSignIn, isExemptPath, isOwnerSession } from "@/lib/access";

/**
 * Owner-only Google sign-in.
 *
 * The Google client lives in its own Cloud project, published "In
 * production" and asking only for `openid email profile`. Sharing the Gmail
 * project would let a lapse to Testing refuse other accounts at Google's end,
 * and the allow-list here would never be exercised.
 *
 * Sessions are JWTs in a cookie, with no database behind them, so the proxy
 * can check one without reaching Postgres. Revoking a device means rotating
 * `AUTH_SECRET`, which signs out every device.
 */
export const { handlers, auth, signIn, signOut } = NextAuth({
  providers: [Google],
  callbacks: {
    signIn({ profile }) {
      return admitsSignIn(profile, process.env.OWNER_EMAIL);
    },
    authorized({ auth: session, request }) {
      return (
        isExemptPath(request.nextUrl.pathname) ||
        isOwnerSession(session?.user?.email, process.env.OWNER_EMAIL)
      );
    },
  },
});
