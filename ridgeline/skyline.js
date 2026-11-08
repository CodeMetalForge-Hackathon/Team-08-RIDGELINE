/*
 * RIDGELINE expected skyline. The horizon the pilot should see toward the next peak, drawn from the
 * current estimate, so they can check by eye that the crosshair is on the right summit before the Lock.
 *
 * Browser: <script src="/solver.js"></script><script src="/skyline.js"></script>  ->  window.RidgelineSkyline
 * Node:    const K = require('./skyline.js')
 *
 * RidgelineSkyline.profile(grid, from, peakId, opts) -> profile, or null when `from` is off the tile or the
 * peak id is unknown.
 *   grid    the object RidgelineSolver.load(json) returns (30 m grid, peaks, eye 1.7 m, rEff)
 *   from    {lat, lon}. {e_m, n_m} is used first when present, so result.estimate can be passed as is.
 *   peakId  for example result.next_peak_id
 *   opts    half_fov_deg 15, step_deg 0.25 (121 samples, the middle one is daz 0), eye_m (grid.eye),
 *           az_center_deg (default the true bearing to the peak), min_range_m 45, step_m 15,
 *           summit_radius_m 45 (0 leaves the summits as flat as the 30 m grid has them, see below)
 *   returns {
 *     az_center, fov_deg                     window center in degrees true, full window width
 *     samples: [{daz, elev_deg, dist_m}]     daz from -15 to +15, left to right as the eye sees it; the
 *                                            highest elevation angle on that ray and how far that ridge is
 *     peak: {id, name, daz, elev_deg, dist_m, bearing_deg, hidden, clear_deg}
 *                                            elev_deg is the summit angle (same formula as solver.js);
 *                                            hidden is true when nearer terrain rises above the summit,
 *                                            clear_deg is the margin (summit minus the nearer terrain)
 *     from: {e_m, n_m, ground_m, eye_m}, ms
 *   }
 *
 * RidgelineSkyline.svgPath(profile, w, h, opts) -> {d, line, peakX, peakY, vex}
 *   d      closed ground shape under the horizon (fill it), in a 0..w by 0..h box
 *   line   the horizon alone (stroke it)
 *   peakX, peakY  the summit marker, null when the peak is outside the window
 *   vex    vertical exaggeration used (1 = true shape)
 *   opts   pad_top 8, pad_bottom 6, max_vex 3, min_vex 0.4. The whole skyline is fitted in the box; only when
 *          that would squash it below min_vex is the low ground cut at the bottom (the top always shows).
 *
 * Model, the same as solver.js and the wall cards: eye 1.7 m above the bilinear ground, earth curvature
 * with refraction (drop d^2 / 2 rEff, k 0.13), one ray every step_deg marched every 15 m from 45 m out to
 * the tile edge on the 30 m grid. The 30 m grid is a block mean of the 5 m terrain, which flattens sharp
 * summits by up to about 20 m (0.45 deg low at Bivouac Peak from the demo spot), so the 23 known summits
 * are put back: the missing height is added at the summit and fades to nothing 45 m out, capped at the summit.
 * Terrain beyond the tile edge is not in the data, so a ray that leaves the tile early can draw too low.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.RidgelineSkyline = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const D2R = Math.PI / 180;
  const R2D = 180 / Math.PI;
  const wrap180 = d => ((((d % 360) + 540) % 360) - 180);
  const wrap360 = d => ((d % 360) + 360) % 360;
  const clock = () => (typeof performance !== 'undefined' ? performance : Date).now();

  const DEFAULTS = Object.freeze({
    half_fov_deg: 15,
    step_deg: 0.25,
    eye_m: null,            // null: grid.eye (1.7 m)
    az_center_deg: null,    // null: the true bearing to the peak
    min_range_m: 45,        // skip the ground at the feet, like the wall cards
    step_m: 15,             // march step near the eye, half a grid cell
    step_grow: 0.004,       // farther out the step grows to r x 0.004, about the spacing between rays
    max_range_m: 40000,     // rays stop at the tile edge first
    summit_radius_m: 45,    // the height the 30 m mean took off a known summit fades out over this radius (0 = off)
    summit_skip_m: 45,      // the last meters before the summit are not "nearer terrain" (export_grid.py)
  });

  // Bilinear on cell centers, NaN outside the interpolation area.
  function elevAt(m, e, n) {
    const f = e / m.dx - 0.5, g = m.ny - n / m.dy - 0.5;
    const c0 = Math.floor(f), r0 = Math.floor(g);
    if (c0 < 0 || r0 < 0 || c0 + 1 >= m.nx || r0 + 1 >= m.ny) return NaN;
    const fx = f - c0, fy = g - r0, i = r0 * m.nx + c0, z = m.elev;
    return (z[i] * (1 - fx) + z[i + 1] * fx) * (1 - fy) + (z[i + m.nx] * (1 - fx) + z[i + m.nx + 1] * fx) * fy;
  }

  // Ground under the eye. Clamped, so the outer half cell of the tile still has a height.
  function groundAt(m, e, n) {
    const f = Math.min(Math.max(e / m.dx - 0.5, 0), m.nx - 1.000001);
    const g = Math.min(Math.max(m.ny - n / m.dy - 0.5, 0), m.ny - 1.000001);
    return elevAt(m, (f + 0.5) * m.dx, (m.ny - g - 0.5) * m.dy);
  }

  // Highest point of the tile (grid or summit), once per grid.
  const tops = typeof WeakMap === 'function' ? new WeakMap() : null;
  function zTop(m) {
    let v = tops && tops.get(m);
    if (v !== undefined) return v;
    v = -Infinity;
    const z = m.elev;   // Math.max over chunks runs as a builtin, a plain loop is slow before the JIT warms up
    for (let i = 0; i < z.length; i += 8192) v = Math.max(v, Math.max.apply(null, z.subarray(i, Math.min(z.length, i + 8192))));
    for (const p of m.peaks) if (p.elev_m > v) v = p.elev_m;
    if (tops) tops.set(m, v);
    return v;
  }

  function toEN(m, from) {
    if (Number.isFinite(from.e_m) && Number.isFinite(from.n_m)) return { e: from.e_m, n: from.n_m };
    const [w, s, ea, no] = m.bounds;
    return { e: ((from.lon - w) / (ea - w)) * m.nx * m.dx, n: ((from.lat - s) / (no - s)) * m.ny * m.dy };
  }

  // Highest tangent (rise over run) seen along one ray, and its distance. Terrain plus the restored summits.
  function rayMax(m, ctx, az, rMax, out) {
    const se = Math.sin(az * D2R), ce = Math.cos(az * D2R);
    const nx = m.nx, z = m.elev, c = ctx.curv, z0 = ctx.z0, step = ctx.step, grow = ctx.grow;
    const lastF = m.nx - 1, lastG = m.ny - 1;
    const fx0 = ctx.e0 / m.dx - 0.5, gy0 = m.ny - ctx.n0 / m.dy - 0.5;
    const dfx = se / m.dx, dgy = -ce / m.dy;
    // The stretch of the ray inside the interpolation area, so the loop needs no edge checks.
    let lo = ctx.rMin, hi = rMax;
    if (dfx > 1e-12) { lo = Math.max(lo, -fx0 / dfx); hi = Math.min(hi, (lastF - fx0) / dfx); }
    else if (dfx < -1e-12) { lo = Math.max(lo, (lastF - fx0) / dfx); hi = Math.min(hi, -fx0 / dfx); }
    else if (fx0 < 0 || fx0 > lastF) hi = -1;
    if (dgy > 1e-12) { lo = Math.max(lo, -gy0 / dgy); hi = Math.min(hi, (lastG - gy0) / dgy); }
    else if (dgy < -1e-12) { lo = Math.max(lo, (lastG - gy0) / dgy); hi = Math.min(hi, -gy0 / dgy); }
    else if (gy0 < 0 || gy0 > lastG) hi = -1;
    hi -= 1e-6;
    // Past the stop distance even the highest point of the tile would sit below the best angle so far.
    const H = ctx.zTop - z0;
    let best = -Infinity, bestR = 0;
    for (let r = lo; r <= hi; r += (r * grow > step ? r * grow : step)) {
      const f = fx0 + dfx * r, g = gy0 + dgy * r;
      const c0 = f | 0, r0 = g | 0, ax = f - c0, ay = g - r0, i = r0 * nx + c0;
      const z00 = z[i], z10 = z[i + nx], a = z00 + (z[i + 1] - z00) * ax;
      const t = (a + (z10 + (z[i + nx + 1] - z10) * ax - a) * ay - z0 - r * r * c) / r;
      if (t > best) {
        best = t; bestR = r;
        if (H > 0) {
          const q = Math.sqrt(t * t + 4 * c * H), stop = t >= 0 ? (2 * H) / (t + q) : (q - t) / (2 * c);
          if (stop < hi) hi = stop;
        }
      }
    }
    const R = ctx.coneR;
    for (let k = 0; k < ctx.cones.length; k++) {
      const q = ctx.cones[k];
      const along = q.de * se + q.dn * ce;
      if (along < ctx.rMin || along > rMax) continue;
      const side = Math.abs(q.de * ce - q.dn * se);
      if (side >= R) continue;
      // The summit lift fades out over R on top of the local grid (so a cliff stays a cliff), never above the summit.
      const h = Math.min(q.top, groundAt(m, ctx.e0 + se * along, ctx.n0 + ce * along) + q.lift * (1 - side / R));
      const t = (h - z0 - along * along * c) / along;
      if (t > best) { best = t; bestR = along; }
    }
    out.t = best; out.r = bestR;
    return out;
  }

  function profile(grid, from, peakId, opts) {
    const t0 = clock();
    const m = grid;
    if (!m || !m.elev || !m.byId) throw new Error('RidgelineSkyline.profile wants the grid from RidgelineSolver.load(json)');
    const p = m.byId[peakId];
    if (!p || !from) return null;
    const o = Object.assign({}, DEFAULTS, opts || {});
    const pos = toEN(m, from);
    if (!(pos.e >= 0 && pos.n >= 0 && pos.e <= m.nx * m.dx && pos.n <= m.ny * m.dy)) return null;
    const eye = Number.isFinite(o.eye_m) ? o.eye_m : m.eye;
    const ground = groundAt(m, pos.e, pos.n);
    const z0 = ground + eye;
    const curv = 1 / (2 * m.rEff);

    const pde = p.e_m - pos.e, pdn = p.n_m - pos.n;
    const dist = Math.hypot(pde, pdn);
    const bearing = wrap360(Math.atan2(pde, pdn) * R2D);
    const azc = Number.isFinite(o.az_center_deg) ? wrap360(o.az_center_deg) : bearing;
    const nSide = Math.max(1, Math.round(o.half_fov_deg / o.step_deg));
    const half = o.half_fov_deg, dStep = half / nSide;

    // Summits the 30 m mean flattened, to be put back. Only those that can touch a ray in the window.
    const coneR = Math.max(0, o.summit_radius_m);
    const cones = [];
    if (coneR > 0) {
      for (const q of m.peaks) {
        const de = q.e_m - pos.e, dn = q.n_m - pos.n, dq = Math.hypot(de, dn);
        if (dq <= o.min_range_m) continue;
        const room = half + Math.atan2(coneR, dq) * R2D + 0.5;
        if (Math.abs(wrap180(Math.atan2(de, dn) * R2D - azc)) > room) continue;
        const lift = q.elev_m - groundAt(m, q.e_m, q.n_m);
        if (lift > 0) cones.push({ de, dn, top: q.elev_m, lift });
      }
    }
    const ctx = {
      e0: pos.e, n0: pos.n, z0, curv, zTop: zTop(m), step: o.step_m, grow: o.step_grow, rMin: o.min_range_m, coneR, cones,
    };
    const hit = { t: -Infinity, r: 0.5 };

    // Is the summit itself in sight? Nearer terrain only, up to summit_skip_m before the top.
    // Done before the samples so the hot loop is first compiled for a fractional range.
    const summit = Math.atan2(p.elev_m - z0 - dist * dist * curv, dist) * R2D;
    rayMax(m, ctx, bearing, dist - o.summit_skip_m, hit);
    const near = hit.t > -Infinity ? Math.atan(hit.t) * R2D : -90;
    const clear = summit - near;

    const samples = new Array(2 * nSide + 1);
    let floor = Infinity;
    for (let k = -nSide; k <= nSide; k++) {
      const daz = k * dStep;
      rayMax(m, ctx, azc + daz, o.max_range_m, hit);
      const ok = hit.t > -Infinity;
      const elev = ok ? Math.atan(hit.t) * R2D : NaN;
      if (ok && elev < floor) floor = elev;
      samples[k + nSide] = { daz, elev_deg: elev, dist_m: ok ? hit.r : 0 };
    }
    // A ray that left the tile at once has no terrain: draw it at the lowest skyline seen.
    if (!Number.isFinite(floor)) floor = 0;
    for (const s of samples) if (!(s.elev_deg === s.elev_deg)) s.elev_deg = floor;

    return {
      az_center: azc,
      fov_deg: 2 * half,
      samples,
      peak: {
        id: p.id, name: p.name, daz: wrap180(bearing - azc), elev_deg: summit, dist_m: dist,
        bearing_deg: bearing, hidden: clear < 0, clear_deg: clear,
      },
      from: { e_m: pos.e, n_m: pos.n, ground_m: ground, eye_m: eye },
      ms: clock() - t0,
    };
  }

  function svgPath(prof, w, h, opts) {
    const o = Object.assign({ pad_top: 8, pad_bottom: 6, max_vex: 3, min_vex: 0.4 }, opts || {});
    const s = prof.samples, half = prof.fov_deg / 2, pk = prof.peak;
    const inside = !!pk && Math.abs(pk.daz) <= half + 1e-9;
    let hi = -Infinity, lo = Infinity;
    for (const q of s) {
      if (q.elev_deg > hi) hi = q.elev_deg;
      if (q.elev_deg < lo) lo = q.elev_deg;
    }
    if (inside) { hi = Math.max(hi, pk.elev_deg); lo = Math.min(lo, pk.elev_deg); }
    if (!Number.isFinite(lo)) lo = hi - 1;
    const xs = w / prof.fov_deg;                       // px per degree across
    const room = Math.max(1, h - o.pad_top - o.pad_bottom);
    const span = Math.max(hi - lo, 0.05);
    const vex = Math.min(o.max_vex, Math.max(o.min_vex, room / (span * xs)));
    const ys = vex * xs;                               // px per degree up
    const top = o.pad_top + Math.max(0, (room - span * ys) / 2);
    const fx = v => Math.round(v * 10) / 10;
    const X = daz => fx((daz + half) * xs);
    const Y = a => fx(Math.min(h, top + (hi - a) * ys));
    let pts = '';
    for (let k = 0; k < s.length; k++) pts += (k ? 'L' : '') + X(s[k].daz) + ' ' + Y(s[k].elev_deg);
    return {
      d: 'M0 ' + h + 'L' + pts + 'L' + w + ' ' + h + 'Z',
      line: 'M' + pts,
      peakX: inside ? X(pk.daz) : null,
      peakY: inside ? Y(pk.elev_deg) : null,
      vex,
    };
  }

  return Object.freeze({ DEFAULTS, profile, svgPath, elevAt });
});
