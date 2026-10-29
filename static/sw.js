// Service worker платформы: приложение ставится на телефон и показывает понятную страницу без сети.
// Страницы кабинета всегда берутся из сети (там деньги и статусы — кэшировать их нельзя),
// из кэша отдаются только оформление, шрифты, иконки и офлайн-страница.
const CACHE = 'liquidity-v3';
const PRECACHE = ['/offline', '/static/css/app.css', '/static/icons/phosphor.svg', '/static/favicon.svg',
                  '/static/fonts/manrope-cyrillic.woff2', '/static/pwa/icon-192.png'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET' || new URL(request.url).origin !== location.origin) return;
  const url = new URL(request.url);
  if (url.pathname.startsWith('/static/')) {
    // Стили, скрипты и набор иконок меняются с каждым релизом: сначала сеть, кэш только без связи.
    // Шрифты и картинки неизменны: сначала кэш.
    const fresh = /\.(css|js|svg)$/.test(url.pathname);
    event.respondWith(caches.open(CACHE).then(async (cache) => {
      const cached = await cache.match(request);
      if (cached && !fresh) return cached;
      try {
        const response = await fetch(request);
        if (response.ok) cache.put(request, response.clone());
        return response;
      } catch (err) {
        return cached || Response.error();
      }
    }));
    return;
  }
  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).catch(() => caches.match('/offline')));
  }
});
