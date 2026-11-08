// Node test for shared/route.js on the Moran solver grid: routes from the demo spot to the saved points, able and
// injured, never through water or over the slope limit, a pin that cannot be reached, and speed.
//   node --test tests/route.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {loadGrid} from '../shared/solver.js';
import {planRoute, LIMITS, aheadPoint, bearing, fmtTime} from '../shared/route.js';

const grid = loadGrid(JSON.parse(readFileSync(new URL('../terrain_viewer/data/moran/solver_grid.json', import.meta.url), 'utf8')));
const demo = {lon: grid.demo.lon, lat: grid.demo.lat};
const saved = grid.json.saved_points.points;
const cellOf = (lon, lat) => {
  const x = (lon - grid.bounds[0]) * grid.mPerDeg[0], y = (lat - grid.bounds[1]) * grid.mPerDeg[1];
  return Math.floor(grid.rows - y / grid.dy) * grid.cols + Math.floor(x / grid.dx);
};

test('saved points exist and are dry', () => {
  assert.ok(saved.length >= 2);
  for (const p of saved) assert.equal(grid.water[p.cell], 0, p.name);
});

for (const [mode, lim] of Object.entries(LIMITS)) {
  test(`routes from the demo spot to every saved point, ${mode} (${lim.max_slope_deg} deg)`, () => {
    for (const p of saved) {
      const r = planRoute(grid, demo, p, lim);
      console.log(`  ${mode.padEnd(7)} -> ${p.name.padEnd(28)} ${r.ok ? `${(r.length_m / 1000).toFixed(2)} km, ${fmtTime(r.time_s)}, climb ${r.climb_m} m, max ${r.max_slope_deg} deg, ${r.ms} ms. ${r.say}` : `no route: ${r.say}`}`);
      if (!r.ok) { assert.equal(r.reason, 'no_safe_route'); continue; }
      assert.ok(r.length_m > 0 && r.time_s > 0);
      assert.ok(r.max_slope_deg <= lim.max_slope_deg + 1e-9, 'slope over the limit');
      for (let k = 1; k < r.path.length; k++) assert.equal(grid.water[cellOf(r.path[k][0], r.path[k][1])], 0, 'route crosses water');
      assert.ok(r.runs.length >= 1 && r.legs.length >= 1);
      assert.match(r.say, /^It (bends|runs).*\.$/);
      assert.ok(!r.say.slice(0, -1).includes('. '), 'one sentence');
      const a = aheadPoint(r, 90);
      assert.ok(a && Number.isFinite(bearing(demo, a)));
    }
  });
}

test('injured is slower or has no route where able has one', () => {
  for (const p of saved) {
    const a = planRoute(grid, demo, p, LIMITS.able), i = planRoute(grid, demo, p, LIMITS.injured);
    if (a.ok && i.ok) assert.ok(i.time_s > a.time_s, p.name);
  }
});

test('a pin on a summit is not a destination at 18 deg', () => {
  const moran = grid.byId.get('mount-moran');
  const r = planRoute(grid, demo, moran, LIMITS.injured);
  assert.ok(!r.ok || !r.reached, 'the injured route must not end on the summit');
});

test('a pin in a lake ends at the shore or has no route', () => {
  let lake = -1;
  for (let i = 0; i < grid.water.length; i++) if (grid.water[i]) { lake = i; break; }
  assert.ok(lake >= 0);
  const c = lake % grid.cols, r0 = Math.floor(lake / grid.cols);
  const lon = grid.bounds[0] + (c + 0.5) * grid.dx / grid.mPerDeg[0], lat = grid.bounds[1] + (grid.rows - r0 - 0.5) * grid.dy / grid.mPerDeg[1];
  const r = planRoute(grid, demo, {lon, lat}, LIMITS.able);
  if (r.ok) for (let k = 1; k < r.path.length; k++) assert.equal(grid.water[cellOf(r.path[k][0], r.path[k][1])], 0);
});

test('each route plans in well under a second', () => {
  const ms = saved.map(p => planRoute(grid, demo, p, LIMITS.able).ms);
  console.log('  plan ms', ms);
  assert.ok(Math.max(...ms) < 1000);
});
