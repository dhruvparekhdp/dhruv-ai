/* Jarvis service worker.
 *
 * Required for iOS "Add to Home Screen" to behave like an app. Caching is
 * deliberately limited to the static shell: API responses are personal, change
 * constantly, and must never be served stale.
 *
 * The push handler is wired now but inert until Phase 2 registers VAPID keys.
 */

const CACHE = "jarvis-shell-v1";
const SHELL = [
  "/",
  "/static/styles.css",
  "/static/app.js",
  "/manifest.json",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Never cache the API, the health probe, or the WebSocket upgrade.
  if (request.method !== "GET" || url.pathname.startsWith("/api/") || url.pathname === "/health") {
    return;
  }

  // Network-first: always prefer fresh, fall back to the cached shell offline.
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok && url.origin === self.location.origin) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(request, copy));
        }
        return response;
      })
      .catch(() => caches.match(request).then((hit) => hit || caches.match("/")))
  );
});

self.addEventListener("push", (event) => {
  const data = event.data ? event.data.json() : { title: "Jarvis", body: "New event" };
  event.waitUntil(
    self.registration.showNotification(data.title || "Jarvis", {
      body: data.body || "",
      icon: "/static/icons/icon-192.png",
      badge: "/static/icons/icon-192.png",
      vibrate: [200, 100, 200],
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(self.clients.openWindow("/"));
});
