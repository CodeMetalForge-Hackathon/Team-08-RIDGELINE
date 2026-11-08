// RIDGELINE solver tests. Node 18+, no dependencies.
//
//   node --test tests/solver.test.mjs        (or)   node tests/solver.test.mjs
//
// Simulates locks from the synthetic demo spot with noise, 200 trials, and checks that a swapped peak (aiming at
// the wrong summit) is caught. Writes the numbers to terrain_viewer/data/moran/solver_eval.json, which the judge
// view shows in its trust panel. Same DEM for the hidden spot and the model, so real-world error is not included.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync, writeFileSync} from 'node:fs';
import {loadGrid, solve, simulateLocks, mulberry32, gauss, inHdr, predict, visible, wrap180, distanceToLonLat, DEFAULTS} from '../shared/solver.js';

const ROOT = new URL('../', import.meta.url);
const GRID_URL = new URL('terrain_viewer/data/moran/solver_grid.json', ROOT);
const EVAL_URL = new URL('terrain_viewer/data/moran/solver_eval.json', ROOT);

const TRIALS = 200, SEED = 20261108;
const NOISE = {heading_sigma_deg: 3, elevation_sigma_deg: 1.5, baro_sigma_m: 10, band_m: 30};
const SWAP_TRIALS = 20, SWAP_MIN_SEP_DEG = 15;

const t0 = performance.now();
const g = loadGrid(JSON.parse(readFileSync(GRID_URL, 'utf8')));
const loadMs = performance.now() - t0;
const demo = g.demo, ids = demo.peaks.map(p => p.peak_id);
const pct = (a, q) => { const s = [...a].sort((x, y) => x - y); return s[Math.min(s.length - 1, Math.max(0, Math.ceil(q * s.length) - 1))]; };
const share = (a, f) => a.filter(f).length / a.length;
const r3 = v => Math.round(v * 1000) / 1000;
const errOf = res => res.best ? distanceToLonLat(g, res.best.cell, demo.lon, demo.lat) : Infinity;

// ------------------------------------------------------------------ Monte Carlo from the demo spot
const rng = mulberry32(SEED);
const byN = {1: [], 2: [], 3: []};
const times = [];
for (let t = 0; t < TRIALS; t++) {
  const locks = simulateLocks(g, demo.cell, ids, {sigma_az_deg: NOISE.heading_sigma_deg, sigma_el_deg: NOISE.elevation_sigma_deg, rng});
  const baro = {alt_m: g.eye[demo.cell] + gauss(rng) * NOISE.baro_sigma_m, band_m: NOISE.band_m};
  for (const n of [1, 2, 3]) {
    const res = solve(g, locks.slice(0, n), baro);
    times.push(res.ms);
    byN[n].push({err: errOf(res), inside: inHdr(res, demo.cell), r95: res.hdr95 ? res.hdr95.radius_m : Infinity,
      enough: res.enough_or_one_more === 'enough', poor: res.fit.poor, feasible: res.feasible, wrong: !!(res.wrong_aim && res.wrong_aim.index !== null)});
  }
}
const stats = rows => ({
  trials: rows.length,
  median_err_m: Math.round(pct(rows.map(r => r.err), 0.5)),
  p90_err_m: Math.round(pct(rows.map(r => r.err), 0.9)),
  max_err_m: Math.round(Math.max(...rows.map(r => r.err))),
  truth_in_95_share: r3(share(rows, r => r.inside)),
  median_r95_m: Math.round(pct(rows.map(r => r.r95), 0.5)),
  enough_share: r3(share(rows, r => r.enough)),
  poor_fit_share: r3(share(rows, r => r.poor)),
  no_feasible_cell_share: r3(share(rows, r => !r.feasible)),
  errors_m: rows.map(r => Math.round(r.err)).sort((a, b) => a - b),   // every trial, sorted: the viewer's trust card draws them
});
const MC = {1: stats(byN[1]), 2: stats(byN[2]), 3: stats(byN[3])};

// ------------------------------------------------------------------ wrong aim: one lock measured on the wrong peak
const wrongPeaks = g.peaks.filter(p => visible(g, demo.cell, p.k) && !ids.includes(p.id));
const combos = [];
const rngW = mulberry32(SEED + 1);
for (let slot = 0; slot < ids.length; slot++) {
  const meant = g.byId.get(ids[slot]), meantQ = predict(g, demo.cell, meant.k);
  for (const w of wrongPeaks) {
    const q = predict(g, demo.cell, w.k), sep = Math.abs(wrap180(q.az - meantQ.az));
    const run = (sa, se, r) => {
      const locks = simulateLocks(g, demo.cell, ids, {sigma_az_deg: sa, sigma_el_deg: se, rng: r});
      const off = wrap180(locks[slot].heading_deg - meantQ.az);   // the trial's frame offset (plus noise)
      const aimed = simulateLocks(g, demo.cell, [w.id], {sigma_az_deg: sa, sigma_el_deg: se, rng: r, frame_offset_deg: off})[0];
      locks[slot] = {peak_id: ids[slot], heading_deg: aimed.heading_deg, elevation_deg: aimed.elevation_deg};
      const baro = {alt_m: g.eye[demo.cell] + (sa ? gauss(r) * NOISE.baro_sigma_m : 0), band_m: NOISE.band_m};
      return solve(g, locks, baro);
    };
    const clean = run(0, 0, mulberry32(7));
    const noisy = Array.from({length: SWAP_TRIALS}, () => run(NOISE.heading_sigma_deg, NOISE.elevation_sigma_deg, rngW));
    combos.push({slot, meant: ids[slot], aimed_at: w.id, bearing_separation_deg: +sep.toFixed(1),
      elevation_difference_deg: +(q.el - meantQ.el).toFixed(1), detectable: sep >= SWAP_MIN_SEP_DEG,
      noise_free_named_slot: !!(clean.wrong_aim && clean.wrong_aim.index === slot),
      noisy_flagged_share: r3(share(noisy, r => r.fit.poor || !r.feasible)),
      noisy_named_slot_share: r3(share(noisy, r => !!(r.wrong_aim && r.wrong_aim.index === slot))),
      noisy_named_other_share: r3(share(noisy, r => !!(r.wrong_aim && r.wrong_aim.index !== null && r.wrong_aim.index !== slot)))});
  }
}
const det = combos.filter(c => c.detectable);
const WRONG = {
  method: `One of the three demo locks is measured on another visible peak but labelled as the intended one (a wrong aim). ${SWAP_TRIALS} noisy trials per (lock, wrong peak) pair, plus one noise-free trial.`,
  detectable_rule: `the wrong peak is at least ${SWAP_MIN_SEP_DEG} deg from the intended one in bearing`,
  pairs: combos.length, detectable_pairs: det.length,
  detectable_noise_free_named_share: r3(share(det, c => c.noise_free_named_slot)),
  detectable_noisy_flagged_share: r3(det.reduce((s, c) => s + c.noisy_flagged_share, 0) / Math.max(1, det.length)),
  detectable_noisy_named_share: r3(det.reduce((s, c) => s + c.noisy_named_slot_share, 0) / Math.max(1, det.length)),
  detectable_noisy_named_other_share: r3(det.reduce((s, c) => s + c.noisy_named_other_share, 0) / Math.max(1, det.length)),
  false_alarm_share_without_swap: MC[3].poor_fit_share,
  pairs_detail: combos,
};

// ------------------------------------------------------------------ frame independence (no compass needed)
const FRAMES = [0, 47.5, 123, 271.25, 359.9].map(off => {
  const res = solve(g, simulateLocks(g, demo.cell, ids, {sigma_az_deg: 0, sigma_el_deg: 0, frame_offset_deg: off}), {alt_m: g.eye[demo.cell], band_m: 30});
  return {frame_offset_deg: off, best_cell: res.best.cell, err_m: Math.round(errOf(res)), recovered_offset_deg: res.heading_offset_deg, r95_m: res.hdr95.radius_m};
});

const TIME = {median_ms: +pct(times, 0.5).toFixed(1), p90_ms: +pct(times, 0.9).toFixed(1), max_ms: +Math.max(...times).toFixed(1),
  grid_load_ms: Math.round(loadMs), solves: times.length,
  environment: `Node ${process.version} on ${process.platform}, one thread. Browser timing is measured separately (see the PR).`};

const EVAL = {
  schema: 'ridgeline-solver-eval/v1', generated_by: 'tests/solver.test.mjs', generated_at: new Date().toISOString(),
  note: 'Synthetic. The demo spot is made up and the same 30 m DEM makes the locks and the model, so DEM error, '
    + 'lens and sensor bias, and people who misjudge a summit are not included.',
  area: g.json.area, grid: {rows: g.rows, cols: g.cols, cell_m: g.json.cell_m, peaks: g.peaks.length},
  demo: {cell: demo.cell, lon: demo.lon, lat: demo.lat, elev_m: demo.elev_m, span_deg: demo.span_deg, peaks: demo.peaks},
  noise: {...NOISE, frame_offset: 'uniform 0 to 360 deg per trial (unknown heading frame)', seed: SEED},
  solver: {sigma_az_deg: DEFAULTS.sigma_az_deg, sigma_el_deg: DEFAULTS.sigma_el_deg, hdr_mass: DEFAULTS.hdr_mass,
    enough_radius_m: DEFAULTS.enough_radius_m, min_locks_for_enough: DEFAULTS.min_locks_for_enough, poor_fit_p: DEFAULTS.poor_fit_p},
  trials: TRIALS,
  three_locks: MC[3],
  by_locks: [1, 2, 3].map(n => ({locks: n, ...MC[n]})),
  wrong_aim: WRONG,
  frame_independence: FRAMES,
  timing: TIME,
};
// readable, but arrays of plain numbers (cell sizes, the sorted errors) on one line each
writeFileSync(EVAL_URL, JSON.stringify(EVAL, null, 2).replace(/\[\s+(-?[\d.]+(?:,\s+-?[\d.]+)*)\s+\]/g, (m, a) => '[' + a.split(/,\s+/).join(', ') + ']') + '\n');
console.log(`solver_eval.json: 3 locks median ${MC[3].median_err_m} m, p90 ${MC[3].p90_err_m} m, truth in 95% ${(100 * MC[3].truth_in_95_share).toFixed(1)}%, `
  + `enough ${(100 * MC[3].enough_share).toFixed(1)}%, wrong aim named ${(100 * WRONG.detectable_noisy_named_share).toFixed(1)}%, `
  + `false alarms ${(100 * WRONG.false_alarm_share_without_swap).toFixed(1)}%, solve median ${TIME.median_ms} ms max ${TIME.max_ms} ms`);

// ------------------------------------------------------------------ assertions
test('grid decodes and the demo spot sees its three peaks across about 100 degrees', () => {
  assert.equal(g.n, g.rows * g.cols);
  assert.ok(Math.abs(g.json.cell_m[0] - 30) < 1 && Math.abs(g.json.cell_m[1] - 30) < 1, '30 m cells');
  assert.ok(g.peaks.length >= 3);
  for (const p of g.peaks) assert.ok(p.elev_m >= 2500, `${p.id} is at least 2,500 m`);
  for (const id of ids) assert.ok(visible(g, demo.cell, g.byId.get(id).k), `${id} visible from the demo spot`);
  assert.ok(demo.span_deg >= 90 && demo.span_deg <= 110, `span ${demo.span_deg}`);
  for (const p of demo.peaks) {
    const q = predict(g, demo.cell, g.byId.get(p.peak_id).k);
    assert.ok(Math.abs(wrap180(q.az - p.bearing_true_deg)) < 0.05 && Math.abs(q.el - p.elevation_deg) < 0.05, 'JS and Python agree on bearing and elevation');
  }
});

test('noise-free locks give the demo cell in any heading frame', () => {
  for (const f of FRAMES) {
    assert.equal(f.best_cell, demo.cell, `frame ${f.frame_offset_deg}`);
    assert.ok(Math.abs(wrap180(f.recovered_offset_deg - f.frame_offset_deg)) < 0.01);
  }
});

test(`Monte Carlo, ${TRIALS} trials, 3 deg heading noise`, () => {
  assert.equal(MC[3].trials, TRIALS);
  assert.ok(MC[3].median_err_m < 150, `median ${MC[3].median_err_m} m`);
  assert.ok(MC[3].truth_in_95_share >= 0.85, `truth inside the 95% region ${MC[3].truth_in_95_share}`);
  assert.ok(MC[3].poor_fit_share <= 0.05, `false wrong-aim alarms ${MC[3].poor_fit_share}`);
  assert.equal(MC[1].enough_share, 0, 'never enough after one lock');
  assert.equal(MC[2].enough_share, 0, 'never enough after two locks');
});

test('a swapped peak is caught and the right lock is named', () => {
  assert.ok(det.length >= 3, 'some wrong peaks are far enough apart to test');
  assert.equal(WRONG.detectable_noise_free_named_share, 1, 'noise-free: always names the swapped lock');
  assert.ok(WRONG.detectable_noisy_flagged_share >= 0.9, `noisy: flagged ${WRONG.detectable_noisy_flagged_share}`);
  assert.ok(WRONG.detectable_noisy_named_share >= 0.8, `noisy: named ${WRONG.detectable_noisy_named_share}`);
  // the plain case the phone shows: aim at Pk 3339 instead of Mount Moran
  const slot = ids.indexOf('mount-moran');
  if (slot >= 0 && g.byId.has('pk-3339')) {
    const c = combos.find(x => x.slot === slot && x.aimed_at === 'pk-3339');
    assert.ok(c && c.noise_free_named_slot, 'Pk 3339 instead of Mount Moran is named');
  }
});

test('every solve finishes well under 1 second', () => {
  assert.ok(TIME.max_ms < 1000, `max ${TIME.max_ms} ms`);
});
