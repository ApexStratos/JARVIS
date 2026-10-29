/**
 * JARVIS Nexus Service Worker
 * Strategy: network-first for API/SSE and the server-rendered page.
 *           stale-while-revalidate only for static shell assets.
 */

const CACHE  = 'jarvis-nexus-v3';
const SHELL  = [
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
];

// Routes that bypass the SW entirely
const BYPASS = ['/api/', '/setup/', '/logout'];

// ── Install: pre-cache static shell only ────────────────────────────────────
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE)
      .then(c => c.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

// ── Activate: purge every old cache ─────────────────────────────────────────
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// ── Fetch: route strategy ────────────────────────────────────────────────────
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  if (e.request.method !== 'GET') return;
  if (BYPASS.some(p => url.pathname.startsWith(p))) return;
  if (e.request.headers.get('Accept')?.includes('text/event-stream')) return;

  // The main Flask page is server-rendered and changes on every deploy.
  // Never cache it — let the browser fetch it directly from the server.
  if (url.pathname === '/') return;

  // Static shell assets: stale-while-revalidate
  e.respondWith(
    caches.open(CACHE).then(cache =>
      cache.match(e.request).then(cached => {
        const fresh = fetch(e.request).then(res => {
          if (res.ok) cache.put(e.request, res.clone());
          return res;
        }).catch(() => cached);
        return cached || fresh;
      })
    )
  );
});
