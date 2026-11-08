'use strict';
// Follow, the next point and the turn on a locked route: node --test ridgeline/test
const test = require('node:test');
const assert = require('node:assert');
const F = require('../follow.js');

// an L: 300 m north in 10 m steps, then 200 m east, one point per 10 m
const L = [];
for (let k = 0; k <= 30; k++) L.push({ e: 0, n: k * 10 });
for (let k = 1; k <= 20; k++) L.push({ e: k * 10, n: 300 });

test('simplify keeps the ends and the corner, and drops the points on a straight line', () => {
  const keep = F.simplify(L, 15);
  assert.deepStrictEqual(keep, [0, 30, 50]);
});

test('build: length, waypoints at the corner and the end, time from the planner', () => {
  const times = L.map((p, k) => k * 8);
  const f = F.build({ pts: L, times });
  assert.strictEqual(Math.round(f.total), 500);
  assert.deepStrictEqual(f.wps.map(w => w.i), [30, 50]);
  assert.strictEqual(f.total_s, 400);
});

test('at the start, facing east, the corner is to the left', () => {
  const f = F.build({ pts: L });
  const g = F.update(f, { e: 0, n: 0, r95_m: 30 }, 90);
  assert.strictEqual(g.wp_index, 0);
  assert.strictEqual(Math.round(g.wp_m), 300);
  assert.strictEqual(Math.round(g.turn_deg), -90);
  assert.strictEqual(g.say, 'Turn left 90°');
  assert.strictEqual(g.offroute, false);
  assert.strictEqual(g.arrived, false);
});

test('straight ahead, turn around, and no heading', () => {
  const f = F.build({ pts: L });
  assert.strictEqual(F.update(f, { e: 0, n: 0 }, 5).say, 'Straight ahead');
  assert.strictEqual(F.update(f, { e: 0, n: 0 }, 180).say, 'Turn around');
  const g = F.update(f, { e: 0, n: 0 }, null);
  assert.strictEqual(g.turn_deg, null);
  assert.strictEqual(g.say, 'Head 0° N');
});

test('the next point moves on once the corner is passed', () => {
  const f = F.build({ pts: L });
  assert.strictEqual(F.update(f, { e: 3, n: 250 }, 0).wp_index, 0);
  const g = F.update(f, { e: 3, n: 285 }, 0);   // within 20 m of the corner along the route
  assert.strictEqual(g.wp_index, 1);
  assert.ok(g.last);
  assert.ok(g.turn_deg > 80 && g.turn_deg < 100, 'the end is to the right');
});

test('time left follows the planner times, not the straight share of the length', () => {
  // the first half takes 3 times as long per metre as the second
  const times = []; let t = 0;
  L.forEach((p, k) => { if (k) t += k <= 30 ? 30 : 10; times.push(t); });
  const f = F.build({ pts: L, times });
  const g = F.update(f, { e: 0, n: 150 }, 0);
  assert.strictEqual(Math.round(g.left_s), times[times.length - 1] - 15 * 30);
});

test('off route only when far from the route and outside half the circle', () => {
  const f = F.build({ pts: L });
  assert.strictEqual(F.update(f, { e: 200, n: 100, r95_m: 50 }, 0).offroute, true);
  assert.strictEqual(F.update(f, { e: 200, n: 100, r95_m: 900 }, 0).offroute, false, 'a wide circle is uncertain, not off route');
  assert.strictEqual(F.update(f, { e: 40, n: 100, r95_m: 10 }, 0).offroute, false, '40 m is still on the route');
});

test('arrived at the end, and not while off route', () => {
  const f = F.build({ pts: L });
  const g = F.update(f, { e: 185, n: 302, r95_m: 20 }, 90);
  assert.ok(g.arrived);
  assert.strictEqual(g.say, 'Arrived');
  const h = F.update(F.build({ pts: L }), { e: 200, n: 450, r95_m: 20 }, 90);
  assert.ok(!h.arrived && h.offroute);
});

test('a route that doubles back keeps its progress on the way out', () => {
  // out 300 m east, then back west 20 m to the north
  const H = [];
  for (let k = 0; k <= 30; k++) H.push({ e: k * 10, n: 0 });
  for (let k = 0; k <= 30; k++) H.push({ e: 300 - k * 10, n: 20 });
  const f = F.build({ pts: H });
  const g = F.update(f, { e: 30, n: 12, r95_m: 20 }, 90);   // nearer the return line, but the walk has just begun
  assert.ok(g.along_m < 60, `progress ${g.along_m} m stays on the way out`);
  for (let e = 40; e <= 300; e += 20) F.update(f, { e, n: 2, r95_m: 20 }, 90);   // walk out
  for (let e = 290; e >= 40; e -= 20) F.update(f, { e, n: 18, r95_m: 20 }, 270);   // and back
  const b = F.update(f, { e: 30, n: 12, r95_m: 20 }, 270);
  assert.ok(b.along_m > 500, `progress ${b.along_m} m is on the way back`);
});

test('legs give the instruction of the part being walked', () => {
  const f = F.build({ pts: L, legs: [{ i1: 30, say: 'North 300 m' }, { i1: 50, say: 'East 200 m' }] });
  assert.strictEqual(F.update(f, { e: 0, n: 100 }, 0).leg_say, 'North 300 m');
  assert.strictEqual(F.update(f, { e: 100, n: 300 }, 90).leg_say, 'East 200 m');
});
