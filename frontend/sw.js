// Exists only so the site qualifies as an installable PWA -- installed
// (home-screen) web apps get meaningfully better background-execution
// treatment from both Android and iOS than a plain browser tab, which is
// the actual point (see index.html's install banner). Deliberately does
// NOT cache anything: index.html is served with no-store specifically so
// every visit gets whatever's actually live right now, and the audio
// stream itself makes no sense to serve from a cache. A pure network
// passthrough still satisfies every browser's installability check.
self.addEventListener("fetch", (event) => {
  // /admin/ is Basic-Auth protected (nginx auth_basic) -- a response
  // routed through a service worker's own fetch() never triggers the
  // browser's native credential prompt for a 401/WWW-Authenticate at all
  // (a deliberate cross-browser security boundary, not something fixable
  // from inside the worker), it just lands on the bare error page.
  // Leaving this request unintercepted (no respondWith call) hands it
  // straight to the browser's normal network stack instead, where auth
  // prompts work as expected.
  const url = new URL(event.request.url);
  if (url.pathname.startsWith("/admin")) {
    return;
  }
  // The audio stream lives on its own host, deliberately outside
  // Cloudflare, precisely so nothing sits between the listener and the
  // origin holding an endless response open. Routing it back through
  // this worker's own fetch() would reintroduce exactly that middleman.
  if (url.hostname !== self.location.hostname) {
    return;
  }
  event.respondWith(fetch(event.request));
});

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});
