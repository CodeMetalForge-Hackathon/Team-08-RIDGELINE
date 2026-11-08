/*
 * RIDGELINE Auto-PACE. After the terrain fix the position stays a probability map on the same 30 m grid, and every
 * source that still works rewrites it (a grid Bayes filter). The phone shows which source it leans on now as PACE
 * tiers, and asks for a new fix when the 95% circle gets too wide.
 *
 *   P  GPS          a reading inside the current 95% circle agrees and is multiplied in (sigma 15 m). A reading
 *                   outside it disagrees and is not used. The wire values stay AGREE and REJECTED (board, CoT).
 *   A  Terrain fix  the solver's own map (solve with keep_p), its age and the distance walked since
 *   C  Steps        steps from the accelerometer, the phone heading turned to true north with the offset the peaks
 *                   fitted, a barometer band (simulated in the browser), lake cells ruled out
 *   E  Re-aim       the 95% radius is over 600 m, or over 2.5 times the radius at the fix
 *
 * Motion. A walk of d metres on a known true bearing shifts the map by d and blurs it. The error grows with the
 * distance since the last anchor (fix, re-fix or GPS) and not with its square root, because a stride bias and a
 * heading bias repeat at every step. sigma = D x max(8%, sin 6 deg), the larger of along and across, both ways.
 * A walk with no usable heading spreads the map in every direction so that its 95% radius grows by the distance.
 * The shift moves whole cells and carries the rest (under half a cell) as one offset of the whole map. A bilinear
 * shift by a fraction of a cell would blur the map a little at every update and widen the circle past the formula.
 * Gaussian blurs add variance exactly here, as box passes for the bulk and small three-tap passes for the rest.
 *
 * Works in the browser (window.RidgelinePace) and in Node (require), like solver.js.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.RidgelinePace = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const D2R = Math.PI / 180;
  const R2D = 180 / Math.PI;
  const K95 = Math.sqrt(-2 * Math.log(0.05));   // 2.4477, the 95% radius of a round 2D Gaussian, in sigmas
  const clock = () => (typeof performance !== 'undefined' ? performance : Date).now();
  const wrap360 = d => ((d % 360) + 360) % 360;

  const DEFAULTS = Object.freeze({
    along_frac: 0.08,       // stride error, 8% of the distance (1 sigma)
    cross_deg: 6,           // heading error (1 sigma), so across the walk the error is distance x sin 6 deg
    baro_sigma_m: 15,       // height band after the reference offset is taken out
    baro_every_m: 100,      // at most one barometer band per 100 m walked, so repeats do not overstate it
    gps_sigma_m: 15,
    gps_every_m: 100,       // the same reading is used again only after 100 m
    refix_min_overlap: 0.01,
    good_m: 300,
    caution_m: 600,
    refix_ratio: 2.5,
    chunk_m: 15,            // steps reach the map in chunks of about this many metres (or on flush)
    idle_mps: 1,            // step stream lost: assume up to this speed, direction unknown
    fix_still_m: 30,        // within this distance of the anchor the anchor is still what the position rests on
    trail_step_m: 10,
    trail_max: 400,
  });
  const STRIDE = Object.freeze({ healthy: 0.7, injured: 0.45 });

  // ---------- grid helpers ----------
  function lonLat(m, e, n) {
    const [w, s, ea, no] = m.bounds;
    return { lon: w + (e / (m.nx * m.dx)) * (ea - w), lat: s + (n / (m.ny * m.dy)) * (no - s) };
  }
  function toEN(m, lon, lat) {
    const [w, s, ea, no] = m.bounds;
    return { e: ((lon - w) / (ea - w)) * m.nx * m.dx, n: ((lat - s) / (no - s)) * m.ny * m.dy };
  }
  function cellAt(m, e, n) {
    const c = Math.floor(e / m.dx), r = Math.floor(m.ny - n / m.dy);
    if (c < 0 || r < 0 || c >= m.nx || r >= m.ny) return -1;
    return r * m.nx + c;
  }
  // ground height (m) at a point, bilinear between cell centres
  function groundAt(m, e, n) {
    const fc = e / m.dx - 0.5, fr = m.ny - n / m.dy - 0.5;
    const c0 = Math.max(0, Math.min(m.nx - 1, Math.floor(fc))), r0 = Math.max(0, Math.min(m.ny - 1, Math.floor(fr)));
    const c1 = Math.min(m.nx - 1, c0 + 1), r1 = Math.min(m.ny - 1, r0 + 1);
    const tx = Math.max(0, Math.min(1, fc - c0)), ty = Math.max(0, Math.min(1, fr - r0));
    const z = (r, c) => m.elev[r * m.nx + c];
    return (1 - ty) * ((1 - tx) * z(r0, c0) + tx * z(r0, c1)) + ty * ((1 - tx) * z(r1, c0) + tx * z(r1, c1));
  }

  // ---------- the map ----------
  // fix: a solver result made with keep_p (p sums to 1), or any {p, r95_m}. opts: {t, baro_m, sim}
  function create(m, fix, opts) {
    if (!fix || !fix.p || fix.p.length !== m.n) throw new Error('Auto-PACE needs the solver map (solve with keep_p)');
    const op = opts || {};
    const o = Object.assign({}, DEFAULTS, op.o || {});
    const st = {
      m, o, p: Float64Array.from(fix.p), tmp: new Float64Array(m.n),
      off_e: 0, off_n: 0,                         // carried shift of the whole map, under half a cell
      t_fix: Number.isFinite(op.t) ? op.t : Date.now(),
      walked_m: 0, steps: 0, assumed_m: 0,       // assumed_m: distance assumed while no steps were counted
      walked_at_fix: 0, anchor: 'fix', walked_at_anchor: 0,
      seg_k: 0, seg_u: 0,                         // metres since the anchor with a known and an unknown heading
      known_m: 0, unknown_m: 0, heading_src: null,
      pend: { de: 0, dn: 0, dk: 0, du: 0 },
      baro: { offset: null, last_m: 0, used: 0, last_alt: null },
      gps: { verdict: 'OFF', dist_m: null, lat: null, lon: null, key: null, last_m: -Infinity, used: 0, bad: [] },
      trail: [], refixes: 0, last_refix: null, water_conflict: false, sim: !!op.sim,
      r95_fix: null, ms: 0, updates: 0, stats: null,
    };
    normalize(st);
    st.stats = computeStats(st);
    // the same 95% radius the phone already shows for this fix
    if (Number.isFinite(fix.r95_m)) st.stats.r95_m = fix.r95_m;
    st.r95_fix = st.stats.r95_m;
    anchorBaro(st, op.baro_m);
    st.trail.push([st.stats.e_m, st.stats.n_m]);
    return st;
  }

  function anchorBaro(st, baro_m) {
    if (!Number.isFinite(baro_m)) { st.baro.offset = null; return; }
    // expected ground under the map now, so the reference error of the barometer drops out
    const m = st.m, p = st.p;
    let g = 0;
    for (let i = 0; i < m.n; i++) if (p[i]) g += p[i] * m.elev[i];
    st.baro.offset = baro_m - (g + m.eye);
    st.baro.last_m = st.walked_m;
    st.baro.last_alt = baro_m;
  }

  function normalize(st) {
    const p = st.p;
    let s = 0;
    for (let i = 0; i < p.length; i++) s += p[i];
    if (!(s > 0)) return false;
    const k = 1 / s;
    for (let i = 0; i < p.length; i++) p[i] *= k;
    return true;
  }

  // ---------- motion ----------
  // n steps of stride_m on a true bearing (deg), or heading null when no heading can be trusted
  function addSteps(st, n, stride_m, heading_deg, src) {
    if (!(n > 0) || !(stride_m > 0)) return;
    st.steps += n;
    addWalk(st, n * stride_m, heading_deg, src);
  }
  function addWalk(st, d, heading_deg, src) {
    if (!(d > 0)) return;
    const P = st.pend;
    if (heading_deg == null || !Number.isFinite(heading_deg)) { P.du += d; st.unknown_m += d; st.heading_src = 'unknown'; }
    else {
      P.de += d * Math.sin(heading_deg * D2R); P.dn += d * Math.cos(heading_deg * D2R); P.dk += d;
      st.known_m += d; st.heading_src = src || 'peaks';
    }
    st.walked_m += d;
    if (P.dk + P.du >= st.o.chunk_m) flush(st);
  }
  // time with no step stream (screen off, sensor stopped): up to idle_mps in any direction
  function idle(st, seconds) {
    if (!(seconds > 0)) return;
    const d = seconds * st.o.idle_mps;
    st.assumed_m += d;
    addWalk(st, d, null);
  }

  function flush(st) {
    const P = st.pend;
    if (!(P.dk > 0 || P.du > 0)) return false;
    const t0 = clock(), o = st.o;
    const k = Math.max(o.along_frac, Math.sin(o.cross_deg * D2R));
    let v = 0;   // variance to add per axis, m^2
    if (P.dk > 0) { v += k * k * ((st.seg_k + P.dk) ** 2 - st.seg_k ** 2); st.seg_k += P.dk; }
    if (P.du > 0) { v += ((st.seg_u + P.du) ** 2 - st.seg_u ** 2) / (K95 * K95); st.seg_u += P.du; }
    shift(st, P.de, P.dn);
    blur(st, v);
    maskWater(st);
    P.de = P.dn = P.dk = P.du = 0;
    done(st, t0);
    return true;
  }

  function shift(st, de, dn) {
    const m = st.m;
    st.off_e += de; st.off_n += dn;
    const ce = Math.round(st.off_e / m.dx), cn = Math.round(st.off_n / m.dy);
    if (!ce && !cn) return;
    st.off_e -= ce * m.dx; st.off_n -= cn * m.dy;
    const nx = m.nx, ny = m.ny, src = st.p, dst = st.tmp;
    dst.fill(0);
    const c0 = Math.max(0, -ce), c1 = Math.min(nx, nx - ce);
    for (let r = 0; r < ny; r++) {
      const r2 = r - cn;   // row 0 is north, so going north lowers the row
      if (r2 < 0 || r2 >= ny) continue;
      const a = r * nx, b = r2 * nx + ce;
      for (let c = c0; c < c1; c++) dst[b + c] = src[a + c];
    }
    st.p = dst; st.tmp = src;
  }

  function blur(st, v) {
    if (!(v > 0)) return;
    blurAxis(st, v / (st.m.dx * st.m.dx), 0);
    blurAxis(st, v / (st.m.dy * st.m.dy), 1);
  }
  // adds variance vc (cells^2) along one axis. Mass that leaves the sheet is dropped (normalized later)
  function blurAxis(st, vc, axis) {
    let rem = vc;
    if (rem >= 6) {   // three equal boxes for the bulk keep the Gaussian shape, width w adds (w^2 - 1)/12 each
      let w = Math.floor(Math.sqrt(4 * rem + 1));
      if (w % 2 === 0) w--;
      for (let k = 0; k < 3; k++) boxPass(st, w, axis);
      rem -= (3 * (w * w - 1)) / 12;
    }
    while (rem >= 2) {
      let w = Math.floor(Math.sqrt(12 * rem + 1));
      if (w % 2 === 0) w--;
      boxPass(st, w, axis);
      rem -= (w * w - 1) / 12;
    }
    while (rem > 1e-9) {
      const vp = Math.min(rem, 0.5);
      tapPass(st, vp / 2, axis);
      rem -= vp;
    }
  }
  function boxPass(st, w, axis) {
    const m = st.m, nx = m.nx, ny = m.ny, src = st.p, dst = st.tmp, h = (w - 1) >> 1, inv = 1 / w;
    const len = axis === 0 ? nx : ny, lines = axis === 0 ? ny : nx, step = axis === 0 ? 1 : nx;
    for (let L = 0; L < lines; L++) {
      const base = axis === 0 ? L * nx : L;
      let s = 0;
      for (let j = 0; j < Math.min(h, len); j++) s += src[base + j * step];
      for (let j = 0; j < len; j++) {
        const add = j + h, drop = j - h - 1;
        if (add < len) s += src[base + add * step];
        if (drop >= 0) s -= src[base + drop * step];
        dst[base + j * step] = s > 0 ? s * inv : 0;
      }
    }
    st.p = dst; st.tmp = src;
  }
  function tapPass(st, a, axis) {
    const m = st.m, nx = m.nx, ny = m.ny, src = st.p, dst = st.tmp, c = 1 - 2 * a;
    if (axis === 0) {
      for (let r = 0; r < ny; r++) {
        const b = r * nx;
        dst[b] = c * src[b] + a * src[b + 1];
        for (let x = 1; x < nx - 1; x++) dst[b + x] = c * src[b + x] + a * (src[b + x - 1] + src[b + x + 1]);
        dst[b + nx - 1] = c * src[b + nx - 1] + a * src[b + nx - 2];
      }
    } else {
      for (let x = 0; x < nx; x++) dst[x] = c * src[x] + a * src[x + nx];
      for (let r = 1; r < ny - 1; r++) {
        const b = r * nx;
        for (let x = 0; x < nx; x++) dst[b + x] = c * src[b + x] + a * (src[b + x - nx] + src[b + x + nx]);
      }
      const b = (ny - 1) * nx;
      for (let x = 0; x < nx; x++) dst[b + x] = c * src[b + x] + a * src[b + x - nx];
    }
    st.p = dst; st.tmp = src;
  }
  // nobody stands in a lake: water cells go to 0 and the rest is scaled back to 1
  function maskWater(st) {
    const m = st.m, p = st.p;
    let s = 0;
    for (let i = 0; i < m.n; i++) if (m.land[i]) s += p[i];
    if (!(s > 1e-300)) { st.water_conflict = true; normalize(st); return; }
    const k = 1 / s;
    for (let i = 0; i < m.n; i++) p[i] = m.land[i] ? p[i] * k : 0;
  }

  // ---------- measurements ----------
  // barometer altitude (m, eye height included, like the solver's baro_m). Used at most once per baro_every_m
  function baro(st, alt_m, force) {
    const B = st.baro;
    if (B.offset == null || !Number.isFinite(alt_m)) return { used: false, why: 'off' };
    B.last_alt = alt_m;
    if (!force && st.walked_m - B.last_m < st.o.baro_every_m) return { used: false, why: 'wait' };
    flush(st);
    const t0 = clock(), m = st.m, p = st.p, nx = m.nx, ny = m.ny;
    const want = alt_m - B.offset - m.eye, s2 = 2 * st.o.baro_sigma_m * st.o.baro_sigma_m;
    // each cell stands for a point moved by the carried offset: bilinear ground between the cell and its neighbours
    const fx = st.off_e / m.dx, fy = st.off_n / m.dy, ax = Math.abs(fx), ay = Math.abs(fy);
    const sx = fx >= 0 ? 1 : -1, sy = fy >= 0 ? -1 : 1;   // north is the row above
    const z = m.elev, before = Float64Array.from(p);
    let s = 0;
    for (let r = 0; r < ny; r++) {
      const r2 = Math.max(0, Math.min(ny - 1, r + sy));
      for (let c = 0; c < nx; c++) {
        const i = r * nx + c;
        if (!p[i]) continue;
        const c2 = Math.max(0, Math.min(nx - 1, c + sx));
        const g = (1 - ay) * ((1 - ax) * z[i] + ax * z[r * nx + c2]) + ay * ((1 - ax) * z[r2 * nx + c] + ax * z[r2 * nx + c2]);
        const d = g - want;
        p[i] *= Math.exp(-(d * d) / s2);
        s += p[i];
      }
    }
    if (!(s > 1e-300)) { st.p.set(before); return { used: false, why: 'conflict' }; }   // the band misses the whole map
    normalize(st);
    B.last_m = st.walked_m; B.used++;
    done(st, t0);
    return { used: true };
  }

  // A GPS reading. Inside the current 95% circle it agrees (AGREE) and is multiplied in once per reading and
  // per gps_every_m walked. Outside it disagrees (REJECTED), is not used, and that reading is never used later.
  function gps(st, lat, lon) {
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) { st.gps.verdict = 'OFF'; st.gps.dist_m = null; return null; }
    flush(st);
    const m = st.m, q = toEN(m, lon, lat), s = st.stats;
    const dist = Math.hypot(q.e - s.e_m, q.n - s.n_m);
    const key = lat.toFixed(6) + ',' + lon.toFixed(6), G = st.gps;
    const bad = G.bad.includes(key);
    const verdict = bad || dist > s.r95_m ? 'REJECTED' : 'AGREE';
    const out = { lat: +lat.toFixed(6), lon: +lon.toFixed(6), verdict, dist_m: Math.round(dist), used: false };
    if (verdict === 'REJECTED' && !bad) { G.bad.push(key); if (G.bad.length > 50) G.bad.shift(); }
    const fresh = key !== G.key || st.walked_m - G.last_m >= st.o.gps_every_m;
    if (verdict === 'AGREE' && fresh) {
      const t0 = clock(), p = st.p, s2 = 2 * st.o.gps_sigma_m * st.o.gps_sigma_m, before = Float64Array.from(p);
      let tot = 0;
      for (let i = 0; i < m.n; i++) {
        if (!p[i]) continue;
        const de = m.E[i] + st.off_e - q.e, dn = m.N[i] + st.off_n - q.n;
        p[i] *= Math.exp(-(de * de + dn * dn) / s2);
        tot += p[i];
      }
      if (tot > 1e-300) {
        normalize(st);
        G.last_m = st.walked_m; G.used++;
        setAnchor(st, 'gps');
        done(st, t0);
        out.used = true;
      } else st.p.set(before);
    }
    G.key = key; G.verdict = verdict; G.dist_m = out.dist_m; G.lat = out.lat; G.lon = out.lon;
    return out;
  }
  function gpsOff(st) { st.gps.verdict = 'OFF'; st.gps.dist_m = null; st.gps.key = null; }

  // A new terrain fix after walking: prediction x new map. When they barely overlap (Bhattacharyya coefficient
  // under refix_min_overlap) the steps were off, and the new fix is taken alone.
  function refix(st, fix, opts) {
    if (!fix || !fix.p || fix.p.length !== st.m.n) throw new Error('refix needs the solver map (solve with keep_p)');
    flush(st);
    const t0 = clock(), p = st.p, q = fix.p, n = st.m.n;
    // the carried offset is under half a cell, so cells are matched one to one
    let bc = 0;
    for (let i = 0; i < n; i++) if (p[i] > 0 && q[i] > 0) bc += Math.sqrt(p[i] * q[i]);
    let tookNew = !(bc >= st.o.refix_min_overlap);
    if (!tookNew) {
      for (let i = 0; i < n; i++) p[i] *= q[i];
      if (!normalize(st)) tookNew = true;
    }
    if (tookNew) { st.p.set(q); st.trail = []; }   // the walked trail was wrong, start it again at the new fix
    st.off_e = 0; st.off_n = 0;
    st.refixes++;
    st.last_refix = { overlap: bc, took_new: tookNew, walked_m: st.walked_m - st.walked_at_fix };
    st.t_fix = opts && Number.isFinite(opts.t) ? opts.t : Date.now();
    st.walked_at_fix = st.walked_m;
    setAnchor(st, 'refix');
    done(st, t0);
    if (tookNew && Number.isFinite(fix.r95_m)) st.stats.r95_m = fix.r95_m;
    st.r95_fix = st.stats.r95_m;
    if (opts && Number.isFinite(opts.baro_m)) anchorBaro(st, opts.baro_m);
    return st.last_refix;
  }

  function setAnchor(st, kind) {
    st.anchor = kind; st.walked_at_anchor = st.walked_m; st.seg_k = 0; st.seg_u = 0;
  }

  function done(st, t0) {
    st.stats = computeStats(st);
    const s = st.stats, tr = st.trail, last = tr[tr.length - 1];
    if (!last || Math.hypot(s.e_m - last[0], s.n_m - last[1]) >= st.o.trail_step_m) {
      tr.push([s.e_m, s.n_m]);
      if (tr.length > st.o.trail_max) for (let i = tr.length - 2; i > 0; i -= 2) tr.splice(i, 1);   // keep both ends
    }
    st.updates++;
    st.ms = clock() - t0;
  }

  // ---------- reading the map ----------
  // best cell (with the carried offset), the mean, and the 95% and 50% radii around the best cell
  function computeStats(st) {
    const m = st.m, p = st.p, n = m.n;
    let bi = 0, bp = -1, se = 0, sn = 0, tot = 0;
    for (let i = 0; i < n; i++) {
      const v = p[i];
      if (!v) continue;
      if (v > bp) { bp = v; bi = i; }
      se += v * m.E[i]; sn += v * m.N[i]; tot += v;
    }
    const e0 = m.E[bi], n0 = m.N[bi];
    const NB = Math.ceil(Math.hypot(m.nx * m.dx, m.ny * m.dy)) + 2;
    const hist = new Float64Array(NB);
    for (let i = 0; i < n; i++) {
      const v = p[i];
      if (!v) continue;
      const de = m.E[i] - e0, dn = m.N[i] - n0;
      hist[Math.floor(Math.sqrt(de * de + dn * dn))] += v;
    }
    const half = Math.hypot(m.dx, m.dy) / 2;
    let cum = 0, r50 = null, r95 = null;
    for (let b = 0; b < NB; b++) {
      cum += hist[b];
      if (r50 == null && cum >= 0.5 * tot) r50 = b + 1;
      if (cum >= 0.95 * tot) { r95 = b + 1; break; }
    }
    const e = e0 + st.off_e, nn = n0 + st.off_n, ll = lonLat(m, e, nn);
    const mean = tot > 0 ? { e_m: se / tot + st.off_e, n_m: sn / tot + st.off_n } : { e_m: e, n_m: nn };
    return {
      index: bi, e_m: e, n_m: nn, lat: ll.lat, lon: ll.lon, mean,
      r95_m: (r95 == null ? NB : r95) + half, r50_m: (r50 == null ? NB : r50) + half,
    };
  }

  // mean and variance per axis in metres (tests and the board legend)
  function moments(st) {
    const m = st.m, p = st.p;
    let s = 0, se = 0, sn = 0, see = 0, snn = 0;
    for (let i = 0; i < m.n; i++) {
      const v = p[i];
      if (!v) continue;
      s += v; se += v * m.E[i]; sn += v * m.N[i]; see += v * m.E[i] * m.E[i]; snn += v * m.N[i] * m.N[i];
    }
    const me = se / s, mn = sn / s;
    return { e_m: me + st.off_e, n_m: mn + st.off_n, var_e: see / s - me * me, var_n: snn / s - mn * mn };
  }

  // probability thresholds of the 95% and 50% highest density regions (for drawing the map)
  function hpd(st) {
    const p = st.p;
    let mx = 0;
    for (let i = 0; i < p.length; i++) if (p[i] > mx) mx = p[i];
    if (!(mx > 0)) return { t95: Infinity, t50: Infinity };
    const NB = 480, lo = Math.log(mx) - 24, w = 24 / NB, hist = new Float64Array(NB);
    let tot = 0;
    for (let i = 0; i < p.length; i++) {
      const v = p[i];
      if (!v) continue;
      tot += v;
      const b = Math.floor((Math.log(v) - lo) / w);
      if (b >= 0) hist[Math.min(NB - 1, b)] += v;
    }
    let cum = 0, t50 = null, t95 = null;
    for (let b = NB - 1; b >= 0; b--) {
      cum += hist[b];
      if (t50 == null && cum >= 0.5 * tot) t50 = Math.exp(lo + b * w);
      if (cum >= 0.95 * tot) { t95 = Math.exp(lo + b * w); break; }
    }
    return { t95: t95 == null ? 0 : t95, t50: t50 == null ? 0 : t50 };
  }

  // ---------- what the phone and the board show ----------
  // basis: what the position rests on now. 'gps' while a GPS reading is current, the fix ('fix' or 'refix') until
  // fix_still_m has been walked since it, then 'steps' (heading known for most of it) or 'drift' (heading unknown)
  function summary(st, t) {
    const s = st.stats, o = st.o, now = Number.isFinite(t) ? t : Date.now();
    const since = st.walked_m - st.walked_at_anchor;
    let basis;
    if (st.anchor === 'gps' && since < o.gps_every_m) basis = 'gps';
    else if (since < o.fix_still_m) basis = st.anchor === 'gps' ? 'steps' : st.anchor;
    else basis = st.seg_u > st.seg_k ? 'drift' : 'steps';
    const r = s.r95_m;
    const refixNow = r > o.caution_m || r > o.refix_ratio * st.r95_fix;
    const level = refixNow ? 'low' : r <= o.good_m ? 'good' : 'caution';
    const conf = Math.max(0, Math.min(1, Math.log(3000 / r) / Math.log(3000 / 60)));
    const tr = st.trail, every = Math.max(1, Math.ceil(tr.length / 60));
    const trail = tr.filter((q, i) => i % every === 0 || i === tr.length - 1).map(q => { const ll = lonLat(st.m, q[0], q[1]); return [+ll.lon.toFixed(6), +ll.lat.toFixed(6)]; });
    return {
      lat: +s.lat.toFixed(7), lon: +s.lon.toFixed(7), e_m: Math.round(s.e_m), n_m: Math.round(s.n_m),
      r95_m: Math.round(r), r95_fix_m: Math.round(st.r95_fix), level, refix: refixNow, basis, conf: +conf.toFixed(3),
      minutes: Math.max(0, (now - st.t_fix) / 60000), walked_m: Math.round(st.walked_m),
      walked_fix_m: Math.round(st.walked_m - st.walked_at_fix), assumed_m: Math.round(st.assumed_m),
      steps: st.steps, heading: st.heading_src, known_m: Math.round(st.known_m), unknown_m: Math.round(st.unknown_m),
      gps: st.gps.verdict, gps_dist_m: st.gps.dist_m, gps_used: st.gps.used,
      baro: st.baro.offset == null ? 'off' : st.baro.used ? 'used' : 'on', baro_used: st.baro.used,
      refixes: st.refixes, last_refix: st.last_refix ? { overlap: +st.last_refix.overlap.toFixed(4), took_new: st.last_refix.took_new } : null,
      water: !!st.water_conflict, sim: !!st.sim, trail,
    };
  }

  // ---------- steps from the accelerometer ----------
  // |acceleration including gravity|, smoothed (time constant smooth_s) against its slow mean (mean_s).
  // A step is a local maximum at least `threshold` above the mean, 0.28 to 2 s after the previous one. The first
  // peak after a pause waits for a second one within max_gap_s, so a single bump is never a step.
  function stepDetector(opts) {
    const o = Object.assign({ threshold: 1.2, min_gap_s: 0.28, max_gap_s: 2, smooth_s: 0.05, mean_s: 2 }, opts || {});
    let fast = null, slow = null, prevT = null, y1 = null, y2 = null, t1 = null, s1 = null, last = -Infinity, tentative = false, count = 0;
    return {
      opts: o,
      get count() { return count; },
      push(t, ax, ay, az) {   // t in seconds, m/s^2
        const a = Math.sqrt(ax * ax + ay * ay + az * az);
        if (!Number.isFinite(a) || !Number.isFinite(t)) return 0;
        if (prevT == null || t - prevT > 1 || t < prevT) { fast = slow = a; prevT = t; y1 = y2 = null; return 0; }
        const dt = Math.max(1e-3, t - prevT);
        prevT = t;
        fast += (a - fast) * (1 - Math.exp(-dt / o.smooth_s));
        slow += (a - slow) * (1 - Math.exp(-dt / o.mean_s));
        let n = 0;
        if (y1 != null && y2 != null && y1 > y2 && y1 >= fast && y1 - s1 >= o.threshold) {
          const gap = t1 - last;
          if (gap >= o.min_gap_s) {
            if (gap <= o.max_gap_s) { n = tentative ? 2 : 1; tentative = false; }
            else tentative = true;
            last = t1;
          }
        }
        y2 = y1; y1 = fast; t1 = t; s1 = slow;
        count += n;
        return n;
      },
    };
  }

  return Object.freeze({
    DEFAULTS, STRIDE, K95, create, addSteps, addWalk, idle, flush, baro, gps, gpsOff, refix, summary, hpd, moments,
    stepDetector, lonLat, toEN, cellAt, groundAt, wrap360, R2D,
  });
});
