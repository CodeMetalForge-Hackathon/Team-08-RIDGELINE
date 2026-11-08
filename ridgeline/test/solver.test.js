'use strict';
// Gate A for RIDGELINE: node --test ridgeline/test
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const S = require('../solver.js');

const m = S.load(JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'data', 'moran.json'), 'utf8')));
const demo = m.demo;
const wall = demo.wall.map(w => w.peak_id);

function rng(seed) {
  let s = seed >>> 0;
  return () => { s = (Math.imul(s, 1664525) + 1013904223) >>> 0; return (s + 0.5) / 4294967296; };
}
const covers = (res, spot) => { const i = S.cellAt(m, spot.e_m, spot.n_m); return i >= 0 && res.hpd95[i] === 1; };
const errOf = (res, spot) => Math.hypot(res.estimate.e_m - spot.e_m, res.estimate.n_m - spot.n_m);
const pct = (a, q) => a.slice().sort((x, y) => x - y)[Math.min(a.length - 1, Math.floor(a.length * q))];

test('the Moran grid and the demo wall load', () => {
  assert.ok(m.n > 50000);
  assert.ok(m.peaks.length >= 3);
  assert.equal(wall.length, 3);
  for (const id of wall) assert.equal(m.byId[id].vis[S.cellAt(m, demo.e_m, demo.n_m)], 1, id + ' visible from the demo spot');
});

test('no Lock starts with the landmark, one Lock asks for another', () => {
  const r0 = S.solve(m, { baro_m: demo.baro_m, locks: [] }, { candidates: wall });
  assert.equal(r0.status, 'start');
  assert.equal(r0.next_peak_id, demo.first_peak_id);
  const r1 = S.solve(m, S.simulate(m, demo, [demo.first_peak_id], { rng: rng(1) }), { candidates: wall });
  assert.equal(r1.status, 'more');
  assert.ok(r1.next_peak_id && r1.next_peak_id !== demo.first_peak_id);
});

test('demo spot, 3 Locks, 3 deg aim error, unknown phone offset: under 1 s and the 95% bubble holds the truth', () => {
  const R = rng(42);
  const trials = 200;
  let covered = 0, worst = 0, enough = 0, falseAlarm = 0;
  const errs = [], radii = [];
  for (let t = 0; t < trials; t++) {
    const spot = { e_m: demo.e_m + (R() - 0.5) * m.dx, n_m: demo.n_m + (R() - 0.5) * m.dy };
    const obs = S.simulate(m, spot, wall, { rng: R, noise_deg: 3, offset_deg: R() * 360 });
    const res = S.solve(m, obs, { candidates: wall });
    worst = Math.max(worst, res.ms);
    if (covers(res, spot)) covered++;
    if (res.status === 'enough') enough++;
    if (res.status === 'mismatch') falseAlarm++;
    errs.push(errOf(res, spot));
    radii.push(res.r95_m);
  }
  console.log(`  demo: coverage ${(100 * covered / trials).toFixed(1)}%, error median ${pct(errs, 0.5).toFixed(0)} m p90 ${pct(errs, 0.9).toFixed(0)} m, r95 median ${pct(radii, 0.5).toFixed(0)} m, enough ${enough}/${trials}, false mismatch ${falseAlarm}/${trials}, slowest ${worst.toFixed(0)} ms`);
  assert.ok(worst < 1000, 'slowest solve ' + worst);
  assert.ok(covered / trials >= 0.9, 'coverage ' + covered / trials);
  assert.ok(falseAlarm / trials <= 0.05, 'false mismatch ' + falseAlarm);
});

test('aiming twice at the same photo is caught and the third peak is asked again', () => {
  const obs = S.simulate(m, demo, [wall[0], wall[1], wall[0]], { rng: rng(3), noise_deg: 2, offset_deg: 77 });
  obs.locks[2].peak_id = wall[2];
  const res = S.solve(m, obs, { candidates: wall });
  assert.equal(res.status, 'mismatch');
  console.log(`  repeat photo: chi2 ${res.chi2.toFixed(1)}, suspect ${JSON.stringify(res.suspect)}`);
  assert.equal(res.suspect.lock_index, 2);
  assert.equal(res.suspect.peak_id, wall[0]);
  assert.equal(res.suspect.repeat, true);
});

test('a Lock on a different real peak is caught and relabelled', () => {
  const at = S.cellAt(m, demo.e_m, demo.n_m);
  const wrong = m.peaks.find(p => !wall.includes(p.id) && p.vis[at] &&
    Math.abs(S.wrap180(S.bearingFrom(m, demo.e_m, demo.n_m, p.id) - S.bearingFrom(m, demo.e_m, demo.n_m, wall[2]))) > 25);
  assert.ok(wrong, 'a second visible peak exists');
  const obs = S.simulate(m, demo, [wall[0], wall[1], wrong.id], { rng: rng(5), noise_deg: 2, offset_deg: 200 });
  obs.locks[2].peak_id = wall[2];
  const res = S.solve(m, obs, {});
  console.log(`  wrong peak ${wrong.name}: status ${res.status}, chi2 ${res.chi2.toFixed(1)}, suspect ${JSON.stringify(res.suspect)}`);
  assert.equal(res.status, 'mismatch');
  assert.equal(res.suspect.lock_index, 2);
});

test('random spots across Moran (where 3 peaks are visible): coverage and error', () => {
  const R = rng(7);
  let n = 0, covered = 0;
  const errs = [];
  while (n < 120) {
    const i = Math.floor(R() * m.n);
    if (!m.land[i]) continue;
    const vis = m.peaks.filter(p => p.vis[i]).map(p => p.id);
    if (vis.length < 3) continue;
    const spot = { e_m: m.E[i] + (R() - 0.5) * m.dx, n_m: m.N[i] + (R() - 0.5) * m.dy };
    let trio = null;
    for (let k = 0; k < 30 && !trio; k++) {
      const pick = [0, 1, 2].map(() => vis[Math.floor(R() * vis.length)]);
      if (new Set(pick).size < 3) continue;
      const b = pick.map(id => S.bearingFrom(m, spot.e_m, spot.n_m, id)).sort((x, y) => x - y);
      const gaps = [b[1] - b[0], b[2] - b[1], 360 - b[2] + b[0]];
      if (Math.min(...gaps) >= 15) trio = pick;
    }
    if (!trio) continue;
    const res = S.solve(m, S.simulate(m, spot, trio, { rng: R, noise_deg: 3, offset_deg: R() * 360 }), {});
    if (covers(res, spot)) covered++;
    errs.push(errOf(res, spot));
    n++;
  }
  console.log(`  random: coverage ${(100 * covered / n).toFixed(1)}%, error median ${pct(errs, 0.5).toFixed(0)} m p75 ${pct(errs, 0.75).toFixed(0)} m p90 ${pct(errs, 0.9).toFixed(0)} m (n ${n})`);
  assert.ok(covered / n >= 0.85, 'coverage ' + covered / n);
});
