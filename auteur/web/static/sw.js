/* A service worker with modest ambitions.
 *
 * It exists so the app installs to the home screen and so the shell still
 * opens on a flaky connection. It deliberately never caches anything under
 * /api/ — a cached job status is worse than no job status, and a cached
 * finished film would be served for the next film too.
 */

var CACHE = "auteur-shell-v1";
var SHELL = [
  "/",
  "/static/style.css",
  "/static/app.js",
  "/manifest.webmanifest",
  "/icon-192.png",
  "/icon-180.png"
];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE)
      .then(function (cache) { return cache.addAll(SHELL); })
      .then(function () { return self.skipWaiting(); })
      .catch(function () { /* an uncached shell still works online */ })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys()
      .then(function (keys) {
        return Promise.all(keys.filter(function (key) { return key !== CACHE; })
                              .map(function (key) { return caches.delete(key); }));
      })
      .then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (event) {
  var request = event.request;
  if (request.method !== "GET") { return; }

  var url = new URL(request.url);
  if (url.origin !== self.location.origin) { return; }
  if (url.pathname.indexOf("/api/") === 0) { return; }

  // Network first, so a redeploy is picked up straight away; the cache is only
  // there for the case where the network is not.
  event.respondWith(
    fetch(request)
      .then(function (response) {
        if (response && response.ok) {
          var copy = response.clone();
          caches.open(CACHE).then(function (cache) { cache.put(request, copy); });
        }
        return response;
      })
      .catch(function () {
        return caches.match(request).then(function (hit) {
          return hit || caches.match("/");
        });
      })
  );
});
