// Network-first service worker (PWA install). API and WebSocket traffic is never cached.
const CACHE = "premove-v073";
const ASSETS = ["/", "/static/styles.css", "/static/js/main.js", "/static/js/util.js", "/static/js/conn.js",
  "/static/js/top.js", "/static/js/coin.js", "/static/js/charts.js", "/static/js/health.js"];
self.addEventListener("install", e => e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting())));
self.addEventListener("activate", e => e.waitUntil(
  caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())
));
self.addEventListener("fetch", e => {
  if (e.request.url.includes("/api/") || e.request.url.includes("/ws")) return;
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
