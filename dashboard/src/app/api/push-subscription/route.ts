import { auth } from "@/auth";
import { isOwnerSession } from "@/lib/access";
import { deleteSubscription, postSubscription } from "@/lib/fly";
import { endpointOf, subscriptionBody } from "@/lib/push";

/**
 * Where the page and the service worker post this browser's push
 * subscription, and where the page drops one it replaced. A route rather than
 * a server action, because the service worker -- re-subscribing after the
 * browser replaced a subscription -- can call a URL but not an action. It
 * forwards to Fly with the server's secret.
 *
 * The owner is checked here as well as in the proxy.
 */
export async function POST(request: Request): Promise<Response> {
  if (!(await isOwner())) return new Response(null, { status: 401 });

  const body = subscriptionBody(await request.json().catch(() => null));
  if (body === null) {
    return new Response(null, { status: 422 });
  }

  return new Response(null, { status: await postSubscription(body) });
}

export async function DELETE(request: Request): Promise<Response> {
  if (!(await isOwner())) return new Response(null, { status: 401 });

  const endpoint = endpointOf(await request.json().catch(() => null));
  if (endpoint === null) {
    return new Response(null, { status: 422 });
  }

  return new Response(null, { status: await deleteSubscription(endpoint) });
}

async function isOwner(): Promise<boolean> {
  const session = await auth();
  return isOwnerSession(session?.user?.email, process.env.OWNER_EMAIL);
}
