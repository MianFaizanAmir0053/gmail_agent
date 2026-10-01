import NextAuth from "next-auth";
import Credentials from "next-auth/providers/credentials";
import Google from "next-auth/providers/google";

import {
  admitsSignInAttempt,
  isExemptPath,
  isOwnerSession,
  isPairingCode,
  isPairingEnabled,
  PAIRING_PROVIDER_ID,
} from "@/lib/access";
import { redeemPairingCode } from "@/lib/fly";

/**
 * Pairing (M16, D4): the fallback for the installed iPhone app, should
 * Google sign-in fail inside it. It is a provider only while
 * `PAIRING_ENABLED` is `true`; Auth.js's own sign-in page then shows a code
 * box under the Google button. The owner gets the code from `/pair`, on a
 * device already signed in, and Fly decides whether it is the live one.
 */
const pairingEnabled = isPairingEnabled(process.env.PAIRING_ENABLED);

const pairing = Credentials({
  id: PAIRING_PROVIDER_ID,
  name: "Pairing code",
  credentials: {
    code: {
      label: "Pairing code",
      type: "text",
      inputMode: "numeric",
      autoComplete: "one-time-code",
      pattern: "[0-9]{6}",
      maxLength: 6,
      required: true,
    },
  },
  async authorize(credentials) {
    // A blank OWNER_EMAIL admits nobody, so no code is spent learning that.
    const owner = process.env.OWNER_EMAIL?.trim();
    if (!owner || !isPairingCode(credentials.code)) {
      return null;
    }
    return (await redeemPairingCode(credentials.code)) ? { email: owner, name: "Owner" } : null;
  },
});

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
 * `AUTH_SECRET`, which signs out every device, a paired one included.
 */
export const { handlers, auth, signIn, signOut } = NextAuth({
  providers: pairingEnabled ? [Google, pairing] : [Google],
  callbacks: {
    signIn({ user, account, profile }) {
      // Google's rule reads only Google's profile, exactly as before pairing.
      // A pairing sign-in has no profile: `authorize` above names the owner
      // once Fly has redeemed the code.
      return admitsSignInAttempt(
        { provider: account?.provider, profile, email: user.email },
        process.env.OWNER_EMAIL,
        pairingEnabled,
      );
    },
    authorized({ auth: session, request }) {
      return (
        isExemptPath(request.nextUrl.pathname) ||
        isOwnerSession(session?.user?.email, process.env.OWNER_EMAIL)
      );
    },
  },
});
