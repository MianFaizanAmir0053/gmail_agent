/*
 * The service worker (M16, D5 and D6). One file, because it is fetched
 * outside the sign-in gate: a second script it imported would need its own
 * exemption.
 *
 * - Caches static build files and icons only (`isCacheable`, tested by
 *   src/lib/swRules.test.ts against this very file); signed-in pages and API
 *   answers always come from the network.
 * - Shows a push as a generic notification; a tap opens the timeline. The
 *   push's own content is never trusted for where to go.
 * - When the browser replaces a subscription, subscribes again and tells the
 *   app, which forwards it to Fly. The app also re-posts on every open, since
 *   not every browser fires that event.
 */
const CACHE = "mailagent-static-v1";

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
      if (response.ok) await cache.put(event.request, response.clone());
      return response;
    })(),
  );
});

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    // Not JSON: fall back to the generic words below.
  }
  const title = typeof data.title === "string" ? data.title : "mailagent";
  const body = typeof data.body === "string" ? data.body : "Something needs you.";
  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      icon: "/icons/icon-192.png",
      badge: "/icons/icon-192.png",
      // One notification at a time: a second push replaces the first.
      tag: "mailagent",
      renotify: true,
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      for (const client of windows) {
        if (new URL(client.url).origin === self.location.origin) {
          await client.navigate("/");
          return client.focus();
        }
      }
      return self.clients.openWindow("/");
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
