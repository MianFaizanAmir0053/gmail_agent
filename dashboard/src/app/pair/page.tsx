import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { PairingCode } from "@/components/PairingCode";
import { isOwnerSession, isPairingEnabled } from "@/lib/access";

/**
 * Pairing (M16, D4): signs in the installed iPhone app if Google sign-in fails
 * inside it. The owner opens this page where they are already signed in,
 * shows a code, and types it into the app's sign-in page, under "Pairing
 * code". Reads no database: the code comes from Fly.
 *
 * Like every page, it requires the owner's session itself rather than trust
 * the proxy alone.
 */
export default async function PairPage() {
  const session = await auth();
  if (!isOwnerSession(session?.user?.email, process.env.OWNER_EMAIL)) {
    redirect("/api/auth/signin");
  }

  if (!isPairingEnabled(process.env.PAIRING_ENABLED)) {
    return (
      <main>
        <h1>Pairing is off</h1>
        <p className="sub">
          Pairing signs in the installed iPhone app if Google sign-in fails inside it. It is switched on only
          if that happens.
        </p>
      </main>
    );
  }

  return (
    <main>
      <h1>Pair the installed app</h1>
      <p className="sub">
        For the iPhone app, if Google sign-in does not work inside it. Show a code here, then open the app
        and type the code under &ldquo;Pairing code&rdquo;.
      </p>
      <PairingCode />
    </main>
  );
}
