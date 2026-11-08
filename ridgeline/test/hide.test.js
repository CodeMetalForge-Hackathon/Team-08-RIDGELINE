'use strict';
// HIDE and SIGNAL candidates: node --test ridgeline/test/hide.test.js
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const S = require('../solver.js');
const H = require('../hide.js');

const raw = fs.readFileSync(path.join(__dirname, '..', 'data', 'moran.json'), 'utf8');
const m = S.load(JSON.parse(raw));
const demo = m.demo;
const fix = { lat: demo.lat, lon: demo.lon };
const VK = 0.6 * 6.0 / 3.6;   // Tobler off-trail peak speed, m/s
const sep = (a, b) => Math.hypot(a.e_m - b.e_m, a.n_m - b.n_m);
const fixCell = S.cellAt(m, demo.e_m, demo.n_m);

// Independent slope (central differences as in scripts/pick_demo.js) and summit count.
const slopeAt = i => {
  const c = i % m.nx, r = Math.floor(i / m.nx);
  const gx = (m.elev[i + 1] - m.elev[i - 1]) / (2 * m.dx), gy = (m.elev[i - m.nx] - m.elev[i + m.nx]) / (2 * m.dy);
  return c < 1 || r < 1 || c >= m.nx - 1 || r >= m.ny - 1 ? 90 : Math.atan(Math.hypot(gx, gy)) * 180 / Math.PI;
};
const seenAt = i => m.peaks.reduce((s, p) => s + p.vis[i], 0);

// Independent reach: Bellman-Ford relaxation in a window around the start, no heap.
function bruteReach(start, cap, speed, limitS) {
  const R = Math.ceil(limitS * VK * speed / Math.min(m.dx, m.dy)) + 2;
  const r0 = Math.floor(start / m.nx), c0 = start % m.nx;
  const rows = [Math.max(1, r0 - R), Math.min(m.ny - 2, r0 + R)], cols = [Math.max(1, c0 - R), Math.min(m.nx - 2, c0 + R)];
  const T = new Map([[start, 0]]);
  const blocked = j => !m.land[j] || slopeAt(j) > cap;
  for (let changed = true; changed;) {
    changed = false;
    for (let r = rows[0]; r <= rows[1]; r++) for (let c = cols[0]; c <= cols[1]; c++) {
      const i = r * m.nx + c, ti = T.get(i);
      if (ti === undefined || ti > limitS) continue;
      for (let dr = -1; dr <= 1; dr++) for (let dc = -1; dc <= 1; dc++) {
        if (!dr && !dc) continue;
        const rr = r + dr, cc = c + dc;
        if (rr < rows[0] || rr > rows[1] || cc < cols[0] || cc > cols[1]) continue;
        const j = rr * m.nx + cc;
        if (j === start || blocked(j) || (dr && dc && blocked(r * m.nx + cc) && blocked(rr * m.nx + c))) continue;
        const len = Math.hypot(dr * m.dy, dc * m.dx);
        const tj = ti + len / (VK * Math.exp(-3.5 * Math.abs((m.elev[j] - m.elev[i]) / len + 0.05)) * speed);
        if (tj < (T.has(j) ? T.get(j) : Infinity) - 1e-9) { T.set(j, tj); changed = true; }
      }
    }
  }
  for (const [i, t] of T) if (t > limitS) T.delete(i);
  return T;
}

function checkList(list, kind, out) {
  assert.equal(list.length, 3, kind + ' count');
  list.forEach((c, k) => {
    assert.equal(c.kind, kind);
    assert.equal(c.rank, k + 1);
    assert.equal(c.of, m.peaks.length);
    assert.equal(c.seen_by, seenAt(c.index), 'seen_by recount');
    assert.ok(Math.abs(c.slope_deg - slopeAt(c.index)) < 0.06, 'slope recount');
    assert.ok(c.slope_deg <= out.cap_deg, `${kind} ${c.rank} slope ${c.slope_deg} under the ${out.cap_deg} deg cap`);
    assert.ok(c.walk_min <= out.minutes, `${kind} ${c.rank} walk ${c.walk_min} min`);
    assert.ok(m.land[c.index], 'on land');
    const q = S.toEN(m, c.lon, c.lat);
    assert.equal(S.cellAt(m, q.e, q.n), c.index, 'lat and lon land in the same cell');
    // nobody walks faster than Tobler's best speed in a straight line
    assert.ok(c.walk_min * 60 + 30 >= sep(c, demo) / (VK * (out.injured ? 0.5 : 1)), 'walk time is physical');
    assert.ok(!/safe/i.test(c.why), 'never says safe');
    assert.ok(!/[\u2014\u2013;:]/.test(c.why), 'no dash, semicolon or colon');
    assert.match(c.why, kind === 'HIDE' ? /^Out of sight of .* summits\. .*min walk\.$/ : /^Seen from .* summits on .* ground\. .*min walk\.$/);
  });
  for (let a = 0; a < list.length; a++) for (let b = a + 1; b < list.length; b++) {
    assert.ok(sep(list[a], list[b]) >= 250, `${kind} ${a + 1} and ${b + 1} are ${sep(list[a], list[b]).toFixed(0)} m apart`);
  }
}

test('demo spot, healthy: 3 HIDE and 3 SIGNAL, 250 m apart, inside 30 min and 30 deg', () => {
  const out = H.candidates(m, fix, { cap_deg: 30, minutes: 30 });
  const show = c => `${c.kind[0]}${c.rank} seen ${c.seen_by}/${c.of} walk ${c.walk_min} min slope ${c.slope_deg}`;
  console.log(`  demo: reach ${out.reach_cells} cells, ${out.ms.toFixed(1)} ms\n  ${out.hide.map(show).join('\n  ')}\n  ${out.signal.map(show).join('\n  ')}`);
  assert.equal(out.note, 'OK');
  checkList(out.hide, 'HIDE', out);
  checkList(out.signal, 'SIGNAL', out);
  for (const c of out.signal) assert.ok(c.slope_deg <= 15, 'SIGNAL ground 15 deg or less');
  assert.ok(Math.max(...out.hide.map(c => c.seen_by)) <= Math.min(...out.signal.map(c => c.seen_by)), 'HIDE seen_by not above SIGNAL seen_by');
  // rank order: fewest (HIDE) or most (SIGNAL) summits first, then the shorter walk
  for (let k = 1; k < 3; k++) {
    const h0 = out.hide[k - 1], h1 = out.hide[k], s0 = out.signal[k - 1], s1 = out.signal[k];
    assert.ok(h0.seen_by < h1.seen_by || (h0.seen_by === h1.seen_by && h0.walk_min <= h1.walk_min), 'HIDE order');
    assert.ok(s0.seen_by > s1.seen_by || (s0.seen_by === s1.seen_by && s0.walk_min <= s1.walk_min), 'SIGNAL order');
  }
  assert.deepEqual(H.marks(out).map(x => Object.keys(x).join()), Array(6).fill('kind,rank,lat,lon,why,seen_by,of,walk_min,slope_deg'));
  assert.deepEqual(H.marks(out).map(x => x.why), out.hide.concat(out.signal).map(c => c.why));
});

test('the reach agrees with an independent search, and H1 and S1 are the best cells in it', () => {
  const out = H.candidates(m, fix, { cap_deg: 30, minutes: 30 });
  const T = bruteReach(fixCell, 30, 1, 30 * 60);
  console.log(`  brute force reach ${T.size} cells, hide.js ${out.reach_cells}`);
  assert.ok(Math.abs(T.size - out.reach_cells) <= 3, `reach ${out.reach_cells} vs ${T.size}`);
  for (const c of out.hide.concat(out.signal)) {
    assert.ok(T.has(c.index), `${c.kind} ${c.rank} reachable`);
    assert.ok(Math.abs(T.get(c.index) / 60 - c.walk_min) <= 0.51, `${c.kind} ${c.rank} walk ${c.walk_min} vs ${(T.get(c.index) / 60).toFixed(2)}`);
  }
  const ok = [...T.keys()].filter(i => slopeAt(i) <= 30);
  const fewest = Math.min(...ok.map(seenAt).filter(s => s < m.peaks.length));
  const most = Math.max(...ok.filter(i => slopeAt(i) <= 15).map(seenAt));
  assert.equal(out.hide[0].seen_by, fewest, 'H1 is seen by the fewest summits');
  assert.equal(out.signal[0].seen_by, most, 'S1 is seen by the most summits');
  const nearest = s => Math.min(...ok.filter(i => seenAt(i) === s).map(i => T.get(i))) / 60;
  assert.ok(Math.abs(out.hide[0].walk_min - nearest(fewest)) <= 0.51, 'H1 is the nearest of them');
});

test('injured reaches fewer cells, and STAY when every step is over the slope limit', () => {
  const healthy = H.candidates(m, fix, {});
  const injured = H.candidates(m, fix, { injured: true });
  const slow = H.candidates(m, fix, { injured: true, cap_deg: 30 });
  console.log(`  demo reach: healthy ${healthy.reach_cells}, injured half speed ${slow.reach_cells}, injured 18 deg ${injured.reach_cells} (${injured.note}), demo cell slope ${slopeAt(fixCell).toFixed(1)} deg`);
  assert.equal(injured.cap_deg, 18);
  assert.ok(injured.reach_cells < healthy.reach_cells);
  assert.ok(slow.reach_cells > 1 && slow.reach_cells < healthy.reach_cells);
  checkList(slow.hide, 'HIDE', slow);
  if (injured.reach_cells <= 1) {
    assert.equal(injured.note, 'STAY');
    assert.deepEqual([injured.hide, injured.signal], [[], []]);
  }
  // from gentle ground the injured walker still gets three of each, all under 18 deg
  const flat = healthy.signal.reduce((a, b) => (b.slope_deg < a.slope_deg ? b : a));
  const h2 = H.candidates(m, flat, {}), i2 = H.candidates(m, flat, { injured: true });
  console.log(`  from S${flat.rank} (${flat.slope_deg} deg): healthy ${h2.reach_cells}, injured ${i2.reach_cells}`);
  assert.ok(i2.reach_cells > 1 && i2.reach_cells < h2.reach_cells);
  for (const c of i2.hide.concat(i2.signal)) assert.ok(c.slope_deg <= 18 && c.walk_min <= 30);
});

test('a shorter time limit keeps every candidate inside it', () => {
  const out = H.candidates(m, fix, { minutes: 10 });
  const full = H.candidates(m, fix, { minutes: 30 });
  assert.ok(out.reach_cells < full.reach_cells);
  for (const c of out.hide.concat(out.signal)) assert.ok(c.walk_min <= 10);
});

test('a solver result works as the fix, off the map and open water give STAY', () => {
  let s = 11;
  const rng = () => { s = (Math.imul(s, 1664525) + 1013904223) >>> 0; return (s + 0.5) / 4294967296; };
  const wall = demo.wall.map(w => w.peak_id);
  const res = S.solve(m, S.simulate(m, demo, wall, { rng, offset_deg: 140 }), { candidates: wall });
  const a = H.candidates(m, res, {}), b = H.candidates(m, { lat: res.estimate.lat, lon: res.estimate.lon }, {});
  assert.deepEqual(a.hide, b.hide);
  assert.deepEqual(a.signal, b.signal);
  assert.equal(H.candidates(m, { lat: 0, lon: 0 }, {}).note, 'STAY');
  let lake = -1;
  for (let i = 0; i < m.n && lake < 0; i++) {
    const r = Math.floor(i / m.nx), c = i % m.nx;
    if (r < 6 || c < 6 || r >= m.ny - 6 || c >= m.nx - 6) continue;
    let wet = true;
    for (let a2 = -5; a2 <= 5 && wet; a2++) for (let b2 = -5; b2 <= 5; b2++) if (m.land[i + a2 * m.nx + b2]) { wet = false; break; }
    if (wet) lake = i;
  }
  assert.ok(lake >= 0, 'a lake cell exists');
  const w = H.candidates(m, { e_m: m.E[lake], n_m: m.N[lake] }, {});
  assert.deepEqual([w.note, w.reach_cells, w.hide.length, w.signal.length], ['STAY', 0, 0, 0]);
});

test('timing: under 300 ms on a fresh grid, including the one-time slope and summit count', () => {
  const g = S.load(JSON.parse(raw));
  const t0 = performance.now();
  const cold = H.candidates(g, fix, {});
  const t1 = performance.now();
  const warm = H.candidates(g, fix, {});
  const t2 = performance.now();
  console.log(`  cold ${(t1 - t0).toFixed(1)} ms, warm ${(t2 - t1).toFixed(1)} ms`);
  assert.equal(cold.note, 'OK');
  assert.deepEqual(cold.hide, warm.hide);
  assert.ok(t1 - t0 < 300, 'cold ' + (t1 - t0));
  assert.ok(t2 - t1 < 100, 'warm ' + (t2 - t1));
});
