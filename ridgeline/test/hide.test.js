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

function checkList(list, kind, out, from = demo) {
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
    assert.ok(c.walk_min * 60 + 30 >= sep(c, from) / (VK * (out.injured ? 0.5 : 1)), 'walk time is physical');
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

// ---------- walkableStart: a hand-held fix on a cliff starts from the nearest open, walkable cell ----------
// Independent patch size: plain flood fill with the same step rule as the reach, stopped at `limit` cells.
function patchSize(i0, cap, limit) {
  const ok = j => m.land[j] && slopeAt(j) <= cap;
  if (!ok(i0)) return 0;
  const seen = new Set([i0]), todo = [i0];
  while (todo.length && seen.size < limit) {
    const i = todo.pop(), r = Math.floor(i / m.nx), c = i % m.nx;
    for (let dr = -1; dr <= 1; dr++) for (let dc = -1; dc <= 1; dc++) {
      const rr = r + dr, cc = c + dc, j = rr * m.nx + cc;
      if ((!dr && !dc) || rr < 0 || cc < 0 || rr >= m.ny || cc >= m.nx || seen.has(j) || !ok(j)) continue;
      if (dr && dc && !ok(r * m.nx + cc) && !ok(rr * m.nx + c)) continue;
      seen.add(j); todo.push(j);
    }
  }
  return seen.size;
}
const OPEN = H.DEFAULTS.open_cells;
const DIR8 = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];

function checkStart(ws, at, cap, r_m) {
  assert.ok(ws.ok, 'a start was found');
  assert.equal(ws.cap_deg, cap);
  assert.ok(m.land[ws.index], 'start on land');
  assert.ok(slopeAt(ws.index) <= cap, `start slope ${slopeAt(ws.index).toFixed(1)} under ${cap}`);
  assert.ok(Math.abs(ws.slope_deg - slopeAt(ws.index)) < 0.06, 'start slope recount');
  assert.ok(patchSize(ws.index, cap, OPEN) >= OPEN, 'start is on open ground, not a boxed-in ledge');
  const d = Math.hypot(m.E[ws.index] - at.e_m, m.N[ws.index] - at.n_m);
  assert.ok(Math.abs(ws.dist_m - d) <= 0.5 && d <= r_m, `start ${d.toFixed(0)} m from the fix, inside ${r_m.toFixed(0)} m`);
  const b = (Math.atan2(m.E[ws.index] - at.e_m, m.N[ws.index] - at.n_m) * 180 / Math.PI + 360) % 360;
  assert.equal(ws.dir, DIR8[Math.round(b / 45) % 8], 'compass direction from the fix');
  const q = S.toEN(m, ws.lon, ws.lat);
  assert.equal(S.cellAt(m, q.e, q.n), ws.index, 'start lat and lon land in the same cell');
  // nothing nearer is open walkable ground
  const r0 = Math.floor(ws.index / m.nx), c0 = ws.index % m.nx, k = 2 * Math.ceil(d / 29) + 2;
  for (let r = r0 - k; r <= r0 + k; r++) for (let c = c0 - k; c <= c0 + k; c++) {
    if (r < 0 || c < 0 || r >= m.ny || c >= m.nx) continue;
    const i = r * m.nx + c;
    if (i === ws.index || Math.hypot(m.E[i] - at.e_m, m.N[i] - at.n_m) >= d - 1e-6) continue;
    assert.ok(!m.land[i] || slopeAt(i) > cap || patchSize(i, cap, OPEN) < OPEN, `cell ${i} is nearer and walkable`);
  }
  // the alternatives: nearest first, inside the circle, spaced out, all open walkable ground
  let last = ws.dist_m;
  for (const a of ws.alts) {
    assert.ok(a.dist_m >= last - 0.5 && a.dist_m <= r_m + 0.5, 'alternatives nearest first, inside the circle');
    last = a.dist_m;
    assert.ok(m.land[a.index] && slopeAt(a.index) <= cap && patchSize(a.index, cap, OPEN) >= OPEN, 'alternative walkable');
  }
  const all = [ws].concat(ws.alts);
  for (let x = 0; x < all.length; x++) for (let y = x + 1; y < all.length; y++) assert.ok(sep(all[x], all[y]) >= H.DEFAULTS.alt_spacing_m, 'alternatives spaced');
}

test('walkableStart: the Gate B hand-held fix (cell over 30 deg) gets a start inside its 363 m circle with Hide and Signal spots', () => {
  // Locks from the real phone, Nov 8 4:28 EST
  const locks = [{ peak_id: 'pk_8', heading_deg: 355.4 }, { peak_id: 'pk_20', heading_deg: 323.4 }, { peak_id: 'pk_13', heading_deg: 55.0 }];
  const res = S.solve(m, { baro_m: 2446.1, locks }, { candidates: demo.wall.map(w => w.peak_id) });
  assert.ok(Math.abs(res.estimate.lat - 43.85221) < 2e-5 && Math.abs(res.estimate.lon + 110.78681) < 2e-5, 'the Gate B estimate');
  assert.equal(Math.round(res.r95_m), 363);
  const before = JSON.stringify(res.estimate), fi = res.estimate.index;
  assert.ok(slopeAt(fi) > 30, `the estimate cell is ${slopeAt(fi).toFixed(1)} deg`);
  assert.equal(H.candidates(m, res, {}).note, 'STAY', 'from the fix cell itself nothing is reachable (what the phone showed)');
  for (const cap of [30, 18]) {
    const injured = cap === 18;
    const ws = H.walkableStart(m, res, { cap_deg: cap });   // r_m defaults to the fix's r95
    console.log(`  Gate B ${cap} deg: start ${ws.dist_m} m ${ws.dir} (${ws.reason}), slope ${ws.slope_deg}, alternatives ${ws.alts.map(a => a.dist_m + ' m ' + a.dir).join(', ')}`);
    assert.equal(ws.r_m, 363);
    assert.ok(ws.moved && ws.reason === 'steep' && ws.fix_index === fi);
    checkStart(ws, res.estimate, cap, res.r95_m);
    assert.ok(ws.alts.length >= 1, 'alternatives for the route planner');
    const out = H.candidates(m, ws, { cap_deg: cap, injured });
    const show = c => `${c.kind[0]}${c.rank} ${c.seen_by}/${c.of} ${c.walk_min} min`;
    console.log(`  reach ${out.reach_cells} cells, ${out.hide.concat(out.signal).map(show).join(', ')}`);
    assert.equal(out.note, 'OK');
    checkList(out.hide, 'HIDE', out, ws);
    checkList(out.signal, 'SIGNAL', out, ws);
    const T = bruteReach(ws.index, cap, injured ? 0.5 : 1, 30 * 60);
    assert.ok(Math.abs(T.size - out.reach_cells) <= 3, `reach ${out.reach_cells} vs ${T.size}`);
    for (const c of out.hide.concat(out.signal)) assert.ok(T.has(c.index), `${c.kind} ${c.rank} reachable from the start`);
  }
  assert.equal(JSON.stringify(res.estimate), before, 'the fix itself does not change');
});

test('walkableStart: the demo truth start is unchanged, injured moves off the 19.4 deg cell', () => {
  const ws = H.walkableStart(m, fix, { cap_deg: 30, r_m: 363 });
  assert.deepEqual([ws.ok, ws.moved, ws.reason, ws.index, ws.dist_m, ws.dir], [true, false, null, fixCell, 0, null]);
  const a = H.candidates(m, fix, { cap_deg: 30 }), b = H.candidates(m, ws, { cap_deg: 30 });
  assert.deepEqual([b.hide, b.signal, b.reach_cells], [a.hide, a.signal, a.reach_cells], 'same spots as from the fix');
  const wi = H.walkableStart(m, fix, { cap_deg: 18, r_m: 363 });
  console.log(`  demo injured: start ${wi.dist_m} m ${wi.dir}, slope ${wi.slope_deg}`);
  assert.ok(wi.moved && wi.reason === 'steep');
  const q = S.toEN(m, demo.lon, demo.lat);
  checkStart(wi, { e_m: q.e, n_m: q.n }, 18, 363);
  const out = H.candidates(m, wi, { injured: true });
  assert.equal(out.note, 'OK');
  for (const c of out.hide.concat(out.signal)) assert.ok(c.slope_deg <= 18 && c.walk_min <= 30);
});

test('walkableStart: water, a boxed-in ledge, nothing walkable inside the circle, off the map', () => {
  let lake = -1;
  for (let i = 0; i < m.n && lake < 0; i++) {
    const r = Math.floor(i / m.nx), c = i % m.nx;
    if (r < 6 || c < 6 || r >= m.ny - 6 || c >= m.nx - 6) continue;
    let wet = true;
    for (let a2 = -5; a2 <= 5 && wet; a2++) for (let b2 = -5; b2 <= 5; b2++) if (m.land[i + a2 * m.nx + b2]) { wet = false; break; }
    if (wet) lake = i;
  }
  const onLake = { e_m: m.E[lake], n_m: m.N[lake] };
  const none = H.walkableStart(m, onLake, { r_m: 100 });
  assert.deepEqual([none.ok, none.reason, none.index, none.alts.length], [false, 'water', -1, 0], 'nothing walkable inside 100 m');
  assert.equal(H.candidates(m, none, {}).note, 'STAY', 'no start, STAY as before');
  const shore = H.walkableStart(m, onLake, { r_m: 3000 });
  assert.ok(shore.moved && shore.reason === 'water');
  checkStart(shore, onLake, 30, 3000);
  // a cell under the cap whose walkable patch is tiny
  let ledge = -1;
  for (let i = 0; i < m.n && ledge < 0; i++) {
    const r = Math.floor(i / m.nx), c = i % m.nx;
    if (r < 12 || c < 12 || r >= m.ny - 12 || c >= m.nx - 12 || !m.land[i] || slopeAt(i) > 30 || patchSize(i, 30, OPEN) >= OPEN) continue;
    ledge = i;
  }
  assert.ok(ledge >= 0, 'a boxed-in ledge exists');
  const at = { e_m: m.E[ledge], n_m: m.N[ledge] };
  const wl = H.walkableStart(m, at, { cap_deg: 30, r_m: 600 });
  console.log(`  ledge of ${patchSize(ledge, 30, OPEN)} cells: start ${wl.ok ? wl.dist_m + ' m ' + wl.dir : 'none'}`);
  assert.equal(wl.reason, 'boxed');
  if (wl.ok) checkStart(wl, at, 30, 600);
  const off = H.walkableStart(m, { lat: 0, lon: 0 }, {});
  assert.deepEqual([off.ok, off.reason], [false, 'off_map']);
});
