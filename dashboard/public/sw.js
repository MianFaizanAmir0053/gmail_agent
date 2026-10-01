/*
 * The service worker (M16, D5 and D6). One file, because it is fetched
 * outside the sign-in gate: a second script it imported would need its own
 * exemption.
 *
 * - Caches static build files and icons only (`isCacheable`, tested by
 *   src/lib/swRules.test.ts against this very file); signed-in pages and API
 *   answers always come from the network.
 * - Shows a push as a generic notification (`notificationFor`, tested the same
 *   way); a tap opens the timeline. The push's own content is never trusted
 *   for where to go.
 * - When the browser replaces a subscription, subscribes again and tells the
 *   app, which forwards it to Fly. The app also re-posts on every open, since
 *   not every browser fires that event.
 */
const CACHE = "mailagent-static-v1";

/*
 * Build files have new names on every deploy, and the old ones are never
 * asked for again. Beyond this many entries the oldest go.
 */
const CACHE_LIMIT = 150;

/*
 * Static build output and icons only. A cached timeline would show proposals
 * that are already decided, so pages, API answers and server-component
 * payloads (`?_rsc=`, and any other query) always come from the network.
 */
self.isCacheable = function isCacheable(url, origin) {
  if (url.origin !== origin) return false;
  if (url.search) return false;
  return url.pathname.startsWith("/_next/static/") || url.pathname.startsWith("/icons/");
};

/*
 * The kinds of push Fly sends, by tag, and the notification each becomes. A
 * notification is replaced only by the next of its own kind, so a proposal
 * or a mail alert never replaces an unread sign-in alert, which Fly does not
 * send twice, and the two mail alerts, each sent once a day, never replace
 * each other. A tag not listed here is shown as a proposal.
 */
const TAGS = new Map([
  ["proposal", "mailagent-proposal"],
  ["alert", "mailagent-alert"],
  ["mail-sync", "mailagent-mail-sync"],
  ["mail-feed", "mailagent-mail-feed"],
]);

/*
 * What a push shows. Its words come from Fly, which sends only generic ones;
 * its tag is one of those above.
 */
self.notificationFor = function notificationFor(data) {
  const push = data !== null && typeof data === "object" ? data : {};
  return {
    title: typeof push.title === "string" ? push.title : "mailagent",
    options: {
      body: typeof push.body === "string" ? push.body : "Something needs you.",
      icon: "/icons/icon-192.png",
      badge: "/icons/icon-192.png",
      // One notification per kind: a second proposal replaces the first.
      tag: TAGS.get(push.tag) ?? TAGS.get("proposal"),
      renotify: true,
    },
  };
};

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      for (const name of await caches.keys()) {
        if (name !== CACHE) await caches.delete(name);
      }
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || !self.isCacheable(url, self.location.origin)) {
    return; // the network, as usual
  }
  event.respondWith(
    (async () => {
      const cache = await caches.open(CACHE);
      const hit = await cache.match(event.request);
      if (hit) return hit;
      const response = await fetch(event.request);
      if (response.ok) {
        // Kept off the answer's path: a full or failing cache must cost a
        // cache entry, never the page.
        event.waitUntil(keep(cache, event.request, response.clone()));
      }
      return response;
    })(),
  );
});

async function keep(cache, request, response) {
  try {
    await cache.put(request, response);
    const keys = await cache.keys(); // oldest first
    for (const old of keys.slice(0, Math.max(0, keys.length - CACHE_LIMIT))) {
      await cache.delete(old);
    }
  } catch {
    // Not cached this time; the network still answered.
  }
}

self.addEventListener("push", (event) => {
  let data = null;
  try {
    data = event.data ? event.data.json() : null;
  } catch {
    // Not JSON: generic words.
  }
  const { title, options } = self.notificationFor(data);
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      const open = windows.find((client) => new URL(client.url).origin === self.location.origin);
      if (!open) return self.clients.openWindow("/");
      // Focus first, while the tap still allows it. A window this worker does
      // not control refuses `navigate`; it shows the app either way.
      const focused = await open.focus();
      try {
        await (focused || open).navigate("/");
      } catch {
        // Left on the page it was showing.
      }
    })(),
  );
});

self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil(
    (async () => {
      const options = event.oldSubscription && event.oldSubscription.options;
      if (!options || !options.applicationServerKey) return;
      const subscription = await self.registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: options.applicationServerKey,
      });
      await fetch("/api/push-subscription", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify(subscription),
      });
    })(),
  );
});
