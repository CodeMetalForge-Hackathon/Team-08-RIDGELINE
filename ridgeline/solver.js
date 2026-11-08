/*
 * RIDGELINE solver. Position fix from the angles between known peaks, no GPS, no compass.
 *
 * Grid Bayes on the 30 m grid written by ridgeline/scripts/export_grid.py.
 * For every cell it multiplies:
 *   barometer band   N(ground + eye - baro, sigma_b)
 *   bearings         the phone heading has an unknown constant offset (room walls, magnetic
 *                    noise, relative gyro), so the offset is fitted per cell and only the
 *                    differences between Locks carry information (classic three point resection)
 *   visibility       a cell that cannot see a locked summit is down-weighted, not removed
 *                    (hidden_factor 0.3; a hard 0.02 cut lost coverage in the viewer team's test)
 *   elevation angle  optional, off by default because wall photos do not reproduce real tilt
 *
 * Works in the browser (window.RidgelineSolver) and in Node (require).
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.RidgelineSolver = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const D2R = Math.PI / 180;
  const R2D = 180 / Math.PI;
  const wrap180 = d => ((((d % 360) + 540) % 360) - 180);
  const wrap360 = d => ((d % 360) + 360) % 360;

  const DEFAULTS = Object.freeze({
    bearing_sigma_deg: 3,
    baro_sigma_m: 25,
    elev_sigma_deg: 1.5,
    hidden_factor: 0.3,
    use_elevation: false,
    enough_r95_m: 300,
    enough_elsewhere: 0.1,
    mismatch_chi2: 14,
    absolute_bearings: false,   // true: compass bearings, no offset is fitted (used only to compare against)
    diagnose: true,             // false skips the relabel search on a mismatch (fast batch statistics)
    keep_p: false,              // true adds p, the normalized probability per cell (Auto-PACE starts from it)
  });

  function bytesFromB64(s) {
    if (typeof Buffer !== 'undefined') return new Uint8Array(Buffer.from(s, 'base64'));
    const bin = atob(s);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  function unpackBits(bytes, n) {
    const out = new Uint8Array(n);
    for (let i = 0; i < n; i++) out[i] = (bytes[i >> 3] >> (7 - (i & 7))) & 1;
    return out;
  }

  function load(json) {
    if (!json || json.schema !== 'ridgeline-grid/v1') throw new Error('Unknown grid schema');
    const nx = json.nx, ny = json.ny, n = nx * ny;
    const raw = bytesFromB64(json.elev_dm_u16);
    const dv = new DataView(raw.buffer, raw.byteOffset, raw.byteLength);
    const elev = new Float32Array(n);
    for (let i = 0; i < n; i++) elev[i] = dv.getUint16(i * 2, true) / 10;
    const land = unpackBits(bytesFromB64(json.land_bits), n);
    const E = new Float32Array(n), N = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      E[i] = ((i % nx) + 0.5) * json.dx_m;
      N[i] = (ny - Math.floor(i / nx) - 0.5) * json.dy_m;
    }
    const peaks = json.peaks.map(p => {
      const { vis_bits, ...rest } = p;
      return Object.assign(rest, { vis: unpackBits(bytesFromB64(vis_bits), n) });
    });
    const byId = Object.fromEntries(peaks.map(p => [p.id, p]));
    return {
      json, nx, ny, n, dx: json.dx_m, dy: json.dy_m, elev, land, E, N, peaks, byId,
      eye: json.eye_m, rEff: 6371000 / (1 - json.k_refraction), bounds: json.bounds,
      demo: json.demo, _brg: new Map(), _dist: new Map(),
    };
  }

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

  function bearings(m, id) {
    let b = m._brg.get(id);
    if (b) return b;
    const p = m.byId[id];
    if (!p) throw new Error('Unknown peak ' + id);
    b = new Float32Array(m.n);
    for (let i = 0; i < m.n; i++) b[i] = wrap360(Math.atan2(p.e_m - m.E[i], p.n_m - m.N[i]) * R2D);
    m._brg.set(id, b);
    return b;
  }

  function distances(m, id) {
    let d = m._dist.get(id);
    if (d) return d;
    const p = m.byId[id];
    d = new Float32Array(m.n);
    for (let i = 0; i < m.n; i++) d[i] = Math.hypot(p.e_m - m.E[i], p.n_m - m.N[i]);
    m._dist.set(id, d);
    return d;
  }

  function bearingFrom(m, e, n, id) {
    const p = m.byId[id];
    return wrap360(Math.atan2(p.e_m - e, p.n_m - n) * R2D);
  }

  function elevAngle(m, groundEye, id, d) {
    const p = m.byId[id];
    return Math.atan2(p.elev_m - groundEye - (d * d) / (2 * m.rEff), d) * R2D;
  }

  // Log likelihood of one cell, plus the fitted offset and residuals when asked.
  function cellLog(m, i, obs, o, B, V, Dst, out) {
    if (!m.land[i]) return -Infinity;
    let lp = 0;
    const L = obs.locks.length;
    if (Number.isFinite(obs.baro_m)) {
      const d = m.elev[i] + m.eye - obs.baro_m;
      lp -= (d * d) / (2 * o.baro_sigma_m * o.baro_sigma_m);
    }
    if (o.absolute_bearings && L >= 1) {
      let q = 0;
      for (let k = 0; k < L; k++) {
        const e = wrap180(obs.locks[k].heading_deg - B[k][i]);
        q += e * e;
        if (out) out.res[k] = e;
      }
      if (out) out.offset = 0;
      lp -= q / (2 * o.bearing_sigma_deg * o.bearing_sigma_deg);
    } else if (L >= 2) {
      let ss = 0, cc = 0;
      for (let k = 0; k < L; k++) {
        const r = wrap180(obs.locks[k].heading_deg - B[k][i]) * D2R;
        ss += Math.sin(r); cc += Math.cos(r);
      }
      const off = Math.atan2(ss, cc) * R2D;
      let q = 0;
      for (let k = 0; k < L; k++) {
        const e = wrap180(obs.locks[k].heading_deg - B[k][i] - off);
        q += e * e;
        if (out) out.res[k] = e;
      }
      if (out) out.offset = off;
      lp -= q / (2 * o.bearing_sigma_deg * o.bearing_sigma_deg);
    } else if (out) {
      out.offset = L === 1 ? wrap180(obs.locks[0].heading_deg - B[0][i]) : 0;
      if (L === 1) out.res[0] = 0;
    }
    const lh = Math.log(o.hidden_factor);
    for (let k = 0; k < L; k++) if (!V[k][i]) lp += lh;
    if (o.use_elevation) {
      const ge = m.elev[i] + m.eye;
      for (let k = 0; k < L; k++) {
        const t = obs.locks[k].elevation_deg;
        if (!Number.isFinite(t)) continue;
        const e = t - elevAngle(m, ge, obs.locks[k].peak_id, Dst[k][i]);
        lp -= (e * e) / (2 * o.elev_sigma_deg * o.elev_sigma_deg);
      }
    }
    return lp;
  }

  function prepare(m, obs, o) {
    const B = obs.locks.map(l => bearings(m, l.peak_id));
    const V = obs.locks.map(l => m.byId[l.peak_id].vis);
    const Dst = o.use_elevation ? obs.locks.map(l => distances(m, l.peak_id)) : [];
    return { B, V, Dst };
  }

  // Best (lowest) chi square over a strided grid. Used to compare alternative labelings.
  function bestChi2(m, obs, o, stride) {
    const { B, V, Dst } = prepare(m, obs, o);
    let best = -Infinity;
    for (let r = 0; r < m.ny; r += stride) {
      for (let c = 0; c < m.nx; c += stride) {
        const lp = cellLog(m, r * m.nx + c, obs, o, B, V, Dst, null);
        if (lp > best) best = lp;
      }
    }
    return -2 * best;
  }

  // Probability threshold whose superlevel set holds `mass` of the total (highest density region).
  function threshold(p, mass, total) {
    let lo = -60, hi = 0;
    for (let it = 0; it < 36; it++) {
      const mid = (lo + hi) / 2;
      const thr = Math.exp(mid);
      let s = 0;
      for (let i = 0; i < p.length; i++) if (p[i] >= thr) s += p[i];
      if (s >= mass * total) lo = mid; else hi = mid;
    }
    return Math.exp(lo);
  }

  function components(m, mask, p, total) {
    const seen = new Uint8Array(m.n);
    const comps = [];
    const stack = [];
    for (let s = 0; s < m.n; s++) {
      if (!mask[s] || seen[s]) continue;
      let mass = 0, best = -1, bp = -1;
      stack.push(s); seen[s] = 1;
      while (stack.length) {
        const i = stack.pop();
        mass += p[i];
        if (p[i] > bp) { bp = p[i]; best = i; }
        const r = Math.floor(i / m.nx), c = i % m.nx;
        const nb = [r > 0 ? i - m.nx : -1, r < m.ny - 1 ? i + m.nx : -1, c > 0 ? i - 1 : -1, c < m.nx - 1 ? i + 1 : -1];
        for (const j of nb) if (j >= 0 && mask[j] && !seen[j]) { seen[j] = 1; stack.push(j); }
      }
      comps.push({ mass: mass / total, peak_index: best });
    }
    comps.sort((a, b) => b.mass - a.mass);
    return comps;
  }

  function nextPeak(m, obs, at, candidates) {
    const used = new Set(obs.locks.map(l => l.peak_id));
    const pool = (candidates && candidates.length ? candidates : m.peaks.map(p => p.id)).filter(id => !used.has(id) && m.byId[id]);
    if (!pool.length) return null;
    if (!obs.locks.length) {
      const first = m.demo && m.demo.first_peak_id;
      return pool.includes(first) ? first : pool[0];
    }
    let best = null, bestScore = -Infinity;
    for (const id of pool) {
      const visible = at >= 0 ? m.byId[id].vis[at] : 1;
      const b = at >= 0 ? bearingFrom(m, m.E[at], m.N[at], id) : 0;
      let minsep = 180;
      for (const l of obs.locks) minsep = Math.min(minsep, Math.abs(wrap180(bearingFrom(m, m.E[at], m.N[at], l.peak_id) - b)));
      const score = (visible ? 0 : -100) - Math.abs(minsep - 70);
      if (score > bestScore) { bestScore = score; best = id; }
    }
    return best;
  }

  function solve(m, obsIn, opts) {
    const t0 = (typeof performance !== 'undefined' ? performance : Date).now();
    const o = Object.assign({}, DEFAULTS, opts || {});
    const obs = { baro_m: obsIn.baro_m, locks: (obsIn.locks || []).filter(l => m.byId[l.peak_id] && Number.isFinite(l.heading_deg)) };
    const L = obs.locks.length;
    const { B, V, Dst } = prepare(m, obs, o);
    const logp = new Float64Array(m.n);
    let best = -Infinity, bi = -1;
    for (let i = 0; i < m.n; i++) {
      const lp = cellLog(m, i, obs, o, B, V, Dst, null);
      logp[i] = lp;
      if (lp > best) { best = lp; bi = i; }
    }
    const p = new Float64Array(m.n);
    let total = 0, se = 0, sn = 0;
    for (let i = 0; i < m.n; i++) {
      const v = logp[i] === -Infinity ? 0 : Math.exp(logp[i] - best);
      p[i] = v; total += v; se += v * m.E[i]; sn += v * m.N[i];
    }
    const t95 = threshold(p, 0.95, total);
    const t50 = threshold(p, 0.5, total);
    const hpd95 = new Uint8Array(m.n), hpd50 = new Uint8Array(m.n);
    let c95 = 0;
    for (let i = 0; i < m.n; i++) {
      if (p[i] > 0 && p[i] >= t95) { hpd95[i] = 1; c95++; }
      if (p[i] > 0 && p[i] >= t50) hpd50[i] = 1;
    }
    // r95: radius around the best cell holding 95% of the probability.
    const dist = new Float64Array(m.n);
    let dmax = 0;
    for (let i = 0; i < m.n; i++) {
      dist[i] = Math.hypot(m.E[i] - m.E[bi], m.N[i] - m.N[bi]);
      if (p[i] > 0 && dist[i] > dmax) dmax = dist[i];
    }
    let lo = 0, hi = dmax + 1;
    for (let it = 0; it < 30; it++) {
      const mid = (lo + hi) / 2;
      let s = 0;
      for (let i = 0; i < m.n; i++) if (dist[i] <= mid) s += p[i];
      if (s >= 0.95 * total) hi = mid; else lo = mid;
    }
    const halfDiag = Math.hypot(m.dx, m.dy) / 2;
    const r95 = hi + halfDiag;
    lo = 0; hi = r95;
    for (let it = 0; it < 30; it++) {
      const mid = (lo + hi) / 2;
      let s = 0;
      for (let i = 0; i < m.n; i++) if (dist[i] <= mid) s += p[i];
      if (s >= 0.5 * total) hi = mid; else lo = mid;
    }
    const r50 = hi + halfDiag;
    const comps = components(m, hpd95, p, total);
    const mainComp = comps.find(c => {
      return c.peak_index === bi;
    }) || comps[0];
    const elsewhere = Math.max(0, 0.95 - (mainComp ? mainComp.mass : 0));
    const fit = { res: new Float64Array(L), offset: 0 };
    cellLog(m, bi, obs, o, B, V, Dst, fit);
    const chi2 = -2 * best;
    const estimate = Object.assign({ index: bi, e_m: m.E[bi], n_m: m.N[bi], ground_m: m.elev[bi] }, lonLat(m, m.E[bi], m.N[bi]));
    const mean = Object.assign({ e_m: se / total, n_m: sn / total }, lonLat(m, se / total, sn / total));
    const conf = Math.max(0, Math.min(1, Math.log(3000 / r95) / Math.log(3000 / 60))) * (1 - elsewhere);
    let status = L === 0 ? 'start' : 'more';
    let suspect = null;
    if (L >= 3 && chi2 > o.mismatch_chi2) {
      status = 'mismatch';
      suspect = o.diagnose ? diagnose(m, obs, o) : null;
    } else if (L >= 3 && r95 <= o.enough_r95_m && elsewhere <= o.enough_elsewhere) {
      status = 'enough';
    }
    const lines = L >= 2 || (o.absolute_bearings && L >= 1) ? obs.locks.map((l, k) => ({
      peak_id: l.peak_id,
      bearing_true_deg: wrap360(l.heading_deg - fit.offset),
      residual_deg: fit.res[k],
    })) : [];
    const others = comps.filter(c => c !== mainComp).slice(0, 3).map(c => Object.assign({
      mass: c.mass,
      distance_m: Math.hypot(m.E[c.peak_index] - m.E[bi], m.N[c.peak_index] - m.N[bi]),
    }, lonLat(m, m.E[c.peak_index], m.N[c.peak_index])));
    const t1 = (typeof performance !== 'undefined' ? performance : Date).now();
    const out = {
      n_locks: L, status, estimate, mean, r95_m: r95, r50_m: r50,
      area95_km2: (c95 * m.dx * m.dy) / 1e6, elsewhere, others, confidence: conf,
      offset_deg: L >= 2 ? fit.offset : null, residuals_deg: Array.from(fit.res), chi2,
      lines, suspect, next_peak_id: nextPeak(m, obs, bi, o.candidates),
      hpd95, hpd50, ms: t1 - t0,
    };
    if (o.keep_p) {   // the same weights, summing to 1. Nothing above reads p again
      for (let i = 0; i < m.n; i++) p[i] /= total;
      out.p = p;
    }
    return out;
  }

  // Which Lock is wrong. First try reading one Lock as a different peak, then fall back
  // to the Lock with the largest residual at the best cell.
  function diagnose(m, obs, o) {
    const base = bestChi2(m, obs, o, 2);
    const pool = (o.candidates && o.candidates.length ? o.candidates : m.peaks.map(p => p.id));
    // Newest Lock first: the earlier Locks agreed with each other before it arrived.
    // A peak that is already used is allowed, because aiming twice at one photo is the usual slip.
    for (let k = obs.locks.length - 1; k >= 0; k--) {
      let best = null;
      for (const id of pool) {
        if (id === obs.locks[k].peak_id) continue;
        const alt = { baro_m: obs.baro_m, locks: obs.locks.map((l, j) => (j === k ? Object.assign({}, l, { peak_id: id }) : l)) };
        const c2 = bestChi2(m, alt, o, 2);
        if (!best || c2 < best.chi2) best = { id, chi2: c2 };
      }
      if (best && best.chi2 < o.mismatch_chi2 && best.chi2 < base / 2) {
        return {
          kind: 'relabel', lock_index: k, was_peak_id: obs.locks[k].peak_id, peak_id: best.id,
          repeat: obs.locks.some((l, j) => j !== k && l.peak_id === best.id),
        };
      }
    }
    const k = obs.locks.length - 1;
    return { kind: 'reaim', lock_index: k, peak_id: obs.locks[k].peak_id };
  }

  // Synthetic Locks from a known spot, for tests and the click to test on the big screen.
  function simulate(m, spot, ids, opt) {
    const s = Object.assign({ noise_deg: 3, offset_deg: 0, elev_noise_deg: 1, baro_noise_m: 10, rng: Math.random }, opt || {});
    const gauss = () => {
      let u = 0, v = 0;
      while (u === 0) u = s.rng();
      while (v === 0) v = s.rng();
      return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
    };
    const i = cellAt(m, spot.e_m, spot.n_m);
    const ground = i >= 0 ? m.elev[i] : 0;
    const locks = ids.map(id => {
      const p = m.byId[id];
      const d = Math.hypot(p.e_m - spot.e_m, p.n_m - spot.n_m);
      return {
        peak_id: id,
        heading_deg: wrap360(bearingFrom(m, spot.e_m, spot.n_m, id) + s.offset_deg + gauss() * s.noise_deg),
        elevation_deg: elevAngle(m, ground + m.eye, id, d) + gauss() * s.elev_noise_deg,
      };
    });
    return { baro_m: ground + m.eye + gauss() * s.baro_noise_m, locks };
  }

  return Object.freeze({ DEFAULTS, load, solve, simulate, lonLat, toEN, cellAt, bearingFrom, wrap180, wrap360 });
});
