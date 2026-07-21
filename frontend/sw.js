// Exists only so the site qualifies as an installable PWA -- installed
// (home-screen) web apps get meaningfully better background-execution
// treatment from both Android and iOS than a plain browser tab, which is
// the actual point (see index.html's install banner). Deliberately does
// NOT cache anything: index.html is served with no-store specifically so
// every visit gets whatever's actually live right now, and the audio
// stream itself makes no sense to serve from a cache. A pure network
// passthrough still satisfies every browser's installability check.
self.addEventListener("fetch", (event) => {
  event.respondWith(fetch(event.request));
});

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});
