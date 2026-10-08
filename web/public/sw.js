// Minimal service worker. Network-first for navigation and for every
// non-hashed file (the shell, the manifest, icons, splash images), so a new
// deploy is picked up on the next load; cache-first only for the hashed
// bundles under /assets/, whose names change with their content. API traffic
// is never cached. Kept intentionally simple so it never blocks a fetch.
const CACHE = "helios-v7"; // v7: cache-first only for hashed /assets/, 2026-10-08 (v6: token from the served shell)
const CORE = ["/", "/index.html", "/manifest.webmanifest", "/icon.svg"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(CORE)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

function networkFirst(request, fallbackPath) {
  return fetch(request)
    .then((resp) => {
      if (resp && resp.ok) {
        const copy = resp.clone();
        caches.open(CACHE).then((cache) => cache.put(request, copy)).catch(() => {});
      }
      return resp;
    })
    .catch(() =>
      caches.match(request).then((cached) => cached || (fallbackPath ? caches.match(fallbackPath) : undefined))
    );
}

function cacheFirst(request) {
  return caches.match(request).then((cached) => {
    if (cached) return cached;
    return fetch(request).then((resp) => {
      if (resp && resp.ok) {
        const copy = resp.clone();
        caches.open(CACHE).then((cache) => cache.put(request, copy)).catch(() => {});
      }
      return resp;
    });
  });
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  // Never cache API traffic.
  if (url.pathname.startsWith("/api")) return;

  if (request.mode === "navigate") {
    // The shell carries the current token: always try the network, fall
    // back to the cached shell only when the Mac cannot be reached.
    event.respondWith(networkFirst(request, "/index.html"));
    return;
  }

  if (url.pathname.startsWith("/assets/")) {
    // Hashed bundles: the name changes with the content, so cached is current.
    event.respondWith(cacheFirst(request));
    return;
  }

  // Manifest, icons, splash images, sw.js itself: network-first so a new
  // deploy shows up without a hand-bumped cache name.
  event.respondWith(networkFirst(request));
});
