'use strict';
/*
 * Trust numbers for the board's trust view.
 *
 *   node ridgeline/scripts/trust.js        2 to 3 minutes, writes ridgeline/data/trust.json
 *
 * For aim errors of 1, 3, 6 and 10 degrees it simulates Locks and solves them with the app's settings
 * (3 degree sigma), at the demo card wall and at random spots across the Moran tile. It reports how far off the
 * fix is, how often the 95% bubble holds the truth, the median plus-minus radius and how often the phone would ask
 * to re-aim. The same Locks are also solved the compass way (absolute bearings, so a compass bias moves the fix)
 * with a 7 degree compass bias per fix, the size phones show next to rock, cars and metal.
 *
 * Barometer reference error. At the demo card wall with 3 degree aim error, the barometer altitude the solver gets
 * is the true eye height plus a fixed offset of -60, -30, 0, +30 or +60 m (a wrong reference pressure shifts every
 * reading the same way, no other barometer noise is added), and once more with the barometer band switched off.
 * Every offset solves the same spots and Locks, so only the barometer changes between rows. This sweep runs in a
 * worker thread while the aim error sweep runs in the main thread.
 */
const fs = require('fs');
const path = require('path');
const { Worker, isMainThread, parentPort } = require('worker_threads');
const S = require('../solver.js');

const OUT = path.join(__dirname, '..', 'data', 'trust.json');
const m = S.load(JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'data', 'moran.json'), 'utf8')));
const demo = m.demo, wall = demo.wall.map(w => w.peak_id);
const LEVELS = [1, 3, 6, 10], N_DEMO = 120, N_RAND = 100, COMPASS_BIAS_SD = 7, SEED = 2026;
const BARO_OFFSETS = [-60, -30, 0, 30, 60], N_BARO = 200, BARO_AIM = 3, BARO_SEED = SEED + 1;

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

// Every row scores its fixes against the truth the same way.
const tally = () => ({ errs: [], radii: [], covered: 0, reaim: 0, slowest: 0 });
function score(t, res, spot) {
  const at = S.cellAt(m, spot.e_m, spot.n_m);
  if (at >= 0 && res.hpd95[at] === 1) t.covered++;
  if (res.status === 'mismatch') t.reaim++;
  t.errs.push(Math.hypot(res.estimate.e_m - spot.e_m, res.estimate.n_m - spot.n_m));
  t.radii.push(res.r95_m);
  t.slowest = Math.max(t.slowest, res.ms);
}
function summary(t) {
  const n = t.errs.length;
  return {
    n, median_m: Math.round(pct(t.errs, 0.5)), p90_m: Math.round(pct(t.errs, 0.9)), coverage: +(t.covered / n).toFixed(3),
    r95_median_m: Math.round(pct(t.radii, 0.5)), reaim_rate: +(t.reaim / n).toFixed(3), slowest_ms: Math.round(t.slowest),
  };
}

function run(cases, aim, compass) {
  const t = tally();
  for (const c of cases) {
    // the gyro frame points anywhere, the compass frame is north plus a bias that is the same for all three Locks
    const obs = S.simulate(m, c.spot, c.ids, { rng: R, noise_deg: aim, offset_deg: compass ? 0 : R() * 360 });
    if (compass) { const bias = gauss() * COMPASS_BIAS_SD; obs.locks.forEach(l => { l.heading_deg = S.wrap360(l.heading_deg + bias); }); }
    score(t, S.solve(m, obs, { candidates: c.candidates, absolute_bearings: compass, diagnose: false }), c.spot);
  }
  return summary(t);
}

// Barometer reference error at the demo wall. Its own random stream, so the aim error rows keep their numbers.
// baro_noise_m 0 makes simulate() return the true eye height, the offset is added on top of it.
function baroSweep() {
  const tb = Date.now(), RB = rng(BARO_SEED);
  const cases = Array.from({ length: N_BARO }, () => {
    const spot = { e_m: demo.e_m + (RB() - 0.5) * m.dx, n_m: demo.n_m + (RB() - 0.5) * m.dy };
    const obs = S.simulate(m, spot, wall, { rng: RB, noise_deg: BARO_AIM, offset_deg: RB() * 360, baro_noise_m: 0 });
    return { spot, ground_m: m.elev[S.cellAt(m, spot.e_m, spot.n_m)], true_baro_m: obs.baro_m, locks: obs.locks };
  });
  // offset null switches the barometer band off. ground_shift: how much higher the ground under the fix is than the truth.
  const runBaro = offset => {
    const t = tally(), shift = [];
    for (const c of cases) {
      const baro_m = offset === null ? null : c.true_baro_m + offset;
      const res = S.solve(m, { baro_m, locks: c.locks }, { candidates: wall, diagnose: false });
      score(t, res, c.spot);
      shift.push(res.estimate.ground_m - c.ground_m);
    }
    const row = Object.assign({ offset_m: offset }, summary(t), { ground_shift_median_m: Math.round(pct(shift, 0.5)) });
    console.log(`baro ${offset === null ? 'band off' : (offset > 0 ? '+' : '') + offset + ' m'}: median ${row.median_m} m, p90 ${row.p90_m} m, inside ${(row.coverage * 100).toFixed(1)}%, plus-minus ${row.r95_median_m} m, fix ground ${row.ground_shift_median_m} m vs truth, re-aim ${(row.reaim_rate * 100).toFixed(1)}%`);
    return row;
  };
  const rows = BARO_OFFSETS.map(o => runBaro(o)), off = runBaro(null);
  return { rows, off, seconds: Math.round((Date.now() - tb) / 1000) };
}

async function main() {
  const t0 = Date.now();
  // The barometer sweep runs in a second thread next to the aim error sweep (same numbers, about half the wait).
  // If the thread cannot start, it runs here afterwards.
  const baroThread = new Promise((ok, no) => {
    const w = new Worker(__filename);
    w.once('message', ok);
    w.once('error', no);
    w.once('exit', code => setImmediate(() => no(new Error('barometer thread stopped, code ' + code))));   // no-op once the results are in
  }).catch(e => { console.log(`${e.message}, running the barometer sweep in the main thread`); return null; });
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
  const baro = (await baroThread) || baroSweep();
  const out = {
    schema: 'ridgeline-trust/v1', made: new Date().toISOString(), seed: SEED, seconds: Math.round((Date.now() - t0) / 1000),
    area: m.json.name, grid_m: Math.round(m.dx), solver_sigma_deg: S.DEFAULTS.bearing_sigma_deg, compass_bias_sd_deg: COMPASS_BIAS_SD,
    demo_wall: demo.wall.map(w => w.name), n_demo: N_DEMO, n_random: randCases.length,
    note: 'Simulated Locks with Gaussian aim error, solved with the app settings. Not a hand-held test.',
    rows,
    baro: {
      where: 'demo', aim_deg: BARO_AIM, n: N_BARO, seed: BARO_SEED, solver_sigma_m: S.DEFAULTS.baro_sigma_m, seconds: baro.seconds,
      note: 'Barometer altitude given to the solver is the true eye height plus a fixed offset, no other barometer noise. Same spots and Locks at every offset.',
      rows: baro.rows, off: baro.off,
    },
  };
  fs.writeFileSync(OUT, JSON.stringify(out, null, 1));
  console.log(`wrote ${OUT} in ${out.seconds} s`);
}

if (isMainThread) main();
else parentPort.postMessage(baroSweep());
