'use strict';
// Expected skyline toward the next peak: node --test ridgeline/test/skyline.test.js
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const S = require('../solver.js');
const K = require('../skyline.js');

const m = S.load(JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'data', 'moran.json'), 'utf8')));
const demo = m.demo;
const wall = demo.wall.map(w => w.peak_id);
const from = { lat: demo.lat, lon: demo.lon };
const at = S.cellAt(m, demo.e_m, demo.n_m);
const R2D = 180 / Math.PI;
const now = () => performance.now();

// The first call of a fresh process runs before the JIT has compiled anything, like the first
// silhouette on the phone. Time it before any test warms the code up.
let t = now();
K.profile(m, from, wall[1]);
const coldMs = now() - t;

function rng(seed) {
  let s = seed >>> 0;
  return () => { s = (Math.imul(s, 1664525) + 1013904223) >>> 0; return (s + 0.5) / 4294967296; };
}
const mid = pr => pr.samples[(pr.samples.length - 1) / 2];

// Summit angle as solver.js predicts it: cell ground + 1.7 m eye, earth curvature with refraction.
function predicted(id) {
  const p = m.byId[id];
  const d = Math.hypot(p.e_m - demo.e_m, p.n_m - demo.n_m);
  return Math.atan2(p.elev_m - (m.elev[at] + m.eye) - (d * d) / (2 * m.rEff), d) * R2D;
}

// The wall cards' horizon march (cards/index.html panorama), kept here as an independent reference.
function cardsElevAt(e, n) {
  const f = e / m.dx - 0.5, g = m.ny - n / m.dy - 0.5;
  const c0 = Math.floor(f), r0 = Math.floor(g);
  if (c0 < 0 || r0 < 0 || c0 + 1 >= m.nx || r0 + 1 >= m.ny) return null;
  const fx = f - c0, fy = g - r0, i = r0 * m.nx + c0;
  return (m.elev[i] * (1 - fx) + m.elev[i + 1] * fx) * (1 - fy) + (m.elev[i + m.nx] * (1 - fx) + m.elev[i + m.nx + 1] * fx) * fy;
}
function cardsSkyline(e0, n0, azDeg) {
  const z0 = cardsElevAt(e0, n0) + m.eye, az = azDeg / R2D, se = Math.sin(az), ce = Math.cos(az);
  let best = -30;
  for (let r = 45; r < 30000; r += 15) {
    const z = cardsElevAt(e0 + se * r, n0 + ce * r);
    if (z == null) break;
    const a = Math.atan2(z - z0 - r * r / (2 * m.rEff), r) * R2D;
    if (a > best) best = a;
  }
  return best;
}

test('toward each wall peak the skyline at daz 0 is the summit, within 0.3 deg of the predicted angle', () => {
  for (const w of demo.wall) {
    const pr = K.profile(m, from, w.peak_id);
    assert.ok(pr, w.name);
    assert.equal(pr.fov_deg, 30);
    assert.equal(pr.samples.length, 121);
    assert.equal(pr.samples[0].daz, -15);
    assert.equal(pr.samples[120].daz, 15);
    const c = mid(pr);
    assert.equal(c.daz, 0);
    assert.equal(pr.peak.daz, 0);
    const want = predicted(w.peak_id);
    const plain = mid(K.profile(m, from, w.peak_id, { summit_radius_m: 0 }));
    console.log(`  ${w.name}: daz 0 ${c.elev_deg.toFixed(3)} deg made at ${c.dist_m.toFixed(0)} m, predicted ${want.toFixed(3)} deg ` +
      `at ${pr.peak.dist_m.toFixed(0)} m (export ${w.elevation_deg}), 30 m grid alone ${plain.elev_deg.toFixed(3)}, ` +
      `bearing ${pr.az_center.toFixed(2)} (export ${w.bearing_deg})`);
    assert.ok(Math.abs(c.elev_deg - want) <= 0.3, `${w.name} ${c.elev_deg} vs ${want}`);
    assert.ok(Math.abs(c.elev_deg - w.elevation_deg) <= 0.3, `${w.name} vs export_grid.py`);
    assert.ok(Math.abs(pr.peak.elev_deg - want) < 0.01, 'peak.elev_deg uses the solver formula');
    assert.ok(Math.abs(S.wrap180(pr.az_center - w.bearing_deg)) < 0.05, 'window centered on the true bearing');
    // The summit itself makes the skyline at daz 0: nothing nearer or behind it on that ray is higher.
    assert.ok(Math.abs(c.dist_m - pr.peak.dist_m) < 1, `${w.name} skyline made at ${c.dist_m} m`);
  }
});

test('a summit the visibility mask calls visible is not hidden behind nearer terrain', () => {
  for (const w of demo.wall) {
    assert.equal(m.byId[w.peak_id].vis[at], 1, w.name + ' visible in the mask');
    const pr = K.profile(m, from, w.peak_id);
    assert.equal(pr.peak.hidden, false, w.name);
    assert.ok(pr.peak.clear_deg > 0, w.name);
    console.log(`  ${w.name}: clear of nearer terrain by ${pr.peak.clear_deg.toFixed(2)} deg`);
  }
  // Cross check against the 10 m line of sight of export_grid.py, from random land cells to all 23 peaks.
  const R = rng(11);
  let n = 0, visOk = 0, visMiss = 0, hidOk = 0, hidMiss = 0;
  while (n < 300) {
    const i = Math.floor(R() * m.n);
    if (!m.land[i]) continue;
    n++;
    for (const p of m.peaks) {
      const pr = K.profile(m, { e_m: m.E[i], n_m: m.N[i] }, p.id, { half_fov_deg: 0.25, step_deg: 0.25 });
      if (!pr || pr.peak.dist_m < 100) continue;
      if (p.vis[i]) { if (pr.peak.hidden) visMiss++; else visOk++; } else if (pr.peak.hidden) hidOk++; else hidMiss++;
    }
  }
  const a = visOk / (visOk + visMiss), b = hidOk / (hidOk + hidMiss);
  console.log(`  ${n} random cells x 23 peaks: mask visible -> not hidden ${(100 * a).toFixed(1)}% (${visMiss} of ${visOk + visMiss} called hidden), ` +
    `mask hidden -> hidden ${(100 * b).toFixed(1)}% (the 30 m mean is smoother than the 10 m DEM)`);
  assert.ok(a >= 0.98, 'visible agreement ' + a);
  assert.ok(b >= 0.9, 'hidden agreement ' + b);
});

test('with the summit cones off it matches the wall cards horizon within 0.1 deg', () => {
  let worst = 0;
  for (const w of demo.wall) {
    const pr = K.profile(m, from, w.peak_id, { summit_radius_m: 0 });
    for (const s of pr.samples) {
      const ref = cardsSkyline(pr.from.e_m, pr.from.n_m, pr.az_center + s.daz);
      worst = Math.max(worst, Math.abs(s.elev_deg - ref));
    }
  }
  console.log(`  363 rays, largest difference from the cards march ${worst.toFixed(3)} deg`);
  assert.ok(worst <= 0.1, 'worst ' + worst);
});

test('after 2 Locks: result.estimate and {lat, lon} give the same profile, off the tile gives null', () => {
  const obs = S.simulate(m, demo, wall.slice(0, 2), { rng: rng(1), noise_deg: 3, offset_deg: 123 });
  const res = S.solve(m, obs, { candidates: wall });
  assert.ok(res.next_peak_id);
  const a = K.profile(m, res.estimate, res.next_peak_id);
  const b = K.profile(m, { lat: res.estimate.lat, lon: res.estimate.lon }, res.next_peak_id);
  let diff = 0;
  for (let k = 0; k < a.samples.length; k++) diff = Math.max(diff, Math.abs(a.samples[k].elev_deg - b.samples[k].elev_deg));
  console.log(`  2 Locks: estimate ${Math.hypot(res.estimate.e_m - demo.e_m, res.estimate.n_m - demo.n_m).toFixed(0)} m from the truth, ` +
    `next ${a.peak.name}, summit ${a.peak.elev_deg.toFixed(2)} deg (truth ${predicted(res.next_peak_id).toFixed(2)}), hidden ${a.peak.hidden}`);
  assert.ok(diff < 1e-3, 'diff ' + diff);
  assert.equal(K.profile(m, { lat: 0, lon: 0 }, wall[0]), null);
  assert.equal(K.profile(m, from, 'pk_none'), null);
});

test('svgPath fills the ground under the horizon inside the box with the summit marker on the line', () => {
  const W = 160, H = 60;
  for (const w of demo.wall) {
    const pr = K.profile(m, from, w.peak_id);
    const g = K.svgPath(pr, W, H);
    assert.match(g.d, /^M0 60L/);
    assert.match(g.d, /L160 60Z$/);
    const pts = g.d.replace(/[MZ]/g, '').split('L').map(s => s.split(' ').map(Number));
    assert.equal(pts.length, pr.samples.length + 2);
    const line = pts.slice(1, -1);
    for (const [x, y] of pts) assert.ok(x >= 0 && x <= W && y >= 0 && y <= H, `${x} ${y} in the box`);
    for (let k = 1; k < line.length; k++) assert.ok(line[k][0] > line[k - 1][0], 'left to right');
    assert.equal(g.line, 'M' + line.map(p => p.join(' ')).join('L'));
    const c = line[(line.length - 1) / 2];
    assert.equal(g.peakX, W / 2);
    assert.ok(Math.abs(g.peakY - c[1]) <= 0.11, `marker ${g.peakY} on the skyline ${c[1]}`);
    assert.ok(Math.min(...line.map(p => p[1])) >= 8 - 0.11, 'top padding kept');
    console.log(`  ${w.name}: marker (${g.peakX}, ${g.peakY}), vertical exaggeration x${g.vex.toFixed(2)}, path ${g.d.length} chars`);
  }
});

test('one profile takes under 50 ms', () => {
  const R = rng(5);
  const ms = [];
  for (let k = 0; k < 30; k++) {
    const spot = { e_m: demo.e_m + (R() - 0.5) * 2000, n_m: demo.n_m + (R() - 0.5) * 2000 };
    t = now();
    const pr = K.profile(m, spot, wall[k % 3]);
    ms.push(now() - t);
    assert.ok(pr);
  }
  ms.sort((x, y) => x - y);
  console.log(`  first call of the process (JIT cold) ${coldMs.toFixed(1)} ms, then 30 calls median ${ms[15].toFixed(1)} ms, ` +
    `slowest ${ms[29].toFixed(1)} ms (Node ${process.version})`);
  assert.ok(ms[29] < 50, 'slowest warm call ' + ms[29]);
  assert.ok(coldMs < 100, 'cold first call ' + coldMs);
});
