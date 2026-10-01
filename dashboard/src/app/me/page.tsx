import Link from "next/link";
import { redirect } from "next/navigation";

import { auth, signOut } from "@/auth";
import { isOwnerSession, isPairingEnabled } from "@/lib/access";

/**
 * Who is signed in. Reads no database, so it works on a deployment that has
 * none yet: this is the page the day-1 phone test opens.
 *
 * Like every page, it requires the owner's session itself rather than trust
 * the proxy alone. Signing out is the one action that needs none: it ends
 * the caller's own session and nothing else.
 */
export default async function MePage() {
  const session = await auth();
  if (!isOwnerSession(session?.user?.email, process.env.OWNER_EMAIL)) {
    redirect("/api/auth/signin");
  }

  async function signOutAction() {
    "use server";
    await signOut({ redirectTo: "/me" });
  }

  return (
    <main>
      <h1>Signed in</h1>
      <p>{session?.user?.email}</p>
      {isPairingEnabled(process.env.PAIRING_ENABLED) && (
        <p>
          <Link href="/pair" prefetch={false}>
            Pair the installed iPhone app
          </Link>
        </p>
      )}
      <form action={signOutAction}>
        <button type="submit">Sign out</button>
      </form>
    </main>
  );
}
