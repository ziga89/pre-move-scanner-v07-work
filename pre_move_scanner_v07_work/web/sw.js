const CACHE="premove-v1";
self.addEventListener("install", e => e.waitUntil(
  caches.open(CACHE).then(c => c.addAll(["/","/static/styles.css","/static/app.js"]))
));
self.addEventListener("fetch", e => {
  if(e.request.url.includes("/api/") || e.request.url.includes("/ws")) return;
  e.respondWith(fetch(e.request).catch(()=>caches.match(e.request)));
});
