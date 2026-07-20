// f9 reader — service worker
// Shell e fontes: cache-first. Páginas de leitura: network-first com fallback
// (o HTML do /reader/<id> carrega o livro inteiro inline — cacheá-lo = livro offline).
// Áudio de TTS é imutável por chave → cache-first.
const CACHE = 'f9-v1';

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(['/'])).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // Google Fonts: cache-first (respostas opacas ok)
  if (url.hostname === 'fonts.googleapis.com' || url.hostname === 'fonts.gstatic.com') {
    e.respondWith(
      caches.open(CACHE).then(async c => {
        const hit = await c.match(req);
        if (hit) return hit;
        const res = await fetch(req);
        c.put(req, res.clone());
        return res;
      })
    );
    return;
  }

  if (url.origin !== location.origin) return;

  // Áudio TTS: cache-first
  if (url.pathname.startsWith('/api/tts/audio/')) {
    e.respondWith(
      caches.open(CACHE).then(async c => {
        const hit = await c.match(req);
        if (hit) return hit;
        const res = await fetch(req);
        if (res.ok) c.put(req, res.clone());
        return res;
      })
    );
    return;
  }

  if (url.pathname.startsWith('/api/')) return;

  // Home, páginas de leitura e estáticos: network-first, fallback cache
  const cacheable = url.pathname === '/'
    || url.pathname.startsWith('/reader/')
    || url.pathname.startsWith('/static/')
    || url.pathname === '/sw.js';
  if (!cacheable) return;

  e.respondWith(
    fetch(req)
      .then(res => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(req, copy));
        }
        return res;
      })
      .catch(() => caches.match(req))
  );
});
