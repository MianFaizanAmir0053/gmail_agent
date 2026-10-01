import { auth } from "@/auth";
import { isOwnerSession } from "@/lib/access";
import { postSubscription } from "@/lib/fly";
import { subscriptionBody } from "@/lib/push";

/**
 * Where the page and the service worker post this browser's push
 * subscription. A route rather than a server action, because the service
 * worker -- re-subscribing after the browser replaced a subscription -- can
 * call a URL but not an action. It forwards to Fly with the server's secret.
 *
 * The owner is checked here as well as in the proxy.
 */
export async function POST(request: Request): Promise<Response> {
  const session = await auth();
  if (!isOwnerSession(session?.user?.email, process.env.OWNER_EMAIL)) {
    return new Response(null, { status: 401 });
  }

  const body = subscriptionBody(await request.json().catch(() => null));
  if (body === null) {
    return new Response(null, { status: 422 });
  }

  return new Response(null, { status: await postSubscription(body) });
}
