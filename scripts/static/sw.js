const CACHE = 'small-jobs-v1';
const PRECACHE = [
  '/static/bootstrap.min.css',
  '/static/bootstrap.bundle.min.js',
  '/static/manifest.json',
];

// Install: pre-cache static assets
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(PRECACHE)));
  self.skipWaiting();
});

// Activate: clear any old caches from previous versions
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // Cache-first for static assets (CSS, JS — rarely change)
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(
      caches.match(e.request).then(cached => {
        if (cached) return cached;
        return fetch(e.request).then(res => {
          caches.open(CACHE).then(c => c.put(e.request, res.clone()));
          return res;
        });
      })
    );
    return;
  }

  // Network-first for all app routes and API calls (data must be fresh)
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});
