// RIDGELINE phone app: works offline after the first load.
//
// On install it keeps a copy of everything the phone needs (the page, the scenario replay, the live solver and route
// modules, the 30 m Moran grid, the manifest and icons) in one versioned cache. Every later request tries the network
// first, so a fresh edit or a newer grid is picked up while online, and falls back to the copy when the network fails
// or takes longer than 3 s. The live link to the judge screen (/lock, /state, /reset, /health) is never cached.
// Change VERSION when the file list changes; old copies are deleted on activate.
const VERSION = 'ridgeline-phone-v1';
const ESSENTIAL = ['./', './index.html', './phone_state.json'];   // without these the install fails and retries next load
const SHELL = [...ESSENTIAL, './manifest.webmanifest', './icons/icon-180.png', './icons/icon-192.png', './icons/icon-512.png',
  './icons/icon-maskable-512.png', '../../shared/solver.js', '../../shared/route.js', '../../terrain_viewer/data/moran/solver_grid.json'];
const LIVE_LINK = new Set(['/lock', '/state', '/reset', '/health']);
const WAIT_MS = 3000;

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(VERSION);
    const got = await Promise.allSettled(SHELL.map(async url => {
      const res = await fetch(new Request(url, {cache: 'reload'}));   // skip the HTTP cache, keep what the server has now
      if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
      await cache.put(url, res);
    }));
    const failed = SHELL.filter((url, i) => got[i].status === 'rejected');
    if (failed.some(url => ESSENTIAL.includes(url))) throw new Error('RIDGELINE offline copy: could not fetch ' + failed.join(', '));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) if (key.startsWith('ridgeline-phone-') && key !== VERSION) await caches.delete(key);
    await self.clients.claim();   // the page that installed it is served from the copy from now on
  })());
});

self.addEventListener('fetch', event => {
  const req = event.request, url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== self.location.origin || LIVE_LINK.has(url.pathname)) return;   // network only
  event.respondWith(networkFirst(req, url));
});

async function networkFirst(req, url) {
  const cache = await caches.open(VERSION);
  const net = fetch(req).then(res => {
    if (res.ok && res.type === 'basic') cache.put(req, res.clone()).catch(() => {});   // keep the copy current
    return res;
  });
  net.catch(() => {});   // a late failure after the copy was served is not an error
  try {
    return await Promise.race([net, new Promise((_, no) => setTimeout(() => no(new Error('slow network')), WAIT_MS))]);
  } catch {
    const hit = await cache.match(req, {ignoreSearch: true}) || (req.mode === 'navigate' ? await cache.match('./') : undefined);
    if (hit) return hit;
    return net.catch(() => new Response(`RIDGELINE is offline and has no copy of ${url.pathname}.`,
      {status: 503, headers: {'Content-Type': 'text/plain; charset=utf-8'}}));
  }
}

// the page asks what is kept, to say "Works offline" in Settings
self.addEventListener('message', event => {
  if (!event.data || event.data.type !== 'ridgeline-status' || !event.source) return;
  event.waitUntil((async () => {
    const cache = await caches.open(VERSION);
    const have = new Set((await cache.keys()).map(r => r.url));
    const missing = SHELL.filter(u => !have.has(new URL(u, self.location).href));
    let bytes = 0;
    for (const r of await cache.keys()) { const res = await cache.match(r); if (res) bytes += (await res.clone().arrayBuffer()).byteLength; }
    event.source.postMessage({type: 'ridgeline-status', version: VERSION, kept: SHELL.length - missing.length, of: SHELL.length, missing, bytes});
  })());
});
