import { auth } from "@/auth";
import { isOwnerSession } from "@/lib/access";
import { switchesOrNull } from "@/lib/proposals";

export const dynamic = "force-dynamic";

/**
 * The owner's switches (M17, D5 and D6), for the header to read again after
 * a navigation. The header sits in the root layout, which a navigation does
 * not render again, so a pause made elsewhere would otherwise not show until
 * a reload.
 *
 * The owner is checked here as well as in the proxy, and the read itself
 * checks the session again (`src/lib/db.ts`).
 */
export async function GET(): Promise<Response> {
  const session = await auth();
  if (!isOwnerSession(session?.user?.email, process.env.OWNER_EMAIL)) {
    return new Response(null, { status: 401 });
  }
  const state = await switchesOrNull();
  if (state === null) return new Response(null, { status: 503 });
  return Response.json(state, { headers: { "Cache-Control": "no-store" } });
}
