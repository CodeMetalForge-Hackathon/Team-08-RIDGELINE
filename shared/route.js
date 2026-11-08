// RIDGELINE safe route on the 30 m solver grid. Plain ES module, no dependencies, same file in the browser and in Node.
//
//   import {loadGrid} from './solver.js';
//   import {planRoute} from './route.js';
//   const r = planRoute(grid, {lon, lat}, {lon, lat}, {max_slope_deg: 30, speed: 1});
//
// The model follows the team's judge-screen planner (terrain_viewer/team8_nav.js) so both screens agree:
// Dijkstra over 8 neighbours, Tobler's hiking function off trail (x 0.6, about 3.6 km/h on the flat), no water cells,
// no cell whose 30 m slope is over the limit (30 deg able, 18 deg injured at half speed), a 5-point running mean on the
// path, difficulty runs (easy up to 10 deg, moderate to 20, steep above), short legs for turn-by-turn.
// The start cell is always allowed (the person is standing on it). If the goal itself is not reachable, the route goes
// to the nearest reachable cell when that is close (gap up to 250 m), otherwise there is no safe route.
// Synthetic terrain model only: no trails, snow, vegetation, rockfall or river crossings are known to it.

export const ROUTE_DEFAULTS = {max_slope_deg: 30, speed: 1, off_trail: 0.6, max_hours: 9, reach_gap_m: 60, near_gap_m: 250};
export const LIMITS = {able: {max_slope_deg: 30, speed: 1}, injured: {max_slope_deg: 18, speed: 0.5}};
export const CLASS_NAMES = ['easy', 'moderate', 'steep'];
const R2D = 180 / Math.PI, DIR8 = ['north', 'northeast', 'east', 'southeast', 'south', 'southwest', 'west', 'northwest'];
const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now());
const wrap = d => ((d % 360) + 360) % 360;
const cls = s => (s <= 10 ? 0 : s <= 20 ? 1 : 2);

function frame(grid) {
  const [W, S] = grid.bounds, [mx, my] = grid.mPerDeg, H = grid.rows * grid.dy;
  return {
    x: lon => (lon - W) * mx, y: lat => (lat - S) * my, lon: x => W + x / mx, lat: y => S + y / my,
    cell: (x, y) => {
      const c = Math.floor(x / grid.dx), r = Math.floor((H - y) / grid.dy);
      return c < 0 || r < 0 || c >= grid.cols || r >= grid.rows ? -1 : r * grid.cols + c;
    },
    cx: i => ((i % grid.cols) + 0.5) * grid.dx, cy: i => H - (Math.floor(i / grid.cols) + 0.5) * grid.dy,
  };
}

// walking time for one step, seconds (Tobler: 6 km/h x exp(-3.5 |grade + 0.05|), off trail x 0.6)
function stepTime(L, dz, o) {
  const v = o.off_trail * (6 / 3.6) * Math.exp(-3.5 * Math.abs(dz / Math.max(L, 1e-6) + 0.05));
  return L / (v * o.speed);
}

// one Dijkstra from the start: every reachable cell gets its walking time and the step it came from
export function reach(grid, from, opts) {
  const o = {...ROUTE_DEFAULTS, ...(opts || {})}, F = frame(grid), t0 = now();
  const R = grid.rows, C = grid.cols, N = R * C, Z = grid.elev, SL = grid.slope, WA = grid.water;
  const start = F.cell(F.x(from.lon), F.y(from.lat));
  if (start < 0) return null;
  const T = new Float32Array(N).fill(Infinity), prev = new Int32Array(N).fill(-1);
  const heap = new Int32Array(N), pos = new Int32Array(N).fill(-1);
  let hn = 0;
  const swap = (a, b) => { const x = heap[a]; heap[a] = heap[b]; heap[b] = x; pos[heap[a]] = a; pos[heap[b]] = b; };
  const up = k => { while (k > 0) { const p = (k - 1) >> 1; if (T[heap[p]] <= T[heap[k]]) break; swap(p, k); k = p; } };
  const down = k => { for (;;) { const l = 2 * k + 1, r = l + 1; let m = k; if (l < hn && T[heap[l]] < T[heap[m]]) m = l; if (r < hn && T[heap[r]] < T[heap[m]]) m = r; if (m === k) break; swap(m, k); k = m; } };
  const NB = [];
  for (let dr = -1; dr <= 1; dr++) for (let dc = -1; dc <= 1; dc++) if (dr || dc) NB.push([dr, dc, Math.hypot(dr * grid.dy, dc * grid.dx)]);
  T[start] = 0; heap[0] = start; pos[start] = 0; hn = 1;
  const lim = o.max_hours * 3600;
  while (hn) {
    const i = heap[0]; hn--; if (hn) { heap[0] = heap[hn]; pos[heap[0]] = 0; down(0); } pos[i] = -2;
    const ti = T[i]; if (ti > lim) break;
    const r = (i / C) | 0, c = i - r * C, zi = Z[i];
    for (let q = 0; q < 8; q++) {
      const rr = r + NB[q][0], cc = c + NB[q][1];
      if (rr < 0 || cc < 0 || rr >= R || cc >= C) continue;
      const j = rr * C + cc;
      if (pos[j] === -2 || WA[j] || SL[j] > o.max_slope_deg) continue;
      const tj = ti + stepTime(NB[q][2], Z[j] - zi, o);
      if (tj < T[j]) { T[j] = tj; prev[j] = i; if (pos[j] < 0) { heap[hn] = j; pos[j] = hn; hn++; } up(pos[j]); }
    }
  }
  return {T, prev, start, from: {lon: from.lon, lat: from.lat}, opts: o, ms: Math.round(now() - t0)};
}

function dp(xs, ys, i0, i1, eps, keep) {   // Douglas-Peucker, for the legs
  let m = -1, md = eps;
  const ax = xs[i0], ay = ys[i0], bx = xs[i1], by = ys[i1], L = Math.hypot(bx - ax, by - ay) || 1e-9;
  for (let i = i0 + 1; i < i1; i++) { const d = Math.abs((bx - ax) * (ay - ys[i]) - (ax - xs[i]) * (by - ay)) / L; if (d > md) { md = d; m = i; } }
  if (m > 0) { dp(xs, ys, i0, m, eps, keep); keep.push(m); dp(xs, ys, m, i1, eps, keep); }
}

// what lies on the straight line, so the sentence can say what the route bends around
function straightLine(grid, F, a, b, cap) {
  const ax = F.x(a.lon), ay = F.y(a.lat), bx = F.x(b.lon), by = F.y(b.lat), L = Math.hypot(bx - ax, by - ay), n = Math.max(1, Math.ceil(L / 15));
  let water = false, steep = false;
  for (let k = 1; k < n; k++) {
    const i = F.cell(ax + (bx - ax) * k / n, ay + (by - ay) * k / n);
    if (i < 0) continue;
    if (grid.water[i]) water = true; else if (grid.slope[i] > cap) steep = true;
  }
  return {length_m: Math.round(L), water, steep};
}

const mRound = v => (v >= 1000 ? `${(v / 1000).toFixed(1)} km` : `${Math.max(10, Math.round(v / 10) * 10)} m`);
export const fmtTime = s => { const m = Math.max(1, Math.round(s / 60)); return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, '0')} min`; };

// the route in one sentence: what it bends around, and its steepest part
export function describe(R) {
  const cap = R.cap_deg, s = R.straight, around = [];
  if (s.water) around.push('water');
  if (s.steep) around.push(`ground steeper than ${cap}°`);
  const head = around.length ? `It bends around ${around.join(' and ')}` : 'It runs almost straight';
  const st = R.runs.filter(r => r.cls === 2 && r.len >= 50).sort((a, b) => b.max - a.max || b.len - a.len)[0];
  const tail = st ? `, and its steepest part, ${st.max}° for ${mRound(st.len)}, ${st.at < 30 ? 'is at the start' : `starts ${mRound(st.at)} in`}`
    : R.runs.some(r => r.cls === 1) ? ', and nothing on it is steeper than 20°' : ', on easy ground all the way';
  const end = R.reached ? '' : `, and stops ${mRound(R.gap_m)} short of the pin, which is on ground it cannot use`;
  return `${head}${tail}${end}.`;
}

// a route from a reach() result to a goal
export function routeTo(grid, rt, to) {
  const o = rt.opts, F = frame(grid), C = grid.cols;
  const gx = F.x(to.lon), gy = F.y(to.lat);
  let goal = F.cell(gx, gy);
  if (goal < 0) return {ok: false, reason: 'outside', say: 'That point is outside the map.'};
  let reached = rt.T[goal] < Infinity;
  if (!reached) {   // the reachable cell nearest to the goal
    const K = Math.ceil(o.near_gap_m / Math.min(grid.dx, grid.dy)) + 1, gr = (goal / C) | 0, gc = goal - gr * C;
    let best = -1, bd = Infinity;
    for (let r = Math.max(0, gr - K); r <= Math.min(grid.rows - 1, gr + K); r++) for (let c = Math.max(0, gc - K); c <= Math.min(C - 1, gc + K); c++) {
      const i = r * C + c; if (!(rt.T[i] < Infinity)) continue;
      const d = Math.hypot(F.cx(i) - gx, F.cy(i) - gy); if (d < bd) { bd = d; best = i; }
    }
    if (best < 0 || bd > o.near_gap_m) {
      return {ok: false, reason: 'no_safe_route', cap_deg: o.max_slope_deg, ms: rt.ms,
        say: `No way there without water or ground steeper than ${o.max_slope_deg}°.`};
    }
    goal = best; reached = bd <= o.reach_gap_m;
  }
  const cells = [goal];
  while (cells[cells.length - 1] !== rt.start) { const p = rt.prev[cells[cells.length - 1]]; if (p < 0) return {ok: false, reason: 'no_path', say: 'No path.'}; cells.push(p); }
  cells.reverse();
  let xs = cells.map(F.cx), ys = cells.map(F.cy);
  xs[0] = F.x(rt.from.lon); ys[0] = F.y(rt.from.lat);
  if (xs.length > 7) {   // 5-point running mean, ends kept
    const sx = xs.slice(), sy = ys.slice();
    for (let i = 2; i < xs.length - 2; i++) { sx[i] = (xs[i - 2] + xs[i - 1] + xs[i] + xs[i + 1] + xs[i + 2]) / 5; sy[i] = (ys[i - 2] + ys[i - 1] + ys[i] + ys[i + 1] + ys[i + 2]) / 5; }
    xs = sx; ys = sy;
  }
  const n = xs.length, zs = [], sl = [], path = [], dist = [0], ts = [0];
  let t = 0, L = 0, climb = 0, desc = 0, smax = 0;
  for (let k = 0; k < n; k++) {
    const i = Math.max(0, F.cell(xs[k], ys[k])), s = k === 0 ? Math.min(grid.slope[cells[0]], o.max_slope_deg) : grid.slope[i];
    zs.push(grid.elev[i]); sl.push(s); if (k && s > smax) smax = s;
    path.push([+F.lon(xs[k]).toFixed(6), +F.lat(ys[k]).toFixed(6), Math.round(zs[k])]);
    if (k) {
      const l = Math.hypot(xs[k] - xs[k - 1], ys[k] - ys[k - 1]), dz = zs[k] - zs[k - 1];
      t += stepTime(l, dz, o); L += l; ts.push(t); dist.push(L); if (dz > 0) climb += dz; else desc -= dz;
    }
  }
  // runs of one difficulty, runs under 40 m join the one before
  let runs = [];
  for (let k = 1; k < n; k++) {
    const c = Math.max(cls(sl[k - 1]), cls(sl[k])), last = runs[runs.length - 1];
    if (last && last.cls === c) last.i1 = k; else runs.push({cls: c, i0: k - 1, i1: k});
  }
  for (let q = 0; q < runs.length && runs.length > 1;) {
    const r = runs[q];
    if (dist[r.i1] - dist[r.i0] >= 40) { q++; continue; }
    if (q > 0) runs[q - 1].i1 = r.i1; else runs[1].i0 = r.i0;
    runs.splice(q, 1);
  }
  runs = runs.reduce((a, r) => { const l = a[a.length - 1]; if (l && l.cls === r.cls) l.i1 = r.i1; else a.push({...r}); return a; }, []);
  runs.forEach(r => { r.len = Math.round(dist[r.i1] - dist[r.i0]); r.at = Math.round(dist[r.i0]); r.dz = Math.round(zs[r.i1] - zs[r.i0]); r.max = Math.round(Math.max(...sl.slice(r.i0, r.i1 + 1))); r.name = CLASS_NAMES[r.cls]; });
  const keep = [0]; if (n > 1) { dp(xs, ys, 0, n - 1, 30, keep); keep.push(n - 1); } keep.sort((a, b) => a - b);
  const legs = [];
  for (let q = 1; q < keep.length; q++) {
    const i0 = keep[q - 1], i1 = keep[q]; if (i1 <= i0) continue;
    const b = wrap(Math.atan2(xs[i1] - xs[i0], ys[i1] - ys[i0]) * R2D), d = dist[i1] - dist[i0], dz = zs[i1] - zs[i0];
    const say = `Head ${DIR8[Math.round(b / 45) % 8]} (${Math.round(b)}°) for ${mRound(d)}` + (dz > 25 ? `, climb ${Math.round(dz)} m` : dz < -25 ? `, descend ${Math.round(-dz)} m` : '');
    legs.push({i0, i1, bearing_deg: Math.round(b * 10) / 10, dist_m: Math.round(d), dz_m: Math.round(dz), t1_s: Math.round(ts[i1]), say});
  }
  const R = {ok: true, reached, gap_m: reached ? 0 : Math.round(Math.hypot(F.cx(goal) - gx, F.cy(goal) - gy)), cap_deg: o.max_slope_deg, speed: o.speed,
    path, dist: dist.map(Math.round), length_m: Math.round(L), time_s: Math.round(t), climb_m: Math.round(climb), descent_m: Math.round(desc),
    max_slope_deg: Math.round(smax * 10) / 10, runs, legs, straight: straightLine(grid, F, rt.from, to, o.max_slope_deg), ms: rt.ms};
  R.say = describe(R);
  return R;
}

export function planRoute(grid, from, to, opts) {
  const t0 = now(), rt = reach(grid, from, opts);
  if (!rt) return {ok: false, reason: 'outside', say: 'The start is outside the map.'};
  const R = routeTo(grid, rt, to);
  R.ms = Math.round(now() - t0);
  return R;
}

// the next point about `ahead` metres along the route from the start, for the direction arrow
export function aheadPoint(R, ahead = 90) {
  if (!R || !R.ok || R.path.length < 2) return null;
  const k = R.dist.findIndex(d => d >= ahead);
  return R.path[k < 0 ? R.path.length - 1 : Math.max(1, k)];
}

// initial bearing, degrees true, from a to b ([lon, lat] or {lon, lat})
export function bearing(a, b) {
  const [lo1, la1] = Array.isArray(a) ? a : [a.lon, a.lat], [lo2, la2] = Array.isArray(b) ? b : [b.lon, b.lat];
  const p1 = la1 / R2D, p2 = la2 / R2D, dl = (lo2 - lo1) / R2D;
  return wrap(Math.atan2(Math.sin(dl) * Math.cos(p2), Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl)) * R2D);
}
