import { auth, signOut } from "@/auth";

/**
 * Who is signed in. Reads no database, so it works on a deployment that has
 * none yet: this is the page the day-1 phone test opens.
 */
export default async function MePage() {
  const session = await auth();

  async function signOutAction() {
    "use server";
    await signOut({ redirectTo: "/me" });
  }

  return (
    <main>
      <h1>Signed in</h1>
      <p>{session?.user?.email ?? "No session."}</p>
      <form action={signOutAction}>
        <button type="submit">Sign out</button>
      </form>
    </main>
  );
}
