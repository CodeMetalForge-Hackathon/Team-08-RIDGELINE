'use strict';
// Pick the demo landing spot and its three wall peaks by running the real solver.
// The linear geometry score in export_grid.py cannot see two far apart places that fit
// the same angles, so this script simulates Locks and keeps the spot with the tightest
// 95% bubble that still holds the truth.
// Run from the repo root after export_grid.py:  node ridgeline/scripts/pick_demo.js
const fs = require('node:fs');
const path = require('node:path');
const S = require('../solver.js');

const file = path.join(__dirname, '..', 'data', 'moran.json');
const json = JSON.parse(fs.readFileSync(file, 'utf8'));
const m = S.load(json);
const EYE = m.eye;

function rng(seed) {
  let s = seed >>> 0;
  return () => { s = (Math.imul(s, 1664525) + 1013904223) >>> 0; return (s + 0.5) / 4294967296; };
}
const R = rng(2026);
const med = a => a.slice().sort((x, y) => x - y)[a.length >> 1];
const up = (i, id) => {
  const p = m.byId[id];
  const d = Math.hypot(p.e_m - m.E[i], p.n_m - m.N[i]);
  return { d, a: Math.atan2(p.elev_m - (m.elev[i] + EYE) - (d * d) / (2 * m.rEff), d) * 180 / Math.PI };
};
const slopeAt = i => {
  const c = i % m.nx, r = Math.floor(i / m.nx);
  if (c < 1 || r < 1 || c >= m.nx - 1 || r >= m.ny - 1) return 90;
  const gx = (m.elev[i + 1] - m.elev[i - 1]) / (2 * m.dx), gy = (m.elev[i - m.nx] - m.elev[i + m.nx]) / (2 * m.dy);
  return Math.atan(Math.hypot(gx, gy)) * 180 / Math.PI;
};

// 1. Random candidates that satisfy the wall rules.
const cands = [];
let tries = 0;
while (cands.length < 500 && tries < 200000) {
  tries++;
  const i = Math.floor(R() * m.n);
  if (!m.land[i] || m.elev[i] >= 3100 || slopeAt(i) >= 25) continue;
  if (m.E[i] < 800 || m.N[i] < 800 || m.E[i] > m.nx * m.dx - 800 || m.N[i] > m.ny * m.dy - 800) continue;
  const vis = m.peaks.filter(p => p.vis[i]).map(p => p.id).filter(id => { const u = up(i, id); return u.a >= 2 && u.d > 1200 && u.d < 7500; });
  if (vis.length < 3) continue;
  for (let k = 0; k < 6; k++) {
    const trio = [0, 1, 2].map(() => vis[Math.floor(R() * vis.length)]);
    if (new Set(trio).size < 3) continue;
    const b = trio.map(id => S.bearingFrom(m, m.E[i], m.N[i], id)).sort((x, y) => x - y);
    const gaps = [b[1] - b[0], b[2] - b[1], 360 - b[2] + b[0]];
    const spread = 360 - Math.max(...gaps);
    if (spread < 55 || spread > 100 || Math.min(...gaps) < 20) continue;
    cands.push({ i, trio, spread });
    break;
  }
}

// 2. One noiseless solve each, keep the 20 tightest.
for (const c of cands) {
  const res = S.solve(m, S.simulate(m, { e_m: m.E[c.i], n_m: m.N[c.i] }, c.trio, { rng: R, noise_deg: 0, baro_noise_m: 0, offset_deg: 0 }), {});
  c.r0 = res.r95_m;
}
cands.sort((a, b) => a.r0 - b.r0);
const top = cands.slice(0, 20);

// 3. 30 noisy trials each (3 deg aim error, unknown offset), pick the best median bubble.
let best = null;
for (const c of top) {
  let cov = 0;
  const r95 = [], err = [];
  for (let t = 0; t < 30; t++) {
    const spot = { e_m: m.E[c.i] + (R() - 0.5) * m.dx, n_m: m.N[c.i] + (R() - 0.5) * m.dy };
    const res = S.solve(m, S.simulate(m, spot, c.trio, { rng: R, noise_deg: 3, offset_deg: R() * 360 }), {});
    if (res.hpd95[S.cellAt(m, spot.e_m, spot.n_m)]) cov++;
    r95.push(res.r95_m);
    err.push(Math.hypot(res.estimate.e_m - spot.e_m, res.estimate.n_m - spot.n_m));
  }
  c.cov = cov / 30; c.r95 = med(r95); c.err = med(err);
  const named = c.trio.filter(id => m.byId[id].verified_name).length;
  c.score = c.r95 * (1 + 0.15 * (3 - named)) * (c.trio.includes(m.demo.first_peak_id) || c.trio.some(id => m.byId[id].name === 'Mount Moran') ? 0.85 : 1);
  if (c.cov >= 0.9 && (!best || c.score < best.score)) best = c;
}
if (!best) throw new Error('no candidate passed');
console.log(`${cands.length} candidates from ${tries} draws; best r95 median ${best.r95.toFixed(0)} m, error median ${best.err.toFixed(0)} m, coverage ${(best.cov * 100).toFixed(0)}%`);

// 4. Write the demo block.
const i = best.i;
const ground = m.elev[i];
const brg = best.trio.map(id => ({ id, b: S.bearingFrom(m, m.E[i], m.N[i], id) }));
const ref = brg.map(x => x.b).sort((a, b) => a - b);
const gaps = [ref[1] - ref[0], ref[2] - ref[1], 360 - ref[2] + ref[0]];
const startAt = ref[(gaps.indexOf(Math.max(...gaps)) + 1) % 3];
brg.sort((a, b) => S.wrap360(a.b - startAt) - S.wrap360(b.b - startAt));
const mid = brg[1].b;
const wall = brg.map(({ id, b }) => {
  const u = up(i, id);
  return { peak_id: id, name: m.byId[id].name, bearing_deg: +b.toFixed(2), elevation_deg: +u.a.toFixed(2), distance_m: Math.round(u.d), rel_deg: +S.wrap180(b - mid).toFixed(1) };
});
const ll = S.lonLat(m, m.E[i], m.N[i]);
const moran = wall.find(w => w.name === 'Mount Moran');
json.demo = {
  e_m: +m.E[i].toFixed(1), n_m: +m.N[i].toFixed(1), lon: +ll.lon.toFixed(6), lat: +ll.lat.toFixed(6),
  ground_m: +ground.toFixed(1), baro_m: +(ground + EYE + 5).toFixed(1), slope_deg: +slopeAt(i).toFixed(1),
  spread_deg: +best.spread.toFixed(1), sim: { r95_median_m: Math.round(best.r95), error_median_m: Math.round(best.err), coverage: best.cov, trials: 30, aim_error_deg: 3 },
  wall, first_peak_id: (moran || wall[1]).peak_id,
  note: 'Synthetic landing spot picked by pick_demo.js with the real solver. Baro is ground + 1.7 m eye + 5 m error.',
};
fs.writeFileSync(file, JSON.stringify(json));
for (const w of wall) console.log(`  wall ${w.name.padEnd(16)} brg ${w.bearing_deg.toFixed(1).padStart(6)} rel ${w.rel_deg.toFixed(1).padStart(6)} up ${w.elevation_deg.toFixed(1).padStart(5)} dist ${w.distance_m} m`);
console.log(`  spot ${ll.lat.toFixed(5)}, ${ll.lon.toFixed(5)} ground ${ground.toFixed(0)} m`);
