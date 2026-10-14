// Bump CACHE_VERSION any time worker-app.html changes
// to force iPhones to grab the new version.
const CACHE_VERSION = 'v10-2026-10-14a';   // worker videos
const ASSETS = ['/worker-app.html', '/worker-app-manifest.json'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE_VERSION).then(c => c.addAll(ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(k => k !== CACHE_VERSION).map(k => caches.delete(k))
    ))
  );
  self.clients.claim();
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);

  // API calls — always live, never cached
  if (url.pathname.startsWith('/api/')) {
    e.respondWith(
      fetch(e.request).catch(() =>
        new Response(JSON.stringify({error: 'offline'}), {headers: {'Content-Type':'application/json'}})
      )
    );
    return;
  }

  // HTML/JS — network first, fall back to cache only if offline.
  // This guarantees workers always see the latest UI when online.
  if (e.request.mode === 'navigate' || url.pathname.endsWith('.html') || url.pathname.endsWith('.js')) {
    e.respondWith(
      fetch(e.request)
        .then(resp => {
          const copy = resp.clone();
          caches.open(CACHE_VERSION).then(c => c.put(e.request, copy));
          return resp;
        })
        .catch(() => caches.match(e.request))
    );
    return;
  }

  // Everything else (images, manifest) — cache first
  e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
});
