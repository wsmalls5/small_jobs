// Service worker for the Door Codes PWA.
// Scope is /codes/ (it is served from /codes/sw.js) so it never competes with
// the main app's root-scoped /sw.js for control of a page.
//
// The shell is cache-FIRST on purpose: the server is only running when Tom's
// dad sits down to do paperwork, so the app has to open with it powered off.
const CACHE = 'door-codes-v1';
const SHELL = '/codes/';
const PRECACHE = [
  SHELL,
  '/codes/manifest.json',
  '/static/codes-icon-192.png',
  '/static/codes-icon-512.png',
];

self.addEventListener('install', e => {
  e.waitUntil((async () => {
    const c = await caches.open(CACHE);
    // Individually, so one 404 can't abort the whole install
    await Promise.all(PRECACHE.map(u => c.add(new Request(u, {cache: 'reload'})).catch(() => {})));
    self.skipWaiting();
  })());
});

self.addEventListener('activate', e => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    // Only ever delete our own caches — 'small-jobs-*' belongs to the main app
    await Promise.all(
      keys.filter(k => k.startsWith('door-codes-') && k !== CACHE).map(k => caches.delete(k))
    );
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== location.origin) return;

  // Data endpoint: always straight to the network. The page keeps its own
  // copy in localStorage, so a stale SW cache here would only confuse things.
  if (url.pathname === '/codes/api') return;

  // Page load: serve the cached shell instantly, refresh it in the background
  if (req.mode === 'navigate') {
    e.respondWith((async () => {
      const cached = await caches.match(SHELL);
      const fresh  = fetch(req).then(async res => {
        if (res && res.ok) {
          const c = await caches.open(CACHE);
          await c.put(SHELL, res.clone());
        }
        return res;
      }).catch(() => null);

      if (cached) {
        e.waitUntil(fresh);
        return cached;
      }
      return (await fresh) || new Response(
        'Door Codes has not been opened while the server was running yet, so there is nothing saved on this phone.',
        {status: 503, headers: {'Content-Type': 'text/plain; charset=utf-8'}}
      );
    })());
    return;
  }

  // Everything else (icons): cache-first, fill on miss
  e.respondWith((async () => {
    const cached = await caches.match(req);
    if (cached) return cached;
    try {
      const res = await fetch(req);
      if (res && res.ok) {
        const c = await caches.open(CACHE);
        await c.put(req, res.clone());
      }
      return res;
    } catch (err) {
      return new Response('', {status: 504});
    }
  })());
});
