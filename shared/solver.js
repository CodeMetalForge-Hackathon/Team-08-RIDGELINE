// RIDGELINE live solver. Plain ES module, no dependencies, runs in the browser and in Node 18+.
//
// Grid Bayes position fix from the differences in heading between known peaks. The phone heading is in an
// unknown relative frame (no compass), so for every candidate cell the unknown heading offset is removed with the
// circular mean of the residuals. Magnetic error and declination therefore do not matter.
//
//   import {loadGrid, solve} from '../shared/solver.js';
//   const grid = loadGrid(await (await fetch('terrain_viewer/data/moran/solver_grid.json')).json());
//   const r = solve(grid, [{peak_id: 'mount-moran', heading_deg: 212.4, elevation_deg: 9.6}, ...],
//                   {alt_m: 2745, band_m: 30});
//
// The grid comes from terrain_viewer/scripts/team8_solver_grid.py (30 m cells, peak list, line of sight per peak).
// Everything it describes is synthetic: same DEM for the hidden demo spot and the model, no real person.

export const DEFAULTS = Object.freeze({
  sigma_az_deg: 3,          // heading difference noise, per lock
  sigma_el_deg: 1.5,        // elevation angle noise, per lock
  use_elevation: true,      // false = ignore the tilt (e.g. photos at eye level on a wall)
  hdr_mass: 0.95,           // highest-density region
  enough_radius_m: 150,     // "enough" when the 95% equivalent radius is under this
  min_locks_for_enough: 3,  // with fewer locks a wrong aim cannot be caught, so never "enough" before this
  compact_factor: 3,        // and the 95% region must be one patch: no cell farther than factor x enough_radius_m
  medium_radius_m: 400,     // confidence label boundary between medium and low
  poor_fit_p: 0.01,         // a fit with a chi-square p-value under this is "poor"
  wrong_aim: true,          // look for the one lock whose removal fits best when the fit is poor
  return_cells: false,      // include the 95% region cells and their probabilities
});

const D2R = Math.PI / 180, R2D = 180 / Math.PI;
export const wrap360 = d => ((d % 360) + 360) % 360;
export const wrap180 = d => ((((d + 180) % 360) + 360) % 360) - 180;

// ------------------------------------------------------------------ grid loading
function b64bytes(s) {
  if (typeof atob === 'function') {
    const bin = atob(s), out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }
  // eslint-disable-next-line no-undef
  return new Uint8Array(Buffer.from(s, 'base64'));
}
function typed(field) {
  const bytes = b64bytes(field.data);
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (field.dtype === 'uint8') return bytes;
  if (field.dtype === 'uint16') { const a = new Uint16Array(bytes.byteLength / 2); for (let i = 0; i < a.length; i++) a[i] = dv.getUint16(2 * i, true); return a; }
  if (field.dtype === 'uint32') { const a = new Uint32Array(bytes.byteLength / 4); for (let i = 0; i < a.length; i++) a[i] = dv.getUint32(4 * i, true); return a; }
  throw new Error('unknown dtype ' + field.dtype);
}

/** Decode solver_grid.json into typed arrays. Keeps the JSON header fields (peaks, demo, bounds) as they are. */
export function loadGrid(json) {
  if (!json || json.schema !== 'ridgeline-solver-grid/v1') throw new Error('Not a ridgeline-solver-grid/v1 file');
  const rows = json.rows, cols = json.cols, n = rows * cols, [dx, dy] = json.cell_m;
  const ev = typed(json.elevation), elev = new Float32Array(n), eye = new Float32Array(n);
  for (let i = 0; i < n; i++) { elev[i] = json.elevation.offset + ev[i] * json.elevation.scale; eye[i] = elev[i] + json.eye_height_m; }
  const sl = typed(json.slope), slope = new Float32Array(n);
  for (let i = 0; i < n; i++) slope[i] = sl[i] * json.slope.scale;
  const peaks = json.peaks.map((p, k) => ({...p, k, bit: (1 << k) >>> 0}));
  return {
    json, rows, cols, n, dx, dy, area_m2: dx * dy, bounds: json.bounds, mPerDeg: json.m_per_deg,
    elev, eye, slope, water: typed(json.water), vis: typed(json.visibility),
    curv: (1 - json.refraction_k) / (2 * json.earth_radius_m),
    peaks, byId: new Map(peaks.map(p => [p.id, p])), demo: json.demo || null,
    _az: new Array(peaks.length), _el: new Array(peaks.length),
  };
}

// ------------------------------------------------------------------ geometry
export const cellX = (g, i) => (i % g.cols + 0.5) * g.dx;
export const cellY = (g, i) => (g.rows - Math.floor(i / g.cols) - 0.5) * g.dy;
export function cellLonLat(g, i) { return [g.bounds[0] + cellX(g, i) / g.mPerDeg[0], g.bounds[1] + cellY(g, i) / g.mPerDeg[1]]; }
export function lonLatToCell(g, lon, lat) {
  const c = Math.floor((lon - g.bounds[0]) * g.mPerDeg[0] / g.dx), r = g.rows - 1 - Math.floor((lat - g.bounds[1]) * g.mPerDeg[1] / g.dy);
  return r < 0 || c < 0 || r >= g.rows || c >= g.cols ? -1 : r * g.cols + c;
}
export function cellInfo(g, i) {
  const [lon, lat] = cellLonLat(g, i);
  return {cell: i, row: Math.floor(i / g.cols), col: i % g.cols, lon: +lon.toFixed(6), lat: +lat.toFixed(6),
    elev_m: +g.elev[i].toFixed(1), x_m: +cellX(g, i).toFixed(1), y_m: +cellY(g, i).toFixed(1)};
}
export const cellDistance = (g, a, b) => Math.hypot(cellX(g, a) - cellX(g, b), cellY(g, a) - cellY(g, b));
export function distanceToLonLat(g, i, lon, lat) {
  return Math.hypot(cellX(g, i) - (lon - g.bounds[0]) * g.mPerDeg[0], cellY(g, i) - (lat - g.bounds[1]) * g.mPerDeg[1]);
}

/** Predicted true bearing (deg, grid north), elevation angle (deg, curvature and refraction) and distance (m). */
export function predict(g, i, k) {
  const p = g.peaks[k], ex = p.x_m - cellX(g, i), ny = p.y_m - cellY(g, i), d = Math.hypot(ex, ny);
  return {az: wrap360(Math.atan2(ex, ny) * R2D), el: Math.atan2(p.elev_m - g.curv * d * d - g.eye[i], d) * R2D, d};
}
function tables(g, k) {   // bearing and elevation angle from every cell to peak k, computed once per peak
  if (!g._az[k]) {
    const az = new Float32Array(g.n), el = new Float32Array(g.n), p = g.peaks[k];
    for (let i = 0; i < g.n; i++) {
      const ex = p.x_m - (i % g.cols + 0.5) * g.dx, ny = p.y_m - (g.rows - Math.floor(i / g.cols) - 0.5) * g.dy, d = Math.hypot(ex, ny);
      az[i] = Math.atan2(ex, ny);                                         // radians
      el[i] = Math.atan2(p.elev_m - g.curv * d * d - g.eye[i], d) * R2D;  // degrees
    }
    g._az[k] = az; g._el[k] = el;
  }
  return [g._az[k], g._el[k]];
}
export const visible = (g, i, k) => ((g.vis[i] >>> k) & 1) === 1;

// ------------------------------------------------------------------ statistics
function lgamma(x) {   // Lanczos
  const c = [76.18009172947146, -86.50532032941677, 24.01409824083091, -1.231739572450155, 0.1208650973866179e-2, -0.5395239384953e-5];
  let y = x, tmp = x + 5.5; tmp -= (x + 0.5) * Math.log(tmp);
  let ser = 1.000000000190015; for (const v of c) ser += v / ++y;
  return -tmp + Math.log(2.5066282746310005 * ser / x);
}
function gammaP(a, x) {   // regularised lower incomplete gamma
  if (x <= 0) return 0;
  if (x < a + 1) { let sum = 1 / a, del = sum, ap = a; for (let n = 0; n < 500; n++) { ap += 1; del *= x / ap; sum += del; if (Math.abs(del) < Math.abs(sum) * 1e-12) break; } return sum * Math.exp(-x + a * Math.log(x) - lgamma(a)); }
  let b = x + 1 - a, c = 1e300, d = 1 / b, h = d;
  for (let i = 1; i < 500; i++) { const an = -i * (i - a); b += 2; d = an * d + b; if (Math.abs(d) < 1e-300) d = 1e-300; c = b + an / c; if (Math.abs(c) < 1e-300) c = 1e-300; d = 1 / d; const del = d * c; h *= del; if (Math.abs(del - 1) < 1e-12) break; }
  return 1 - Math.exp(-x + a * Math.log(x) - lgamma(a)) * h;
}
/** Upper tail of the chi-square distribution: P(X >= x) with k degrees of freedom. */
export const chi2Sf = (x, k) => k <= 0 ? 1 : 1 - gammaP(k / 2, x / 2);

// ------------------------------------------------------------------ the solve
function normaliseLocks(g, locks) {
  return locks.map((l, j) => {
    const p = g.byId.get(l.peak_id);
    if (!p) throw new Error(`Unknown peak_id "${l.peak_id}" in lock ${j + 1}`);
    if (typeof l.heading_deg !== 'number' || !Number.isFinite(l.heading_deg)) throw new Error(`Lock ${j + 1} needs a finite heading_deg`);
    const el = typeof l.elevation_deg === 'number' && Number.isFinite(l.elevation_deg) ? l.elevation_deg : null;
    return {j, k: p.k, id: p.id, name: p.name, h: wrap360(l.heading_deg) * D2R, hdeg: wrap360(l.heading_deg), el};
  });
}

function core(g, L, baro, o) {
  const t0 = now();
  const n = L.length, sa = o.sigma_az_deg, se = o.sigma_el_deg, useEl = o.use_elevation !== false;
  const tabs = L.map(l => tables(g, l.k));
  let need = 0; for (const l of L) need = (need | g.peaks[l.k].bit) >>> 0;
  const alt = baro && Number.isFinite(baro.alt_m) ? baro.alt_m : null, band = baro && Number.isFinite(baro.band_m) ? baro.band_m : Infinity;
  const cand = [], ll = [];
  let best = -1, bestLL = -Infinity, bestOff = 0;
  const vis = g.vis, eye = g.eye;
  for (let i = 0; i < g.n; i++) {
    if (alt !== null && Math.abs(eye[i] - alt) > band) continue;
    if (((vis[i] & need) >>> 0) !== need) continue;
    let s = 0, c = 0;
    for (let j = 0; j < n; j++) { const r = L[j].h - tabs[j][0][i]; s += Math.sin(r); c += Math.cos(r); }
    const off = Math.atan2(s, c);
    let chi = 0;
    for (let j = 0; j < n; j++) {
      const e = wrap180((L[j].h - tabs[j][0][i] - off) * R2D) / sa; chi += e * e;
      if (useEl && L[j].el !== null) { const v = (L[j].el - tabs[j][1][i]) / se; chi += v * v; }
    }
    const v = -0.5 * chi;
    cand.push(i); ll.push(v);
    if (v > bestLL) { bestLL = v; best = i; bestOff = off; }
  }
  const nEl = useEl ? L.filter(l => l.el !== null).length : 0;
  const dof = Math.max(0, (n - 1) + nEl - 2);
  if (best < 0) return {feasible: false, n_candidates: 0, dof, chi2: Infinity, fit_p: 0, ms: now() - t0};
  // posterior, 95% highest-density region
  const N = cand.length, p = new Float64Array(N);
  let Z = 0; for (let q = 0; q < N; q++) { p[q] = Math.exp(ll[q] - bestLL); Z += p[q]; }
  const order = Array.from({length: N}, (_, q) => q).sort((a, b) => p[b] - p[a]);
  let acc = 0, m = 0; const target = o.hdr_mass * Z;
  while (m < N && acc < target) { acc += p[order[m]]; m++; }
  const area = m * g.area_m2;
  let spread = 0;
  for (let q = 0; q < m; q++) spread = Math.max(spread, cellDistance(g, best, cand[order[q]]));
  const out = {feasible: true, n_candidates: N, best, bestOff, chi2: -2 * bestLL, dof, fit_p: chi2Sf(-2 * bestLL, dof),
    hdr_count: m, hdr_area_m2: area, r95_m: Math.sqrt(area / Math.PI), spread_m: spread, ms: 0};
  out.hdrSet = new Set(); for (let q = 0; q < m; q++) out.hdrSet.add(cand[order[q]]);
  if (o.return_cells) { out.cells = order.slice(0, m).map(q => cand[q]); out.cell_p = order.slice(0, m).map(q => p[q] / Z); }
  out.ms = now() - t0;
  return out;
}
const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now());

/**
 * Solve one fix.
 * @param grid   loadGrid() result
 * @param locks  [{peak_id, heading_deg, elevation_deg}] heading in any fixed relative frame, elevation may be null
 * @param baro   {alt_m, band_m} barometric altitude of the phone and the half-width of the band, or null
 * @param opts   overrides of DEFAULTS
 */
export function solve(grid, locks, baro = null, opts = {}) {
  const t0 = now(), o = {...DEFAULTS, ...opts};
  const L = normaliseLocks(grid, locks || []);
  const res = {n_locks: L.length, sigma_az_deg: o.sigma_az_deg, sigma_el_deg: o.sigma_el_deg, use_elevation: o.use_elevation !== false,
    baro: baro ? {alt_m: baro.alt_m, band_m: baro.band_m} : null};
  const base = core(grid, L, baro, o);
  res.feasible = base.feasible; res.n_candidates = base.n_candidates;
  if (base.feasible) {
    const b = base.best;
    res.best = cellInfo(grid, b);
    res.heading_offset_deg = +wrap360(base.bestOff * R2D).toFixed(2);   // phone frame minus true: true = heading - offset
    res.hdr95 = {mass: o.hdr_mass, cells: base.hdr_count, area_m2: Math.round(base.hdr_area_m2), radius_m: Math.round(base.r95_m),
      farthest_cell_m: Math.round(base.spread_m)};
    res.fit = {chi2: +base.chi2.toFixed(2), dof: base.dof, p: +base.fit_p.toFixed(4), poor: base.dof > 0 && base.fit_p < o.poor_fit_p};
    res.residuals = L.map(l => {
      const q = predict(grid, b, l.k);
      return {peak_id: l.id, predicted_bearing_deg: +q.az.toFixed(2), predicted_elevation_deg: +q.el.toFixed(2), dist_m: Math.round(q.d),
        az_resid_deg: +wrap180(l.hdeg - base.bestOff * R2D - q.az).toFixed(2), el_resid_deg: l.el === null ? null : +(l.el - q.el).toFixed(2),
        true_bearing_deg: +wrap360(l.hdeg - base.bestOff * R2D).toFixed(2)};
    });
    if (o.return_cells) { res.cells = base.cells; res.cell_p = base.cell_p; }
    res._hdr = base.hdrSet;
  } else {
    res.best = null; res.hdr95 = null; res.residuals = [];
    res.fit = {chi2: null, dof: base.dof, p: 0, poor: L.length > 0};
  }
  // wrong aim: the fit is poor (or no cell fits at all), so name the one lock whose removal fits best
  res.wrong_aim = null;
  if (o.wrong_aim && L.length >= 3 && res.fit.poor) {
    const trials = L.map((l, j) => {
      const sub = core(grid, L.filter((_, q) => q !== j), baro, o);
      return {index: j, peak_id: l.id, name: l.name, fit_p: sub.feasible ? sub.fit_p : 0, chi2: sub.feasible ? sub.chi2 : Infinity, r95_m: sub.feasible ? Math.round(sub.r95_m) : null};
    }).sort((a, b) => b.fit_p - a.fit_p || a.chi2 - b.chi2);
    const top = trials[0];
    if (top.fit_p >= o.poor_fit_p) {
      res.wrong_aim = {index: top.index, peak_id: top.peak_id, name: top.name, fit_p_without: +top.fit_p.toFixed(4),
        message: `Lock ${top.index + 1} (${top.name}) does not fit the others. Re-aim at ${top.name}.`,
        trials: trials.map(t => ({index: t.index, peak_id: t.peak_id, fit_p_without: +t.fit_p.toFixed(4), r95_m: t.r95_m}))};
    } else {
      res.wrong_aim = {index: null, peak_id: null, name: null, message: 'The locks do not fit together. Check each peak and lock again.',
        trials: trials.map(t => ({index: t.index, peak_id: t.peak_id, fit_p_without: +t.fit_p.toFixed(4), r95_m: t.r95_m}))};
    }
  } else if (L.length > 0 && !res.feasible) {
    res.wrong_aim = {index: null, peak_id: null, name: null, message: 'No spot at this altitude sees all of these peaks. Check the last peak and lock again.', trials: []};
  }
  // labels
  const r95 = res.hdr95 ? res.hdr95.radius_m : Infinity, poor = res.fit.poor || !res.feasible;
  const compact = !!res.hdr95 && res.hdr95.farthest_cell_m <= o.compact_factor * o.enough_radius_m;
  res.confidence = L.length === 0 ? 'none' : poor ? 'check aim' : r95 < o.enough_radius_m ? 'high' : r95 < o.medium_radius_m ? 'medium' : 'low';
  res.enough_or_one_more = !poor && r95 < o.enough_radius_m && compact && L.length >= o.min_locks_for_enough ? 'enough' : 'one_more';
  res.enough_rule = {radius_under_m: o.enough_radius_m, min_locks: o.min_locks_for_enough, one_patch_within_m: o.compact_factor * o.enough_radius_m,
    radius_ok: r95 < o.enough_radius_m, locks_ok: L.length >= o.min_locks_for_enough, one_patch: compact, fit_ok: !poor};
  res.suggested_next = res.feasible ? suggestNext(grid, res.best.cell, L, base.bestOff) : null;
  res.ms = +(now() - t0).toFixed(1);
  return res;
}

/** The visible peak whose bearing from the cell is closest to perpendicular to the lines already used. */
export function suggestNext(g, cell, L, off = 0, minDist = 400) {
  const used = new Set(L.map(l => l.k)), lines = L.map(l => predict(g, cell, l.k).az);
  let best = null;
  for (const p of g.peaks) {
    if (used.has(p.k) || !visible(g, cell, p.k)) continue;
    const q = predict(g, cell, p.k);
    if (q.d < minDist || q.el < 0.5) continue;
    const s = lines.length ? Math.min(...lines.map(b => Math.abs(Math.sin((q.az - b) * D2R)))) : 1;
    const score = s - q.d / 1e6;   // tie: the nearer peak
    if (!best || score > best.score) best = {score, p, q};
  }
  if (!best) return null;
  const crossing = Math.asin(Math.min(1, best.score + best.q.d / 1e6)) * R2D;
  return {peak_id: best.p.id, name: best.p.name, bearing_true_deg: +best.q.az.toFixed(1), elevation_deg: +best.q.el.toFixed(1), dist_m: Math.round(best.q.d),
    heading_in_phone_frame_deg: L.length >= 2 ? +wrap360(best.q.az + off * R2D).toFixed(1) : null, crossing_angle_deg: +crossing.toFixed(1)};
}

/** True when the cell lies in the 95% region of a solve() result. */
export const inHdr = (res, cell) => !!(res && res._hdr && res._hdr.has(cell));

// ------------------------------------------------------------------ simulation helpers (tests, judge clicks)
export function mulberry32(seed) {
  let a = seed >>> 0;
  return () => { a = (a + 0x6D2B79F5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}
export function gauss(rng) { let u = 0, v = 0; while (u === 0) u = rng(); while (v === 0) v = rng(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v); }

/** Synthetic locks from a cell: true bearing + a random frame offset + noise, elevation angle + noise. */
export function simulateLocks(g, cell, peakIds, {sigma_az_deg = 3, sigma_el_deg = 1.5, rng = Math.random, frame_offset_deg = null} = {}) {
  const off = frame_offset_deg === null ? rng() * 360 : frame_offset_deg;
  return peakIds.map(id => {
    const p = g.byId.get(id); if (!p) throw new Error('unknown peak ' + id);
    const q = predict(g, cell, p.k);
    return {peak_id: id, heading_deg: +wrap360(q.az + off + gauss(rng) * sigma_az_deg).toFixed(3), elevation_deg: +(q.el + gauss(rng) * sigma_el_deg).toFixed(3)};
  });
}

/** Three (or n) visible peaks from a cell with the widest crossing angles, for a judge-picked spot. */
export function pickPeaks(g, cell, n = 3, {minDist = 400, maxDist = 12000, minEl = 0.5} = {}) {
  const vis = g.peaks.filter(p => visible(g, cell, p.k)).map(p => ({p, q: predict(g, cell, p.k)}))
    .filter(v => v.q.d >= minDist && v.q.d <= maxDist && v.q.el >= minEl);
  if (vis.length < n) return vis.map(v => v.p.id);
  let best = null;
  const rec = (start, chosen) => {
    if (chosen.length === n) {
      let s = Infinity; for (let a = 0; a < n; a++) for (let b = a + 1; b < n; b++) s = Math.min(s, Math.abs(Math.sin((chosen[a].q.az - chosen[b].q.az) * D2R)));
      const score = s - chosen.reduce((t, v) => t + v.q.d, 0) / 1e7;
      if (!best || score > best.score) best = {score, ids: chosen.map(v => v.p.id)};
      return;
    }
    for (let i = start; i < vis.length; i++) rec(i + 1, [...chosen, vis[i]]);
  };
  rec(0, []);
  return best.ids;
}

/** Compact, JSON-safe summary of a result (no Sets or big arrays), for POST /lock and /state. */
export function summary(res) {
  const {_hdr, cells, cell_p, ...rest} = res;
  return rest;
}
