/*
 * RIDGELINE HIDE and SIGNAL. After the fix: where to get out of sight and where to be seen, within a short walk.
 * Terrain only. Trees, buildings, snow and people are not in the model, so a HIDE cell is a fold in the ground,
 * not a promise. No enemy or fire markers, and never call a spot "safe".
 *
 * API (browser: window.RidgelineHide, Node: require('./hide.js'))
 *
 *   RidgelineHide.candidates(grid, fix, opts)
 *     -> {hide: [..3], signal: [..3], reach_cells, minutes, cap_deg, injured, note, ms}
 *
 *   grid   the object RidgelineSolver.load(json) returns (the `model` the phone and the board already hold).
 *          Reads grid.nx, ny, n, dx, dy, elev, land, E, N, bounds and peaks[].vis. moran.json is not parsed again.
 *          The slope and the summit count per cell are computed once per grid and cached.
 *   fix    {lat, lon}, or {e_m, n_m}, or a solver result (its .estimate is used).
 *   opts   {cap_deg: 30 or 18, minutes: 30, injured: false}
 *          injured walks at half speed. cap_deg defaults to 18 when injured, else 30.
 *
 *   hide     reachable cells seen by the fewest summits, ties go to the shorter walk
 *   signal   reachable cells on ground of 15 deg or less seen by the most summits, ties go to the shorter walk
 *            Each list holds up to 3, ranked 1 to 3, at least 250 m apart. A HIDE cell is seen by fewer than all
 *            summits and a SIGNAL cell by at least one.
 *   candidate {kind: 'HIDE' or 'SIGNAL', rank, lat, lon, seen_by, of, walk_min, slope_deg, why, e_m, n_m, index}
 *     seen_by    how many of the `of` summits have a line of sight to a person standing there (1.7 m eye height)
 *     walk_min   walking time from the fix in whole minutes (0 means under half a minute)
 *     slope_deg  ground slope of the 30 m cell
 *     why        one plain sentence, e.g. "Out of sight of 21 of 23 summits. 12 min walk."
 *   reach_cells  how many 30 m cells (the fix cell included) can be walked to within `minutes`
 *   note         'OK', or 'STAY' with empty lists when no other cell can be reached under the slope limit
 *
 *   RidgelineHide.marks(out) -> [{kind, rank, lat, lon, why}]   the small list the phone posts as summary.marks
 *   RidgelineHide.layers(grid) -> {slope, seen, of}   per cell slope (deg) and summit count, cached. Call it once
 *          after loading to move the one-time cost (about 40 ms in Node) out of the first fix.
 *
 * Reach: Dijkstra from the fix cell over the 30 m grid (8 neighbours), Tobler off-trail walking time
 * 6 km/h x exp(-3.5 |grade + 0.05|) x 0.6 (the same rule as terrain_viewer/team8_nav.js), half speed when injured,
 * water blocked, cells steeper than cap_deg blocked, no diagonal step between two blocked cells.
 * Slope is the central difference slope of the 30 m grid (as in scripts/pick_demo.js). The fix cell is always
 * the start, even when it is steeper than the limit, but it is only offered as a candidate when it is not.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.RidgelineHide = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const R2D = 180 / Math.PI;
  const VK = 0.6 * 6.0 / 3.6;   // m/s, Tobler peak speed off-trail, as in team8_nav.js

  const DEFAULTS = Object.freeze({
    cap_deg: 30,              // healthy slope limit
    injured_cap_deg: 18,      // slope limit when injured and no cap_deg is given
    minutes: 30,
    injured: false,
    signal_slope_deg: 15,     // open ground that air and ridges can see
    spacing_m: 250,
    count: 3,
    snap_cells: 3,            // a fix on water starts from the nearest land cell within this many cells
  });

  const now = () => (typeof performance !== 'undefined' ? performance : Date).now();
  const cache = typeof WeakMap === 'function' ? new WeakMap() : null;

  // Slope (deg) and summit count per cell, once per grid.
  function layers(g) {
    let L = cache ? cache.get(g) : g._hide;
    if (L) return L;
    const { nx, ny, n, dx, dy, elev } = g;
    const slope = new Float32Array(n), seen = new Uint8Array(n);
    for (let r = 0; r < ny; r++) {
      for (let c = 0; c < nx; c++) {
        const i = r * nx + c;
        const w = c > 0 ? i - 1 : i, e = c < nx - 1 ? i + 1 : i;
        const no = r > 0 ? i - nx : i, so = r < ny - 1 ? i + nx : i;
        const gx = (elev[e] - elev[w]) / ((e - w) * dx);
        const gy = (elev[no] - elev[so]) / (((so - no) / nx) * dy);
        slope[i] = Math.atan(Math.hypot(gx, gy)) * R2D;
      }
    }
    for (const p of g.peaks) {
      const v = p.vis;
      for (let i = 0; i < n; i++) seen[i] += v[i];
    }
    L = { slope, seen, of: g.peaks.length };
    if (cache) cache.set(g, L); else g._hide = L;
    return L;
  }

  function lonLat(g, e, n) {
    const [w, s, ea, no] = g.bounds;
    return { lon: w + (e / (g.nx * g.dx)) * (ea - w), lat: s + (n / (g.ny * g.dy)) * (no - s) };
  }

  // The fix cell, from {lat, lon}, {e_m, n_m} or a solver result. -1 when off the map.
  function fixCell(g, fix) {
    if (!fix) return -1;
    const f = fix.estimate || fix;
    if (Number.isInteger(f.index) && f.index >= 0 && f.index < g.n) return f.index;
    let e, n;
    if (Number.isFinite(f.e_m) && Number.isFinite(f.n_m)) { e = f.e_m; n = f.n_m; }
    else if (Number.isFinite(f.lat) && Number.isFinite(f.lon)) {
      const [w, s, ea, no] = g.bounds;
      e = ((f.lon - w) / (ea - w)) * g.nx * g.dx;
      n = ((f.lat - s) / (no - s)) * g.ny * g.dy;
    } else return -1;
    const c = Math.floor(e / g.dx), r = Math.floor(g.ny - n / g.dy);
    if (c < 0 || r < 0 || c >= g.nx || r >= g.ny) return -1;
    return r * g.nx + c;
  }

  function nearestLand(g, i, k) {
    if (i < 0 || g.land[i]) return i;
    const r0 = Math.floor(i / g.nx), c0 = i % g.nx;
    let best = -1, bd = Infinity;
    for (let r = Math.max(0, r0 - k); r <= Math.min(g.ny - 1, r0 + k); r++) {
      for (let c = Math.max(0, c0 - k); c <= Math.min(g.nx - 1, c0 + k); c++) {
        const j = r * g.nx + c;
        if (!g.land[j]) continue;
        const d = Math.hypot((c - c0) * g.dx, (r - r0) * g.dy);
        if (d < bd) { bd = d; best = j; }
      }
    }
    return best;
  }

  // One Dijkstra from the start. Returns the walking time per cell (Infinity when not reached) and the cells reached.
  function walk(g, L, start, cap, speed, limitS) {
    const { nx, ny, n, dx, dy, elev, land } = g, slope = L.slope;
    const T = new Float32Array(n).fill(Infinity);
    const heap = new Int32Array(n), pos = new Int32Array(n).fill(-1);
    let hn = 0;
    const up = k => {
      const x = heap[k], tx = T[x];
      while (k > 0) { const p = (k - 1) >> 1; if (T[heap[p]] <= tx) break; heap[k] = heap[p]; pos[heap[k]] = k; k = p; }
      heap[k] = x; pos[x] = k;
    };
    const down = k => {
      const x = heap[k], tx = T[x];
      for (;;) {
        const l = 2 * k + 1;
        if (l >= hn) break;
        const m = l + 1 < hn && T[heap[l + 1]] < T[heap[l]] ? l + 1 : l;
        if (T[heap[m]] >= tx) break;
        heap[k] = heap[m]; pos[heap[k]] = k; k = m;
      }
      heap[k] = x; pos[x] = k;
    };
    const blocked = j => !land[j] || slope[j] > cap;
    const diag = Math.hypot(dx, dy);
    const done = [];
    T[start] = 0; heap[0] = start; pos[start] = 0; hn = 1;
    while (hn) {
      const i = heap[0];
      hn--;
      if (hn) { heap[0] = heap[hn]; pos[heap[0]] = 0; down(0); }
      pos[i] = -2;
      const ti = T[i];
      if (ti > limitS) break;
      done.push(i);
      const r = (i / nx) | 0, c = i - r * nx, zi = elev[i];
      for (let dr = -1; dr <= 1; dr++) {
        const rr = r + dr;
        if (rr < 0 || rr >= ny) continue;
        for (let dc = -1; dc <= 1; dc++) {
          if (!dr && !dc) continue;
          const cc = c + dc;
          if (cc < 0 || cc >= nx) continue;
          const j = rr * nx + cc;
          if (pos[j] === -2 || blocked(j)) continue;
          if (dr && dc && blocked(r * nx + cc) && blocked(rr * nx + c)) continue;
          const len = dr && dc ? diag : dr ? dy : dx;
          const tj = ti + len / (VK * Math.exp(-3.5 * Math.abs((elev[j] - zi) / len + 0.05)) * speed);
          if (tj < T[j]) {
            T[j] = tj;
            if (pos[j] < 0) { heap[hn] = j; pos[j] = hn; hn++; }
            up(pos[j]);
          }
        }
      }
    }
    for (let k = 0; k < hn; k++) T[heap[k]] = Infinity;   // queued but beyond the time limit
    return { T, done };
  }

  function walkText(s) {
    return s < 30 ? 'Under 1 min walk.' : `${Math.round(s / 60)} min walk.`;
  }

  function candidates(grid, fix, opts) {
    const t0 = now();
    const o = Object.assign({}, DEFAULTS, opts || {});
    const injured = !!o.injured;
    const cap = Number.isFinite(+(opts || {}).cap_deg) ? +opts.cap_deg : injured ? o.injured_cap_deg : o.cap_deg;
    const minutes = Number.isFinite(+o.minutes) ? +o.minutes : DEFAULTS.minutes;
    const L = layers(grid);
    const out = { hide: [], signal: [], reach_cells: 0, minutes, cap_deg: cap, injured, note: 'STAY', ms: 0 };
    const start = nearestLand(grid, fixCell(grid, fix), o.snap_cells);
    if (start < 0 || !(minutes > 0)) { out.ms = now() - t0; return out; }
    const { T, done } = walk(grid, L, start, cap, injured ? 0.5 : 1, minutes * 60);
    out.reach_cells = done.length;
    if (done.length <= 1) { out.ms = now() - t0; return out; }

    const { slope, seen, of } = L;
    const ok = done.filter(i => slope[i] <= cap);
    const hideOrder = ok.filter(i => seen[i] < of).sort((a, b) => seen[a] - seen[b] || T[a] - T[b]);
    const signalOrder = ok.filter(i => seen[i] > 0 && slope[i] <= o.signal_slope_deg).sort((a, b) => seen[b] - seen[a] || T[a] - T[b]);
    const make = (kind, rank, i) => {
      const ll = lonLat(grid, grid.E[i], grid.N[i]);
      const s = seen[i], sl = slope[i], deg = Math.round(sl);
      const why = kind === 'HIDE'
        ? `Out of sight of ${s === 0 ? 'all ' + of : of - s + ' of ' + of} summits. ${walkText(T[i])}`
        : `Seen from ${s === of ? 'all ' + of : s + ' of ' + of} summits on ${deg <= 1 ? 'flat' : 'gentle ' + deg + '°'} ground. ${walkText(T[i])}`;
      return {
        kind, rank, lat: +ll.lat.toFixed(6), lon: +ll.lon.toFixed(6), seen_by: s, of,
        walk_min: Math.round(T[i] / 60), slope_deg: Math.round(sl * 10) / 10, why,
        e_m: grid.E[i], n_m: grid.N[i], index: i,
      };
    };
    const pick = (order, kind) => {
      const got = [];
      for (const i of order) {
        if (got.length >= o.count) break;
        if (got.some(j => Math.hypot(grid.E[i] - grid.E[j], grid.N[i] - grid.N[j]) < o.spacing_m)) continue;
        got.push(i);
      }
      return got.map((i, k) => make(kind, k + 1, i));
    };
    out.hide = pick(hideOrder, 'HIDE');
    out.signal = pick(signalOrder, 'SIGNAL');
    out.note = out.hide.length || out.signal.length ? 'OK' : 'STAY';
    out.ms = now() - t0;
    return out;
  }

  // The small list the phone posts as summary.marks for the board.
  function marks(out) {
    return out ? [].concat(out.hide || [], out.signal || []).map(c => ({ kind: c.kind, rank: c.rank, lat: c.lat, lon: c.lon, why: c.why })) : [];
  }

  return Object.freeze({ DEFAULTS, candidates, marks, layers });
});
