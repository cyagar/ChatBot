// Technician Manual Assistant — service worker.
//
// Strategy:
//  - App shell (HTML/CSS/JS/icons/manifest): cache-first, so the UI itself opens
//    offline.
//  - Manual files/page images the technician has actually opened: cache-on-read
//    (runtime cache), so previously opened manuals stay available offline —
//    this is the plan's "previously opened manual pages available from cache"
//    requirement, not a blanket pre-cache of the whole 71-file corpus.
//  - Chat/API calls that need a live model or live DB state: network-only. If
//    the network fails, we return a structured JSON error the frontend
//    recognizes and renders as an explicit "you're offline" state rather than
//    a generic failure — never a silent wrong answer.

// Bump SHELL_CACHE's version suffix whenever any file in SHELL_ASSETS changes.
// This file being byte-different is what makes the browser notice an update
// exists at all -- app.js/app.css changing on disk alone does nothing, since
// cache-first means an already-registered service worker keeps serving the
// stale cached copies indefinitely otherwise.
const SHELL_CACHE = "tma-shell-v5";
const MANUAL_CACHE = "tma-manuals-v1";

const SHELL_ASSETS = [
  "/",
  "/static/css/app.css",
  "/static/js/app.js",
  "/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k !== SHELL_CACHE && k !== MANUAL_CACHE)
          .map((k) => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

function isManualAsset(url) {
  return /\/api\/manuals\/\d+\/(file|pages\/\d+\/image)/.test(url.pathname);
}

function isLiveApi(url) {
  return url.pathname.startsWith("/api/") && !isManualAsset(url);
}

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET") return; // POST/PATCH always go straight to network

  if (isLiveApi(url)) {
    event.respondWith(
      fetch(event.request).catch(
        () =>
          new Response(
            JSON.stringify({
              offline: true,
              detail: "This requires a live connection (chat answers and machine search are not available offline).",
            }),
            { status: 503, headers: { "Content-Type": "application/json" } }
          )
      )
    );
    return;
  }

  if (isManualAsset(url)) {
    event.respondWith(
      caches.open(MANUAL_CACHE).then(async (cache) => {
        const cached = await cache.match(event.request);
        const network = fetch(event.request)
          .then((resp) => {
            if (resp.ok) cache.put(event.request, resp.clone());
            return resp;
          })
          .catch(() => cached);
        return cached || network;
      })
    );
    return;
  }

  // App shell: cache-first, falling back to network, updating the cache as we go.
  event.respondWith(
    caches.match(event.request).then(
      (cached) =>
        cached ||
        fetch(event.request).then((resp) => {
          if (resp.ok && url.origin === self.location.origin) {
            caches.open(SHELL_CACHE).then((cache) => cache.put(event.request, resp.clone()));
          }
          return resp;
        })
    )
  );
});
