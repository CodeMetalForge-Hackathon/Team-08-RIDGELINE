'use strict';
/*
 * Trust numbers for the board's trust view.
 *
 *   node ridgeline/scripts/trust.js        about 3 minutes, writes ridgeline/data/trust.json
 *
 * For aim errors of 1, 3, 6 and 10 degrees it simulates Locks and solves them with the app's settings
 * (3 degree sigma), at the demo card wall and at random spots across the Moran tile. It reports how far off the
 * fix is, how often the 95% bubble holds the truth, the median plus-minus radius and how often the phone would ask
 * to re-aim. The same Locks are also solved the compass way (absolute bearings, so a compass bias moves the fix)
 * with a 7 degree compass bias per fix, the size phones show next to rock, cars and metal.
 */
const fs = require('fs');
const path = require('path');
const S = require('../solver.js');

const OUT = path.join(__dirname, '..', 'data', 'trust.json');
const m = S.load(JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'data', 'moran.json'), 'utf8')));
const demo = m.demo, wall = demo.wall.map(w => w.peak_id);
const LEVELS = [1, 3, 6, 10], N_DEMO = 120, N_RAND = 100, COMPASS_BIAS_SD = 7, SEED = 2026;

function rng(seed) {
  let s = seed >>> 0;
  return () => { s = (Math.imul(s, 1664525) + 1013904223) >>> 0; return (s + 0.5) / 4294967296; };
}
const R = rng(SEED);
const gauss = () => Math.sqrt(-2 * Math.log(R())) * Math.cos(2 * Math.PI * R());
const pct = (a, q) => a.slice().sort((x, y) => x - y)[Math.min(a.length - 1, Math.floor(a.length * q))];

// The same spots and peak trios at every aim error, so the rows compare like with like.
const demoCases = Array.from({ length: N_DEMO }, () => ({
  spot: { e_m: demo.e_m + (R() - 0.5) * m.dx, n_m: demo.n_m + (R() - 0.5) * m.dy }, ids: wall, candidates: wall,
}));
const randCases = [];
while (randCases.length < N_RAND) {
  const i = Math.floor(R() * m.n);
  if (!m.land[i]) continue;
  const vis = m.peaks.filter(p => p.vis[i]).map(p => p.id);
  if (vis.length < 3) continue;
  const spot = { e_m: m.E[i] + (R() - 0.5) * m.dx, n_m: m.N[i] + (R() - 0.5) * m.dy };
  let ids = null;
  for (let k = 0; k < 30 && !ids; k++) {
    const pick = [0, 1, 2].map(() => vis[Math.floor(R() * vis.length)]);
    if (new Set(pick).size < 3) continue;
    const b = pick.map(id => S.bearingFrom(m, spot.e_m, spot.n_m, id)).sort((x, y) => x - y);
    if (Math.min(b[1] - b[0], b[2] - b[1], 360 - b[2] + b[0]) >= 15) ids = pick;
  }
  if (ids) randCases.push({ spot, ids, candidates: undefined });
}

function run(cases, aim, compass) {
  const errs = [], radii = [];
  let covered = 0, reaim = 0, slowest = 0;
  for (const c of cases) {
    // the gyro frame points anywhere, the compass frame is north plus a bias that is the same for all three Locks
    const obs = S.simulate(m, c.spot, c.ids, { rng: R, noise_deg: aim, offset_deg: compass ? 0 : R() * 360 });
    if (compass) { const bias = gauss() * COMPASS_BIAS_SD; obs.locks.forEach(l => { l.heading_deg = S.wrap360(l.heading_deg + bias); }); }
    const res = S.solve(m, obs, { candidates: c.candidates, absolute_bearings: compass, diagnose: false });
    const at = S.cellAt(m, c.spot.e_m, c.spot.n_m);
    if (at >= 0 && res.hpd95[at] === 1) covered++;
    if (res.status === 'mismatch') reaim++;
    errs.push(Math.hypot(res.estimate.e_m - c.spot.e_m, res.estimate.n_m - c.spot.n_m));
    radii.push(res.r95_m);
    slowest = Math.max(slowest, res.ms);
  }
  const n = cases.length;
  return {
    n, median_m: Math.round(pct(errs, 0.5)), p90_m: Math.round(pct(errs, 0.9)), coverage: +(covered / n).toFixed(3),
    r95_median_m: Math.round(pct(radii, 0.5)), reaim_rate: +(reaim / n).toFixed(3), slowest_ms: Math.round(slowest),
  };
}

const t0 = Date.now();
const rows = [];
for (const aim of LEVELS) {
  const row = { aim_deg: aim };
  for (const [name, cases] of [['demo', demoCases], ['random', randCases]]) {
    row[name] = { angles: run(cases, aim, false), compass: run(cases, aim, true) };
    const a = row[name].angles, c = row[name].compass;
    console.log(`aim ${aim} deg ${name}: angles median ${a.median_m} m, inside ${(a.coverage * 100).toFixed(0)}%, re-aim ${(a.reaim_rate * 100).toFixed(0)}% | compass median ${c.median_m} m, inside ${(c.coverage * 100).toFixed(0)}%`);
  }
  rows.push(row);
}
const out = {
  schema: 'ridgeline-trust/v1', made: new Date().toISOString(), seed: SEED, seconds: Math.round((Date.now() - t0) / 1000),
  area: m.json.name, grid_m: Math.round(m.dx), solver_sigma_deg: S.DEFAULTS.bearing_sigma_deg, compass_bias_sd_deg: COMPASS_BIAS_SD,
  demo_wall: demo.wall.map(w => w.name), n_demo: N_DEMO, n_random: randCases.length,
  note: 'Simulated Locks with Gaussian aim error, solved with the app settings. Not a hand-held test.',
  rows,
};
fs.writeFileSync(OUT, JSON.stringify(out, null, 1));
console.log(`wrote ${OUT} in ${out.seconds} s`);
