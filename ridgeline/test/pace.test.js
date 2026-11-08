'use strict';
// Auto-PACE grid filter: node --test ridgeline/test
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const S = require('../solver.js');
const P = require('../pace.js');

const m = S.load(JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'data', 'moran.json'), 'utf8')));
const demo = m.demo;
const wall = demo.wall.map(w => w.peak_id);
const K = Math.max(P.DEFAULTS.along_frac, Math.sin(P.DEFAULTS.cross_deg * Math.PI / 180));

function rng(seed) {
  let s = seed >>> 0;
  return () => { s = (Math.imul(s, 1664525) + 1013904223) >>> 0; return (s + 0.5) / 4294967296; };
}
const ms = () => Number(process.hrtime.bigint()) / 1e6;
const sum = p => { let s = 0; for (const v of p) s += v; return s; };
const errTo = (st, e, n) => Math.hypot(st.stats.e_m - e, st.stats.n_m - n);

// the demo fix: three wall peaks seen from the demo spot, 3 deg aim error, unknown phone offset
function demoFix(seed) {
  const obs = S.simulate(m, demo, wall, { rng: rng(seed), offset_deg: 37 });
  return { obs, fix: S.solve(m, obs, { candidates: wall, keep_p: true }) };
}
// a round Gaussian map (sigma s0) around a point, for the shape tests
function gaussMap(e0, n0, s0) {
  const p = new Float64Array(m.n);
  for (let i = 0; i < m.n; i++) { const de = m.E[i] - e0, dn = m.N[i] - n0; p[i] = Math.exp(-(de * de + dn * dn) / (2 * s0 * s0)); }
  const s = sum(p);
  for (let i = 0; i < m.n; i++) p[i] /= s;
  return { p };
}
// the first point found with no water within R metres, away from the sheet edges
function openSpot(R) {
  const dry = (e, n) => {
    for (let de = -R; de <= R; de += 30) for (let dn = -R; dn <= R; dn += 30) {
      if (de * de + dn * dn > R * R) continue;
      const i = P.cellAt(m, e + de, n + dn);
      if (i < 0 || !m.land[i]) return false;
    }
    return true;
  };
  for (let e = R; e < m.nx * m.dx - R; e += 300) for (let n = R; n < m.ny * m.dy - R; n += 300) if (dry(e, n)) return { e, n };
  return null;
}
// three peaks visible from a spot, spread as widely as possible (the board's test pilot rule)
function threePeaks(e, n) {
  const i = S.cellAt(m, e, n);
  const vis = m.peaks.filter(p => p.vis[i]).map(p => p.id);
  let best = null;
  for (let a = 0; a < vis.length; a++) for (let b = a + 1; b < vis.length; b++) for (let c = b + 1; c < vis.length; c++) {
    const br = [vis[a], vis[b], vis[c]].map(id => S.bearingFrom(m, e, n, id)).sort((x, y) => x - y);
    const gap = Math.min(br[1] - br[0], br[2] - br[1], 360 - br[2] + br[0]);
    if (!best || gap > best.gap) best = { gap, ids: [vis[a], vis[b], vis[c]] };
  }
  return best ? best.ids : null;
}

test('keep_p adds the normalized map and changes nothing else', () => {
  const obs = S.simulate(m, demo, wall, { rng: rng(3), offset_deg: 12 });
  const a = S.solve(m, obs, { candidates: wall }), b = S.solve(m, obs, { candidates: wall, keep_p: true });
  assert.equal(S.DEFAULTS.keep_p, false);
  assert.ok(!('p' in a), 'no map unless asked');
  assert.equal(b.p.length, m.n);
  assert.ok(Math.abs(sum(b.p) - 1) < 1e-9, 'sums to 1');
  let bi = 0;
  for (let i = 1; i < m.n; i++) if (b.p[i] > b.p[bi]) bi = i;
  assert.equal(bi, b.estimate.index, 'the best cell is the estimate');
  for (const k of ['status', 'r95_m', 'r50_m', 'chi2', 'confidence', 'offset_deg', 'next_peak_id']) assert.deepStrictEqual(b[k], a[k], k);
  assert.deepStrictEqual(b.estimate, a.estimate);
  assert.deepStrictEqual(Array.from(b.hpd95), Array.from(a.hpd95));
});

test('a known heading moves the map by the walk and widens it by D x max(8%, sin 6 deg)', () => {
  const o = openSpot(1600);
  assert.ok(o, 'an open spot exists on the sheet');
  const s0 = 30;
  const st = P.create(m, gaussMap(o.e, o.n, s0), { t: 0 });
  const a = P.moments(st);
  for (let k = 0; k < 20; k++) P.addWalk(st, 25, 0);   // 500 m north
  P.flush(st);
  const b = P.moments(st), want = Math.sqrt(s0 * s0 + (K * 500) ** 2);
  assert.ok(Math.abs(b.n_m - a.n_m - 500) < 3, `moved north ${(b.n_m - a.n_m).toFixed(1)} m`);
  assert.ok(Math.abs(b.e_m - a.e_m) < 3, 'no sideways drift');
  for (const v of [b.var_e, b.var_n]) assert.ok(Math.abs(Math.sqrt(v) / want - 1) < 0.05, `sigma ${Math.sqrt(v).toFixed(1)} m, formula ${want.toFixed(1)} m`);
  // east as well, from a fresh start: the column shift goes the right way
  const st2 = P.create(m, gaussMap(o.e, o.n, s0), { t: 0 });
  P.addWalk(st2, 400, 90); P.flush(st2);
  const c = P.moments(st2);
  assert.ok(Math.abs(c.e_m - o.e - 400) < 3 && Math.abs(c.n_m - o.n) < 3, `moved east ${(c.e_m - o.e).toFixed(1)} m`);
  // the growth is linear in the distance, not its square root: twice the walk, twice the added spread
  assert.ok(Math.abs(Math.sqrt(c.var_e - s0 * s0) / (K * 400) - 1) < 0.06);
});

test('with no usable heading the 95% radius grows by the distance walked and the centre stays', () => {
  const o = openSpot(1600), s0 = 30;
  const st = P.create(m, gaussMap(o.e, o.n, s0), { t: 0 });
  const r0 = st.stats.r95_m;
  for (let k = 0; k < 12; k++) P.addWalk(st, 25, null);   // 300 m, heading unknown
  P.flush(st);
  const b = P.moments(st), want = Math.sqrt(s0 * s0 + (300 / P.K95) ** 2);
  assert.ok(Math.hypot(b.e_m - o.e, b.n_m - o.n) < 3, 'centre stays');
  assert.ok(Math.abs(Math.sqrt(b.var_e) / want - 1) < 0.05 && Math.abs(Math.sqrt(b.var_n) / want - 1) < 0.05);
  // the 95% radius adds in quadrature: K95 x sqrt(s0^2 + (300/K95)^2), plus half a cell diagonal like the solver
  const r = st.stats.r95_m, rWant = P.K95 * want + Math.hypot(m.dx, m.dy) / 2;
  assert.ok(Math.abs(r - rWant) < 15, `95% radius ${r0.toFixed(0)} -> ${r.toFixed(0)} m, formula ${rWant.toFixed(0)} m`);
  assert.ok(r > 300, 'at least the distance walked');
  assert.equal(P.summary(st, 0).basis, 'drift');
});

test('lake cells hold no probability after a walk, and the map still sums to 1', () => {
  const { fix } = demoFix(5);
  const st = P.create(m, fix, { t: 0 });
  // walk toward the nearest water so part of the map would land in it
  let best = null;
  for (let b = 0; b < 360; b += 15) {
    for (let L = 30; L <= 3000; L += 30) {
      const i = P.cellAt(m, demo.e_m + L * Math.sin(b * Math.PI / 180), demo.n_m + L * Math.cos(b * Math.PI / 180));
      if (i >= 0 && !m.land[i]) { if (!best || L < best.L) best = { b, L }; break; }
    }
  }
  assert.ok(best, 'water on the sheet');
  for (let k = 0; k < Math.ceil(best.L / 20); k++) P.addWalk(st, 20, best.b);
  P.flush(st);
  let wet = 0;
  for (let i = 0; i < m.n; i++) if (!m.land[i]) wet += st.p[i];
  assert.equal(wet, 0, 'nobody stands in a lake');
  assert.ok(Math.abs(sum(st.p) - 1) < 1e-9);
  assert.ok(m.land[st.stats.index] === 1);
});

test('on a slope the barometer band keeps the circle smaller than steps alone, and both hold the truth', () => {
  const { obs, fix } = demoFix(7);
  const g = (e, n) => P.groundAt(m, e, n);
  const ge = (g(demo.e_m + 30, demo.n_m) - g(demo.e_m - 30, demo.n_m)) / 60, gn = (g(demo.e_m, demo.n_m + 30) - g(demo.e_m, demo.n_m - 30)) / 60;
  const up = P.wrap360(Math.atan2(ge, gn) * 180 / Math.PI);
  assert.ok(Math.hypot(ge, gn) > 0.2, 'the demo spot is on a slope');
  const ref = obs.baro_m - (g(demo.e_m, demo.n_m) + m.eye);   // the barometer's reference error stays the same
  const withB = P.create(m, fix, { t: 0, baro_m: obs.baro_m }), noB = P.create(m, fix, { t: 0 });
  let te = demo.e_m, tn = demo.n_m, used = 0;
  for (let k = 0; k < 40; k++) {   // 400 m uphill
    te += 10 * Math.sin(up * Math.PI / 180); tn += 10 * Math.cos(up * Math.PI / 180);
    P.addWalk(withB, 10, up); P.addWalk(noB, 10, up);
    if (P.baro(withB, g(te, tn) + m.eye + ref).used) used++;
  }
  P.flush(withB); P.flush(noB);
  assert.equal(used, 4, 'one band per 100 m walked');
  assert.ok(withB.stats.r95_m < 0.85 * noB.stats.r95_m, `with ${withB.stats.r95_m.toFixed(0)} m, without ${noB.stats.r95_m.toFixed(0)} m`);
  assert.ok(errTo(withB, te, tn) <= withB.stats.r95_m && errTo(noB, te, tn) <= noB.stats.r95_m, 'truth inside both');
  // a band is not used again before another 100 m
  assert.equal(P.baro(withB, g(te, tn) + m.eye + ref).used, false);
});

test('GPS inside the 95% circle agrees and tightens the map, one outside is rejected and never used', () => {
  const { fix } = demoFix(11);
  const st = P.create(m, fix, { t: 0 });
  for (let k = 0; k < 10; k++) P.addWalk(st, 20, 90);   // 200 m east, truth walks the same
  P.flush(st);
  const te = demo.e_m + 200, tn = demo.n_m, r0 = st.stats.r95_m;
  // a spoofed reading 2.5 km NE of the truth
  const far = P.lonLat(m, te + 2500 * Math.SQRT1_2, tn + 2500 * Math.SQRT1_2);
  const before = Float64Array.from(st.p);
  const bad = P.gps(st, far.lat, far.lon);
  assert.equal(bad.verdict, 'REJECTED');
  assert.equal(bad.used, false);
  assert.ok(bad.dist_m > r0);
  assert.deepStrictEqual(Array.from(st.p), Array.from(before), 'a rejected reading leaves the map alone');
  assert.equal(P.summary(st, 0).gps, 'REJECTED');
  // an honest reading 40 m from the truth
  const near = P.lonLat(m, te - 20, tn - 34.6);
  const ok = P.gps(st, near.lat, near.lon);
  assert.equal(ok.verdict, 'AGREE');
  assert.equal(ok.used, true);
  assert.ok(st.stats.r95_m < 80 && st.stats.r95_m < r0 / 3, `${r0.toFixed(0)} m -> ${st.stats.r95_m.toFixed(0)} m`);
  assert.ok(errTo(st, te, tn) < 80);
  assert.equal(P.summary(st, 0).basis, 'gps');
  // the same honest reading again is not multiplied in twice
  const r1 = st.stats.r95_m;
  assert.equal(P.gps(st, near.lat, near.lon).used, false);
  assert.equal(st.stats.r95_m, r1);
  // the spoofed reading stays rejected, even if the circle were to grow to reach it
  for (let k = 0; k < 40; k++) P.addWalk(st, 50, null);
  P.flush(st);
  assert.equal(P.gps(st, far.lat, far.lon).verdict, 'REJECTED');
});

test('a new fix after walking is multiplied in when it agrees and taken alone when the steps were off', () => {
  // agreeing: the truth walked the same 400 m the steps say
  const { fix } = demoFix(13);
  const st = P.create(m, fix, { t: 0 });
  for (let k = 0; k < 20; k++) P.addWalk(st, 20, 300);
  P.flush(st);
  const te = demo.e_m + 400 * Math.sin(300 * Math.PI / 180), tn = demo.n_m + 400 * Math.cos(300 * Math.PI / 180);
  const ids = threePeaks(te, tn);
  assert.ok(ids, 'three peaks seen from the new spot');
  const nf = S.solve(m, S.simulate(m, { e_m: te, n_m: tn }, ids, { rng: rng(17), offset_deg: 37 }), { keep_p: true });
  const pred = st.stats.r95_m;
  const out = P.refix(st, nf, { t: 60000 });
  assert.equal(out.took_new, false);
  assert.ok(out.overlap >= 0.01, `overlap ${out.overlap.toFixed(3)}`);
  assert.ok(st.stats.r95_m <= Math.min(pred, nf.r95_m) * 1.05, `prediction ${pred.toFixed(0)}, new ${nf.r95_m.toFixed(0)}, both ${st.stats.r95_m.toFixed(0)}`);
  assert.ok(errTo(st, te, tn) <= st.stats.r95_m, 'truth inside');
  const sm = P.summary(st, 60000);
  assert.equal(sm.basis, 'refix');
  assert.equal(sm.walked_fix_m, 0);
  // steps off: the map walked 1.5 km east, the person did not move
  const b = demoFix(19);
  const st2 = P.create(m, b.fix, { t: 0 });
  for (let k = 0; k < 75; k++) P.addWalk(st2, 20, 90);
  P.flush(st2);
  const again = S.solve(m, S.simulate(m, demo, wall, { rng: rng(23), offset_deg: 37 }), { candidates: wall, keep_p: true });
  const out2 = P.refix(st2, again, { t: 60000 });
  assert.equal(out2.took_new, true);
  assert.ok(out2.overlap < 0.01);
  assert.equal(st2.stats.index, again.estimate.index);
  assert.equal(st2.stats.r95_m, again.r95_m);
  assert.equal(P.summary(st2, 60000).last_refix.took_new, true);
});

test('the tiers ask for a new fix over 600 m or over 2.5 times the radius at the fix', () => {
  const { fix } = demoFix(29);
  const st = P.create(m, fix, { t: 0 });
  let s = P.summary(st, 0);
  assert.equal(s.basis, 'fix');
  assert.equal(s.refix, false);
  assert.equal(s.level, fix.r95_m <= 300 ? 'good' : 'caution');
  while (!P.summary(st, 0).refix && st.walked_m < 5000) P.addWalk(st, 20, null);
  P.flush(st);
  s = P.summary(st, 0);
  assert.equal(s.refix, true);
  assert.equal(s.level, 'low');
  assert.ok(s.r95_m > 600 || s.r95_m > 2.5 * s.r95_fix_m);
  // a tight fix asks earlier, at 2.5 times its own radius
  const o = openSpot(1600);
  const tight = P.create(m, gaussMap(o.e, o.n, 20), { t: 0 });
  while (!P.summary(tight, 0).refix && tight.walked_m < 5000) P.addWalk(tight, 10, null);
  P.flush(tight);
  const t2 = P.summary(tight, 0);
  assert.ok(t2.r95_m < 600 && t2.r95_m > 2.5 * t2.r95_fix_m, `asked at ${t2.r95_m} m from ${t2.r95_fix_m} m`);
  assert.ok(t2.trail.length >= 1 && t2.trail.every(q => Number.isFinite(q[0]) && Number.isFinite(q[1])));
});

test('one update of the Moran map stays under 30 ms', () => {
  const { obs, fix } = demoFix(31);
  const st = P.create(m, fix, { t: 0, baro_m: obs.baro_m });
  for (let k = 0; k < 5; k++) { P.addWalk(st, 15, 45); }   // warm up
  const t = [];
  for (let k = 0; k < 15; k++) { const t0 = ms(); P.addWalk(st, 15, 45 + k); t.push(ms() - t0); }
  // the barometer band and a GPS reading, 7 times each (a new reading each time, so every one is used), median
  // because a single call on a busy PC can land on a garbage collection
  const tb = [], tg = [];
  for (let k = 0; k < 7; k++) { const t0 = ms(); P.baro(st, P.groundAt(m, st.stats.e_m, st.stats.n_m) + m.eye, true); tb.push(ms() - t0); }
  for (let k = 0; k < 7; k++) { const q = P.lonLat(m, st.stats.e_m + 10 + k, st.stats.n_m); const t0 = ms(); P.gps(st, q.lat, q.lon); tg.push(ms() - t0); }
  const mid = a => a.slice().sort((x, y) => x - y)[a.length >> 1];
  t.sort((a, b) => a - b);
  const med = t[Math.floor(t.length / 2)];
  console.log(`  pace update: median ${med.toFixed(1)} ms, max ${t[t.length - 1].toFixed(1)} ms, baro median ${mid(tb).toFixed(1)} ms (max ${Math.max(...tb).toFixed(1)}), gps median ${mid(tg).toFixed(1)} ms (max ${Math.max(...tg).toFixed(1)})`);
  assert.ok(med < 30, `median ${med.toFixed(1)} ms`);
  assert.ok(mid(tb) < 30 && mid(tg) < 30, `baro ${mid(tb).toFixed(1)} ms, gps ${mid(tg).toFixed(1)} ms`);
});

test('the step detector counts a walk, ignores stillness and a single bump', () => {
  const R = rng(37), noise = s => (R() - 0.5) * 2 * s;
  // 10 s of walking at 1.8 steps per second, 60 Hz
  let det = P.stepDetector(), n = 0;
  for (let k = 0; k < 600; k++) { const t = k / 60; n += det.push(t, noise(0.3), 9.81 + 2.5 * Math.sin(2 * Math.PI * 1.8 * t) + noise(0.4), noise(0.3)); }
  assert.ok(Math.abs(n - 18) <= 2, `${n} steps for 18`);
  // still in the hand
  det = P.stepDetector(); n = 0;
  for (let k = 0; k < 600; k++) n += det.push(k / 60, noise(0.15), 9.81 + noise(0.25), noise(0.15));
  assert.equal(n, 0);
  // one knock on the table
  det = P.stepDetector(); n = 0;
  for (let k = 0; k < 600; k++) { const t = k / 60; n += det.push(t, 0, 9.81 + (Math.abs(t - 4) < 0.08 ? 6 : 0) + noise(0.1), 0); }
  assert.equal(n, 0);
  // the stride the phone uses
  assert.equal(P.STRIDE.healthy, 0.7);
  assert.equal(P.STRIDE.injured, 0.45);
});
