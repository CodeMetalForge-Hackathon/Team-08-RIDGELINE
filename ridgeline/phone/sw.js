/* RIDGELINE service worker, served at /sw.js so its scope is the whole app.

   Network first with a 2.5 s timeout. The code changes often, so a fresh file from the PC always wins
   when the PC answers. Every same-origin GET that comes back 200 is copied into the cache, so anything
   opened once (the phone page, solver.js, the terrain grid, the topo map, the route planner, hide.js,
   skyline.js) opens again later with no cable and no network.
   /api/* is never touched: the Lock relay must reach the PC or fail, and the page keeps the newest
   state and sends it again when the link is back. */
'use strict';
const VERSION = 'ridgeline-v1';
const PREFIX = 'ridgeline-';
const TIMEOUT_MS = 2500;
const CORE = [
  '/', '/solver.js', '/data/moran.json', '/topo/moran.jpg',
  '/tv/team8_nav.js', '/tv/data/moran/bundle.js',
  '/hide.js', '/skyline.js',
  '/manifest.webmanifest', '/phone/icon-192.png', '/phone/icon-512.png',
];

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(VERSION);
    // one add per file, each with its own catch: a 404 on an optional file must not break the install
    await Promise.all(CORE.map(url => cache.add(new Request(url, { cache: 'reload' })).catch(() => null)));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(VERSION);
    for (const key of await caches.keys()) {
      if (!key.startsWith(PREFIX) || key === VERSION) continue;
      // carry over anything the new install could not fetch, then drop the old cache
      try {
        const old = await caches.open(key);
        for (const req of await old.keys()) {
          if (!(await cache.match(req))) { const res = await old.match(req); if (res) await cache.put(req, res); }
        }
      } catch (e) { /* keep going */ }
      await caches.delete(key);
    }
    await self.clients.claim();
  })());
});

function fromCache(req) {
  // a page address may carry a query string, the cached page does not
  return caches.match(req).then(hit => hit || (req.mode === 'navigate' ? caches.match(req, { ignoreSearch: true }) : undefined));
}

function offline() {
  return new Response('RIDGELINE is offline and this file was never opened while the PC was connected.',
    { status: 503, statusText: 'Offline', headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
}

self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return;
  if (req.headers.has('range')) return;

  let stored = null;
  const network = fetch(req).then(res => {
    if (res.status === 200 && res.type === 'basic') {
      const copy = res.clone();
      stored = caches.open(VERSION).then(c => c.put(req, copy)).catch(() => null);
    }
    return res;
  });
  event.waitUntil(network.then(() => stored, () => null));

  event.respondWith(new Promise(resolve => {
    let done = false;
    const finish = res => { if (done || !res) return false; done = true; resolve(res); return true; };
    // the PC is slow to answer: serve the cached copy if there is one, keep waiting if there is not
    const timer = setTimeout(() => { fromCache(req).then(finish, () => null); }, TIMEOUT_MS);
    network.then(res => {
      clearTimeout(timer);
      if (res.status >= 500) fromCache(req).then(hit => finish(hit) || finish(res), () => finish(res));
      else finish(res);
    }, () => {
      clearTimeout(timer);
      fromCache(req).then(hit => finish(hit) || finish(offline()), () => finish(offline()));
    });
  }));
});
