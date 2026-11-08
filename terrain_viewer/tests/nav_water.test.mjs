// A person on foot cannot cross water. Nothing the route planner draws (the route, and the thin red line for the last
// stretch that is too steep) may lie on water, wherever the pin goes: in a lake, on an island, on the shore, on land.
//   node --test terrain_viewer/tests/nav_water.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {loadNav, decodePNG} from './nav_harness.mjs';

const PILOTS = 14;

const areas = new Map(), tripSets = new Map();
const area = id => { if (!areas.has(id)) areas.set(id, makeArea(id)); return areas.get(id); };
// one set of trips per mountain, shared by the tests (each trip is a pilot, a pin and a walking limit)
const tripsFor = async id => { if (!tripSets.has(id)) tripSets.set(id, trips(await area(id), 20261108)); return tripSets.get(id); };

async function makeArea(id) {
  const {nav, D} = await loadNav(id);
  const ng = decodePNG(Buffer.from(D.img.nogo.split(',')[1], 'base64'));
  const W = ng.width, H = ng.height, B = D.meta.bounds, water = new Uint8Array(W * H);
  for (let i = 0; i < W * H; i++) water[i] = ng.data[4 * i] > 127 ? 1 : 0;
  const ll = i => [B[0] + ((i % W) + 0.5) / W * (B[2] - B[0]), B[3] - (((i / W) | 0) + 0.5) / H * (B[3] - B[1])];
  // land parts that touch no edge of the sheet are islands
  const lab = new Int32Array(W * H).fill(-1), islands = [];
  for (let s = 0; s < W * H; s++) {
    if (water[s] || lab[s] >= 0) continue;
    const st = [s], cells = []; let edge = false; lab[s] = s;
    while (st.length) {
      const i = st.pop(), r = (i / W) | 0, c = i - r * W; cells.push(i);
      if (!r || !c || r === H - 1 || c === W - 1) edge = true;
      for (const [dr, dc] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
        const rr = r + dr, cc = c + dc, j = rr * W + cc;
        if (rr >= 0 && cc >= 0 && rr < H && cc < W && !water[j] && lab[j] < 0) { lab[j] = s; st.push(j); }
      }
    }
    if (!edge && cells.length >= 4) islands.push(cells);
  }
  const wet = [], shore = [];
  for (let i = 0; i < W * H; i++) {
    if (water[i]) { wet.push(i); continue; }
    const r = (i / W) | 0, c = i - r * W;
    near: for (let dr = -3; dr <= 3; dr++) for (let dc = -3; dc <= 3; dc++) {
      const rr = r + dr, cc = c + dc;
      if (rr >= 0 && cc >= 0 && rr < H && cc < W && water[rr * W + cc]) { shore.push(i); break near; }
    }
  }
  const M = 111320 * Math.cos((B[1] + B[3]) / 2 * Math.PI / 180), MY = 110950;
  const metres = (a, b) => Math.hypot((b[0] - a[0]) * M, (b[1] - a[1]) * MY);
  const wetM = pts => {   // metres of a lon,lat line on water, every 2 m
    let m = 0;
    for (let k = 1; k < pts.length; k++) {
      const a = pts[k - 1], b = pts[k], L = metres(a, b), n = Math.max(1, Math.ceil(L / 2));
      for (let s = 0; s < n; s++) { const t = (s + 0.5) / n; if (nav.isWater(a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))) m += L / n; }
    }
    return m;
  };
  return {nav, ll, wet, shore, islands, wetM, metres};
}

function trips(A, seed) {
  const {nav, ll, wet, shore, islands} = A, rng = nav.rng(seed), pick = a => a[Math.floor(rng() * a.length)], out = [];
  for (let p = 0; p < PILOTS; p++) {
    const st = nav.randomSpot(rng); if (!st) continue;
    const ks = [];
    for (let q = 0; q < 4 && wet.length; q++) ks.push(['water', ll(pick(wet))]);
    for (let q = 0; q < 4 && shore.length; q++) ks.push(['shore', ll(pick(shore))]);
    for (let q = 0; q < 2 && islands.length; q++) ks.push(['island', ll(pick(pick(islands)))]);
    for (let q = 0; q < 3; q++) { const s = nav.randomSpot(rng); if (s) ks.push(['land', s]); }
    for (const [cap, speed] of [[30, 1], [18, 0.5]]) {
      const rt = nav.reach(st[0], st[1], cap, speed, 9 * 3600);
      for (const [kind, d] of ks) {
        const pp = nav.pathTo(rt, d[0], d[1]);
        out.push({kind, d, cap, r: pp && nav.buildRoute(pp, rt)});
      }
    }
  }
  return out;
}

for (const id of ['moran', 'washington', 'fuji']) {
  test(`${id}: no drawn route or red line lies on water`, async () => {
    const A = await area(id), T = await tripsFor(id);
    let routes = 0, worst = 0, where = null;
    for (const t of T) {
      if (!t.r) continue;
      routes++;
      const m = A.wetM(t.r.path) + (t.r.why === 'steep' && t.r.tail ? A.wetM(t.r.tail) : 0);
      if (m > worst) { worst = m; where = t; }
    }
    assert.ok(routes > 50, `only ${routes} routes`);
    assert.equal(worst, 0, `a ${where && where.kind} pin at ${where && where.d} drew ${worst.toFixed(1)} m on water (${where && where.r.why})`);
  });
}

test('moran: a pin in a lake goes to the closest reachable shore and says so', async () => {
  const A = await area('moran'), T = (await tripsFor('moran')).filter(t => t.kind === 'water' && t.r);
  assert.ok(T.length > 20 && A.wet.length > 0);
  for (const t of T) {
    assert.equal(t.r.why, 'water');
    assert.equal(t.r.reached, false);
    assert.match(t.r.note, /in the water/);
    assert.equal(A.nav.isWater(...t.r.path[t.r.path.length - 1].slice(0, 2)), false, 'the route ends on dry land');
  }
});

test('moran: a pin on an island is cut off by water, with no line across', async () => {
  const A = await area('moran'), T = (await tripsFor('moran')).filter(t => t.kind === 'island' && t.r);
  assert.ok(A.islands.length >= 1 && T.length > 10);
  for (const t of T) {
    assert.equal(t.r.why, 'island');
    assert.equal(t.r.tail, null);
    assert.match(t.r.note, /cut off by water/);
  }
});

test('moran: a route that gets there ends on the pin itself', async () => {
  const A = await area('moran'), T = (await tripsFor('moran')).filter(t => t.r && t.r.reached);
  assert.ok(T.length > 20);
  for (const t of T) assert.ok(A.metres(t.r.path[t.r.path.length - 1], t.d) < 1, `ends ${A.metres(t.r.path[t.r.path.length - 1], t.d).toFixed(1)} m from the pin`);
});

test('moran: the stretch that is too steep is drawn over land and ends on the pin', async () => {
  const A = await area('moran'), T = (await tripsFor('moran')).filter(t => t.r && t.r.why === 'steep');
  assert.ok(T.length > 10);
  for (const t of T) {
    assert.ok(t.r.tail.length >= 2);
    assert.ok(A.metres(t.r.tail[0], t.r.path[t.r.path.length - 1]) < 1, 'the red line starts where the route stops');
    assert.ok(A.metres(t.r.tail[t.r.tail.length - 1], t.d) < 1, 'and ends on the pin');
    assert.match(t.r.note, /steeper than/);
  }
});
