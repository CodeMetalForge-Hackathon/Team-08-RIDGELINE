/* Team-8 nav core. Runs in the browser, offline, with no drawing: index.html calls it when the judge drops the pilot
   somewhere or picks a destination.

   Position, grid Bayes on about 20 m cells (Moran: the 10 m working grid pooled 2 x 2):
   - Barometer: Gaussian on altitude (sigma 30 m, the day's calibration is the weak part, so it only cuts candidates).
   - Each aimed peak: in view or not (ray test with curvature and refraction, k = 0.13), the tilt angle (Gaussian),
     and the horizontal angle the gyro reads. The gyro frame has an unknown heading, so only the differences between
     peaks count: no compass and no declination. That is the sailors' three-point fix (station pointer), done by the phone.
   - One peak in view only: the compass bearing (after the stored declination) is the fallback.
   Route: Dijkstra over the working grid, Tobler off-trail time (x 0.6), slope limit on the 30 m smoothed slope,
   water and cliffs blocked (nogo.png), injured walks at half speed. One search per start, so every destination after
   the first is a backtrack. */
(function () {
'use strict';
const R_E = 6371000, KREF = 0.13, CURV = (1 - KREF) / (2 * R_E), EYE = 1.7;
const D2R = Math.PI / 180, R2D = 180 / Math.PI;
const VK = 0.6 * 6.0 / 3.6;   // Tobler peak speed off-trail, m/s, as in team8_simulate.py
const DIR8 = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
const wrap = d => ((d % 360) + 360) % 360;
const angd = (a, b) => ((a - b + 540) % 360) - 180;

function rng(seed) {   // mulberry32: the same spot always gives the same readings
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6D2B79F5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
function gauss(r) { let u = r(); while (u <= 1e-12) u = r(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * r()); }
const seedOf = (lon, lat, extra) => ((Math.round(lon * 1e5) * 73856093) ^ (Math.round(lat * 1e5) * 19349663) ^ ((extra | 0) * 83492791)) >>> 0;

async function pixels(src) {
  const im = await new Promise((ok, no) => { const i = new Image(); i.onload = () => ok(i); i.onerror = no; i.src = src; });
  const c = document.createElement('canvas'); c.width = im.width; c.height = im.height;
  const x = c.getContext('2d', {willReadFrequently: true}); x.drawImage(im, 0, 0);
  return {w: im.width, h: im.height, d: x.getImageData(0, 0, im.width, im.height).data};
}

async function init(D) {
  const t0 = performance.now(), M = D.meta, B = M.bounds;
  const [T, SLP, NG] = await Promise.all([pixels(D.img.terrain), pixels(D.img.slope), D.img.nogo ? pixels(D.img.nogo) : null]);
  const W = T.w, H = T.h, N = W * H, dec = M.elevation_decoder;
  const DX = M.grid.cell_m[0] * M.grid.width / W, DY = M.grid.cell_m[1] * M.grid.height / H;
  const XW = W * DX, YH = H * DY;   // the tile in metres, x east of the west edge, y north of the south edge
  const Z = new Float32Array(N), SLOPE = new Float32Array(N), WATER = new Uint8Array(N), CLIFF = new Uint8Array(N);
  const at = (img, r, c) => 4 * (Math.min(img.h - 1, Math.floor(r * img.h / H)) * img.w + Math.min(img.w - 1, Math.floor(c * img.w / W)));
  for (let r = 0, i = 0; r < H; r++) for (let c = 0; c < W; c++, i++) {
    Z[i] = T.d[4 * i] * dec.rScaler + T.d[4 * i + 1] * dec.gScaler + T.d[4 * i + 2] * dec.bScaler + dec.offset;
    SLOPE[i] = SLP.d[at(SLP, r, c)] / 2;
    if (NG) { const k = at(NG, r, c); WATER[i] = NG.d[k] > 127 ? 1 : 0; CLIFF[i] = NG.d[k + 1] > 127 ? 1 : 0; }
  }
  const xOf = lon => (lon - B[0]) / (B[2] - B[0]) * XW, yOf = lat => (lat - B[1]) / (B[3] - B[1]) * YH;
  const lonOf = x => B[0] + x / XW * (B[2] - B[0]), latOf = y => B[1] + y / YH * (B[3] - B[1]);
  function zAt(x, y) {   // bilinear on the working grid
    const c = Math.max(0, Math.min(W - 1.001, x / DX - 0.5)), r = Math.max(0, Math.min(H - 1.001, (YH - y) / DY - 0.5));
    const c0 = Math.floor(c), r0 = Math.floor(r), fc = c - c0, fr = r - r0, i = r0 * W + c0;
    return (Z[i] * (1 - fc) + Z[i + 1] * fc) * (1 - fr) + (Z[i + W] * (1 - fc) + Z[i + W + 1] * fc) * fr;
  }
  const cellAt = (x, y) => { const c = Math.floor(x / DX), r = Math.floor((YH - y) / DY); return c < 0 || r < 0 || c >= W || r >= H ? -1 : r * W + c; };
  const isWater = (lon, lat) => { const i = cellAt(xOf(lon), yOf(lat)); return i < 0 || WATER[i] === 1; };
  const inside = (lon, lat) => lon > B[0] && lon < B[2] && lat > B[1] && lat < B[3];
  const slopeAt = (lon, lat) => { const i = cellAt(xOf(lon), yOf(lat)); return i < 0 ? 0 : SLOPE[i]; };
  function losFine(x, y, zo, xp, yp, zp, d) {   // same test as team8_simulate.py: 2 m of slack, skip the first 2% and the summit
    const zpe = zp - CURV * d * d, n = Math.max(8, Math.ceil(d / Math.min(DX, DY)));
    for (let s = 1; s < n; s++) {
      const t = s / n; if (t < 0.02 || t > 0.985) continue;
      if (zAt(x + t * (xp - x), y + t * (yp - y)) - CURV * (t * d) * (t * d) > zo + t * (zpe - zo) + 2) return false;
    }
    return true;
  }

  // ---------------------------------------------------------------- posterior grid (about 20 m)
  const K = Math.max(1, Math.round(Math.sqrt(N / 200000)));
  const PW = Math.floor(W / K), PH = Math.floor(H / K), PN = PW * PH, PDX = DX * K, PDY = DY * K, CELL_KM2 = PDX * PDY / 1e6;
  const ZC = new Float32Array(PN), LANDC = new Uint8Array(PN);
  for (let pr = 0; pr < PH; pr++) for (let pc = 0; pc < PW; pc++) {
    let s = 0, w = 0;
    for (let a = 0; a < K; a++) for (let b = 0; b < K; b++) { const i = (pr * K + a) * W + pc * K + b; s += Z[i]; w += WATER[i]; }
    ZC[pr * PW + pc] = s / (K * K); LANDC[pr * PW + pc] = w * 2 < K * K ? 1 : 0;
  }
  let landKm2 = 0; for (let i = 0; i < PN; i++) landKm2 += LANDC[i]; landKm2 *= CELL_KM2;
  const pX = pc => (pc + 0.5) * PDX, pY = pr => YH - (pr + 0.5) * PDY;
  const PBOUNDS = [B[0], latOf(YH - PH * PDY), lonOf(PW * PDX), B[3]];
  const pCell = (lon, lat) => { const c = Math.floor(xOf(lon) / PDX), r = Math.floor((YH - yOf(lat)) / PDY); return c < 0 || r < 0 || c >= PW || r >= PH ? -1 : r * PW + c; };
  function losCoarse(pr, pc, zo, xp, yp, zp, d) {
    const x = pX(pc), y = pY(pr), zpe = zp - CURV * d * d, n = Math.max(6, Math.ceil(d / Math.min(PDX, PDY)));
    for (let s = 1; s < n; s++) {
      const t = s / n; if (t < 0.02 || t > 0.985) continue;
      const c = Math.round((x + t * (xp - x)) / PDX - 0.5), r = Math.round((YH - (y + t * (yp - y))) / PDY - 0.5);
      if (c < 0 || r < 0 || c >= PW || r >= PH) continue;
      if (ZC[r * PW + c] - CURV * (t * d) * (t * d) > zo + t * (zpe - zo) + 2) return false;
    }
    return true;
  }

  function summarize(w, cand, key, j) {
    const n = cand.length; let tot = 0;
    for (let k = 0; k < n; k++) tot += w[k];
    if (!(tot > 0)) return {key, peaks: j, empty: true};
    const vals = new Float32Array(n); for (let k = 0; k < n; k++) vals[k] = w[k] / tot;
    const sorted = vals.slice().sort();
    let acc = 0, thr95 = 0, thr50 = 0, n95 = 0, n50 = 0;
    for (let q = n - 1; q >= 0; q--) {
      acc += sorted[q];
      if (!n50 && acc >= 0.5) { n50 = n - q; thr50 = sorted[q]; }
      if (acc >= 0.95) { n95 = n - q; thr95 = sorted[q]; break; }
    }
    if (!n95) { n95 = n; thr95 = sorted[0]; }
    const P = new Float32Array(PN); for (let k = 0; k < n; k++) P[cand[k]] = vals[k];
    const lab = new Int32Array(PN).fill(-1), comps = [], stack = [];   // connected parts of the 95% area, 8-neighbours
    let bx0 = Infinity, bx1 = -Infinity, by0 = Infinity, by1 = -Infinity;   // and the box around all of them, for the camera
    for (let k = 0; k < n; k++) {
      const i0 = cand[k]; if (P[i0] < thr95 || lab[i0] >= 0) continue;
      const cp = {mass: 0, sx: 0, sy: 0, n: 0}; lab[i0] = comps.length; stack.push(i0);
      while (stack.length) {
        const i = stack.pop(), r = (i / PW) | 0, c = i - r * PW, p = P[i], x = pX(c), y = pY(r);
        cp.mass += p; cp.sx += p * x; cp.sy += p * y; cp.n++;
        if (x < bx0) bx0 = x; if (x > bx1) bx1 = x; if (y < by0) by0 = y; if (y > by1) by1 = y;
        for (let dr = -1; dr <= 1; dr++) for (let dc = -1; dc <= 1; dc++) {
          const rr = r + dr, cc = c + dc; if (rr < 0 || cc < 0 || rr >= PH || cc >= PW) continue;
          const jj = rr * PW + cc; if (lab[jj] < 0 && P[jj] >= thr95) { lab[jj] = comps.length; stack.push(jj); }
        }
      }
      comps.push(cp);
    }
    comps.sort((a, b) => b.mass - a.mass);
    const main = comps[0], ex = main.sx / main.mass, ey = main.sy / main.mass, in95 = comps.reduce((s, c) => s + c.mass, 0);
    return {key, peaks: j, P, thr95, thr50, area95: n95 * CELL_KM2, area50: n50 * CELL_KM2, ncomp: comps.length,
      est: {lon: lonOf(ex), lat: latOf(ey), z: zAt(ex, ey)}, r95: Math.sqrt(main.n * CELL_KM2 * 1e6 / Math.PI), alt: Math.max(0, in95 - main.mass),
      bb: [[bx0, by0], [bx0, by1], [bx1, by0], [bx1, by1]].map(([x, y]) => [lonOf(x), latOf(y), zAt(x, y)])};
  }

  // ms = {baro, sb, sa, se, peaks: [{lon, lat, elev_m, m (gyro deg), e (tilt deg)}], compass: {deg, s} | null}
  function solve(ms) {
    const t1 = performance.now(), cand = [];
    for (let i = 0; i < PN; i++) { if (!LANDC[i]) continue; const u = (ZC[i] - ms.baro) / ms.sb; if (u <= 4.5 && u >= -4.5) cand.push(i); }
    const n = cand.length, lb = new Float64Array(n);
    for (let k = 0; k < n; k++) { const u = (ZC[cand[k]] - ms.baro) / ms.sb; lb[k] = Math.exp(-0.5 * u * u); }
    const per = ms.peaks.map(pk => {
      const xp = xOf(pk.lon), yp = yOf(pk.lat), zp = pk.elev_m, b = new Float32Array(n), f = new Float64Array(n);
      for (let k = 0; k < n; k++) {
        const i = cand[k], pr = (i / PW) | 0, pc = i - pr * PW, x = pX(pc), y = pY(pr), dx = xp - x, dy = yp - y, d = Math.hypot(dx, dy);
        b[k] = Math.atan2(dx, dy) * R2D;
        const zo = ZC[i] + EYE, ue = (Math.atan2(zp - CURV * d * d - zo, d) * R2D - pk.e) / ms.se;
        // a summit the coarse 20 m grid calls hidden is only made less likely: that test is often wrong next to a ridge,
        // and x 0.02 put the truth outside the 95% area on 9% of random drops (x 0.3: 2%, median error 76 m to 81 m, 120 drops on Moran)
        f[k] = ue * ue > 50 ? 0 : Math.exp(-0.5 * ue * ue) * (d > 150 && losCoarse(pr, pc, zo, xp, yp, zp, d) ? 1 : 0.3);
      }
      return {b, f};
    });
    const stages = [{key: 'prior', peaks: 0, area95: landKm2, empty: false, P: null}, summarize(lb, cand, 'baro', 0)];
    for (let j = 1; j <= per.length; j++) {
      const w = new Float64Array(n), sa2 = ms.sa * ms.sa;
      for (let k = 0; k < n; k++) {
        let v = lb[k];
        for (let q = 0; q < j && v > 0; q++) v *= per[q].f[k];
        if (v > 0 && j >= 2) {   // angles between peaks: residuals about their mean, the unknown gyro heading drops out
          const r0 = angd(ms.peaks[0].m, per[0].b[k]); let s1 = 0, s2 = 0;
          for (let q = 1; q < j; q++) { const rq = angd(angd(ms.peaks[q].m, per[q].b[k]), r0); s1 += rq; s2 += rq * rq; }
          v *= Math.exp(-0.5 * (s2 - s1 * s1 / j) / sa2);
        } else if (v > 0 && ms.compass) { const u = angd(ms.compass.deg, per[0].b[k]) / ms.compass.s; v *= Math.exp(-0.5 * u * u); }
        w[k] = v;
      }
      stages.push(summarize(w, cand, 'peak' + j, j));
    }
    return {stages, cells: n, ms: Math.round(performance.now() - t1)};
  }
  // the gyro heading that best fits the peaks at a point, so the readings can be drawn as bearings on the paper
  function offsetAt(lon, lat, peaks) {
    const x = xOf(lon), y = yOf(lat); let sx = 0, sy = 0;
    peaks.forEach(p => { const r = (p.m - Math.atan2(xOf(p.lon) - x, yOf(p.lat) - y) * R2D) * D2R; sx += Math.sin(r); sy += Math.cos(r); });
    return Math.atan2(sx, sy) * R2D;
  }
  const inStage = (st, lon, lat) => { const i = pCell(lon, lat); return !!(st && st.P && i >= 0 && st.P[i] >= st.thr95); };

  // ---------------------------------------------------------------- what the pilot sees from a spot, and the readings
  function skyline(x, y, zo) {
    const step = 0.5, nA = 720, ds = Math.max(DX, DY) * 1.5, nD = Math.floor(15000 / ds);
    const hz = new Array(nA), nr = new Array(nA);
    for (let a = 0; a < nA; a++) {
      const th = a * step * D2R, sx = Math.sin(th), sy = Math.cos(th); let mx = -90, mn = -90;
      for (let s = 1; s <= nD; s++) {
        const d = s * ds, px = x + sx * d, py = y + sy * d;
        if (px < 0 || py < 0 || px > XW || py > YH) break;
        const g = Math.atan2(zAt(px, py) - CURV * d * d - zo, d) * R2D;
        if (g > mx) mx = g; if (d <= 1500 && g > mn) mn = g;
      }
      hz[a] = Math.round(mx * 100) / 100; nr[a] = Math.round(mn * 100) / 100;
    }
    return {az_step_deg: step, horizon_deg: hz, near_ridge_deg: nr};
  }
  // expected squared position error of a set of peaks (Fisher information, the gyro heading as a nuisance)
  function setError(set, x, y, zo, sa, se) {
    let a11 = 0, a12 = 0, a22 = 0, g1 = 0, g2 = 0;
    const s2a = (sa * D2R) ** 2, s2e = (se * D2R) ** 2;
    set.forEach(s => {
      const u = xOf(s.lon) - x, v = yOf(s.lat) - y, d2 = u * u + v * v, d = Math.sqrt(d2), dh = s.elev_m - zo;
      const bx = -v / d2, by = u / d2;                   // d(bearing)/d(position), rad per metre
      a11 += bx * bx / s2a; a12 += bx * by / s2a; a22 += by * by / s2a; g1 += bx; g2 += by;
      const k = dh / (d * (d2 + dh * dh)), ex = k * u, ey = k * v;   // d(tilt)/d(position)
      a11 += ex * ex / s2e; a12 += ex * ey / s2e; a22 += ey * ey / s2e;
    });
    const n = set.length;
    a11 -= g1 * g1 / (n * s2a); a12 -= g1 * g2 / (n * s2a); a22 -= g2 * g2 / (n * s2a);
    const det = a11 * a22 - a12 * a12;
    return det > 0 ? (a11 + a22) / det : Infinity;
  }
  // o = {sa, se, sb, seed}; the solver never sees lon, lat or the gyro heading theta
  function sight(lon, lat, o) {
    const x = xOf(lon), y = yOf(lat), z = zAt(x, y), zo = z + EYE, r = rng(o.seed !== undefined ? o.seed : seedOf(lon, lat));
    const seen = M.peaks.map(p => {
      const xp = xOf(p.lon), yp = yOf(p.lat), dx = xp - x, dy = yp - y, d = Math.hypot(dx, dy);
      return {name: p.name, verified: !!p.verified, lon: p.lon, lat: p.lat, elev_m: p.elev_m, dist_m: Math.round(d),
        bearing_deg: wrap(Math.atan2(dx, dy) * R2D), elev_deg: Math.atan2(p.elev_m - CURV * d * d - zo, d) * R2D,
        visible: d > 300 && losFine(x, y, zo, xp, yp, p.elev_m, d)};
    });
    const vis = seen.filter(s => s.visible);
    let pick = vis.slice();
    if (vis.length > 3) {
      const pool = vis.slice().sort((a, b) => (b.verified - a.verified) || (b.elev_m - a.elev_m)).slice(0, 9);
      let best = null, bs = Infinity;
      for (let i = 0; i < pool.length; i++) for (let j = i + 1; j < pool.length; j++) for (let k = j + 1; k < pool.length; k++) {
        const set = [pool[i], pool[j], pool[k]], e = setError(set, x, y, zo, o.sa, o.se) * Math.pow(1.3, set.filter(s => !s.verified).length);
        if (e < bs) { bs = e; best = set; }
      }
      pick = best || pool.slice(0, 3);
    }
    pick.sort((a, b) => b.elev_m - a.elev_m);   // aim at the tallest first
    const theta = r() * 360;                      // where the gyro frame happens to point
    const peaks = pick.map(s => ({name: s.name, verified: s.verified, lon: s.lon, lat: s.lat, elev_m: s.elev_m, dist_m: s.dist_m,
      true_bearing_deg: s.bearing_deg, m: wrap(s.bearing_deg + theta + gauss(r) * o.sa), e: s.elev_deg + gauss(r) * o.se * 0.6}));
    const compass = pick.length === 1 ? {deg: wrap(pick[0].bearing_deg + gauss(r) * 3), s: 5} : null;
    return {lon, lat, z, seen, peaks, baro: z + gauss(r) * o.sb * 0.4, sb: o.sb, sa: o.sa, se: o.se, theta, compass,
      skyline: o.noSky ? null : skyline(x, y, zo), eye_m: zo, n_visible: vis.length};
  }
  function randomSpot(r) {
    for (let tries = 0; tries < 1000; tries++) {
      const c = Math.floor(r() * W), rr = Math.floor(r() * H), i = rr * W + c;
      if (WATER[i] || SLOPE[i] > 35 || c < 10 || rr < 10 || c > W - 10 || rr > H - 10) continue;
      return [lonOf((c + 0.5) * DX), latOf(YH - (rr + 0.5) * DY)];
    }
    return null;
  }

  // ---------------------------------------------------------------- routes
  const KR = Math.max(1, Math.round(Math.sqrt(N / 230000)));
  const RW = Math.floor(W / KR), RH = Math.floor(H / KR), RN = RW * RH, RDX = DX * KR, RDY = DY * KR;
  const ZR0 = new Float32Array(RN), ZR = new Float32Array(RN), SR = new Float32Array(RN), NOGO = new Uint8Array(RN);
  for (let r = 0; r < RH; r++) for (let c = 0; c < RW; c++) {
    let s = 0, m = 0, ng = 0;
    for (let a = 0; a < KR; a++) for (let b = 0; b < KR; b++) {
      const i = (r * KR + a) * W + c * KR + b; s += Z[i]; if (SLOPE[i] > m) m = SLOPE[i]; ng |= WATER[i] | (CLIFF[i] << 1);
    }
    ZR0[r * RW + c] = s / (KR * KR); SR[r * RW + c] = m; NOGO[r * RW + c] = ng;
  }
  for (let r = 0; r < RH; r++) for (let c = 0; c < RW; c++) {   // a 3 x 3 box, about the 30 m footprint of the Python planner
    let s = 0, n = 0;
    for (let a = -1; a <= 1; a++) for (let b = -1; b <= 1; b++) { const rr = r + a, cc = c + b; if (rr >= 0 && cc >= 0 && rr < RH && cc < RW) { s += ZR0[rr * RW + cc]; n++; } }
    ZR[r * RW + c] = s / n;
  }
  const rX = c => (c + 0.5) * RDX, rY = r => YH - (r + 0.5) * RDY;
  const rCell = (x, y) => Math.min(RH - 1, Math.max(0, Math.floor((YH - y) / RDY))) * RW + Math.min(RW - 1, Math.max(0, Math.floor(x / RDX)));
  const NB = [];
  for (let dr = -1; dr <= 1; dr++) for (let dc = -1; dc <= 1; dc++) if (dr || dc) NB.push([dr, dc, Math.hypot(dr * RDY, dc * RDX)]);
  const tobler = (L, dz) => L / (VK * Math.exp(-3.5 * Math.abs(dz / Math.max(L, 1e-6) + 0.05)));
  // a binary heap of cell indices keyed by K[i]; pos[i] is -1 before the first push and -2 once popped
  function cellHeap(K) {
    const heap = new Int32Array(RN), pos = new Int32Array(RN).fill(-1); let hn = 0;
    const swap = (a, b) => { const x = heap[a]; heap[a] = heap[b]; heap[b] = x; pos[heap[a]] = a; pos[heap[b]] = b; };
    const up = k => { while (k > 0) { const p = (k - 1) >> 1; if (K[heap[p]] <= K[heap[k]]) break; swap(p, k); k = p; } };
    const down = k => { for (;;) { const l = 2 * k + 1, r = l + 1; let m = k; if (l < hn && K[heap[l]] < K[heap[m]]) m = l; if (r < hn && K[heap[r]] < K[heap[m]]) m = r; if (m === k) break; swap(m, k); k = m; } };
    return {pos, size: () => hn,
      push(j) { if (pos[j] < 0) { heap[hn] = j; pos[j] = hn; hn++; } up(pos[j]); },
      pop() { const i = heap[0]; hn--; if (hn) { heap[0] = heap[hn]; pos[heap[0]] = 0; down(0); } pos[i] = -2; return i; }};
  }
  // a diagonal step may not slip through the corner between two blocked cells (two water cells touching at a corner
  // are one shoreline to a person on foot); mask 1 = water only, 3 = water or cliff
  const cornerShut = (r, c, rr, cc, mask) => r !== rr && c !== cc && ((NOGO[r * RW + cc] & mask) || (NOGO[rr * RW + c] & mask));
  // one Dijkstra from a start: every reachable cell gets its walking time and the step it came from
  function reach(lon, lat, cap, speed, maxS) {
    const t1 = performance.now(), start = rCell(xOf(lon), yOf(lat)), T = new Float32Array(RN).fill(Infinity), prev = new Int32Array(RN).fill(-1);
    const hp = cellHeap(T), pos = hp.pos;
    T[start] = 0; hp.push(start);
    const lim = maxS || 6 * 3600;
    while (hp.size()) {
      const i = hp.pop(), ti = T[i]; if (ti > lim) break;
      const r = (i / RW) | 0, c = i - r * RW, zi = ZR[i];
      for (let q = 0; q < 8; q++) {
        const rr = r + NB[q][0], cc = c + NB[q][1]; if (rr < 0 || cc < 0 || rr >= RH || cc >= RW) continue;
        const j = rr * RW + cc; if (pos[j] === -2 || NOGO[j] || SR[j] > cap || cornerShut(r, c, rr, cc, 3)) continue;
        const tj = ti + tobler(NB[q][2], ZR[j] - zi) / speed;
        if (tj < T[j]) { T[j] = tj; prev[j] = i; hp.push(j); }
      }
    }
    return {T, prev, start, cap, speed, lon, lat, ms: Math.round(performance.now() - t1)};
  }
  // a straight line on the working grid (about 10 m) that touches no water and no cliff and stays under the limit
  function clearLine(x0, y0, x1, y1, cap) {
    const n = Math.max(1, Math.ceil(Math.hypot(x1 - x0, y1 - y0) / (Math.min(DX, DY) / 3)));
    for (let s = 0; s <= n; s++) {
      const i = cellAt(x0 + (x1 - x0) * s / n, y0 + (y1 - y0) * s / n);
      if (i < 0 || WATER[i] || CLIFF[i] || SLOPE[i] > cap) return false;
    }
    return true;
  }
  // from a spot on land back to the nearest cell the walker can reach, over land only (any slope, cliffs included,
  // never water): the stretch the walker cannot do. null when water cuts the spot off (an island, a far shore).
  function landLink(rt, g0) {
    const D = new Float32Array(RN).fill(Infinity), prev = new Int32Array(RN).fill(-1), hp = cellHeap(D);
    D[g0] = 0; hp.push(g0);
    while (hp.size()) {
      const i = hp.pop();
      if (rt.T[i] < Infinity) { const tail = [i]; while (tail[tail.length - 1] !== g0) tail.push(prev[tail[tail.length - 1]]); return {cell: i, tail, len: D[i]}; }
      const r = (i / RW) | 0, c = i - r * RW;
      for (let q = 0; q < 8; q++) {
        const rr = r + NB[q][0], cc = c + NB[q][1]; if (rr < 0 || cc < 0 || rr >= RH || cc >= RW) continue;
        const j = rr * RW + cc; if (hp.pos[j] === -2 || (NOGO[j] & 1) || cornerShut(r, c, rr, cc, 1)) continue;
        const dj = D[i] + NB[q][2];
        if (dj < D[j]) { D[j] = dj; prev[j] = i; hp.push(j); }
      }
    }
    return null;
  }
  // The cells from the start to a spot. Nothing the planner returns crosses water:
  //   reached  the spot itself (the exact pin is the last point when the step onto it is clear)
  //   'steep'  the spot is on land the walker cannot get onto under the limit: the route stops at the closest safe cell
  //            along the land, and tail is that last stretch over land (never water) for a thin red line
  //   'water'  the pin is in the water: the route goes to the reachable cell nearest to it, the closest shore
  //   'island' the pin is on land that water cuts off: the route goes to the closest reachable shore, no line across
  function pathTo(rt, lon, lat) {
    const gx = xOf(lon), gy = yOf(lat), g0 = rCell(gx, gy), wet = isWater(lon, lat);
    let goal = g0, reached = !wet && rt.T[g0] < Infinity, why = null, tail = null, pin = null, gap = 0;
    if (!wet && !reached) {
      const link = landLink(rt, g0);
      if (link) {
        goal = link.cell; why = 'steep'; tail = link.tail; gap = link.len;
        if (link.len <= 2.3 * Math.max(RDX, RDY) && clearLine(rX(goal % RW), rY((goal / RW) | 0), gx, gy, rt.cap)) { reached = true; why = null; tail = null; gap = 0; }
      }
    }
    if (!reached && !why) {   // in the water, or cut off by it
      const gr = (g0 / RW) | 0, gc = g0 - gr * RW; let best = -1, bd = Infinity;
      for (let i = 0; i < RN; i++) {
        if (!(rt.T[i] < Infinity)) continue;
        const r = (i / RW) | 0, c = i - r * RW, d = (c - gc) * (c - gc) * RDX * RDX + (r - gr) * (r - gr) * RDY * RDY;
        if (d < bd) { bd = d; best = i; }
      }
      if (best < 0) return null;
      goal = best; why = wet ? 'water' : 'island'; gap = Math.hypot(rX(goal % RW) - gx, rY((goal / RW) | 0) - gy);
    }
    if (reached && clearLine(rX(goal % RW), rY((goal / RW) | 0), gx, gy, rt.cap)) pin = [lon, lat];
    const cells = [goal];
    while (cells[cells.length - 1] !== rt.start) { const p = rt.prev[cells[cells.length - 1]]; if (p < 0) return null; cells.push(p); }
    cells.reverse();
    if (tail) {   // cell centres from the route's end over the land to the pin, which takes the place of its own cell
      tail = tail.map(i => { const x = rX(i % RW), y = rY((i / RW) | 0); return [lonOf(x), latOf(y), zAt(x, y)]; });
      tail[tail.length - 1] = [lon, lat, zAt(gx, gy)];
    }
    return {cells, reached, gap_m: Math.round(gap), target: [lon, lat], why, pin, tail};
  }
  // a leg of the drawn line is fine when every few metres of it sit on a cell the planner itself would walk
  function legOK(x0, y0, x1, y1, rt) {
    const n = Math.max(1, Math.ceil(Math.hypot(x1 - x0, y1 - y0) / (Math.min(RDX, RDY) / 4)));
    for (let s = 0; s <= n; s++) {
      const i = rCell(x0 + (x1 - x0) * s / n, y0 + (y1 - y0) * s / n);
      if (i !== rt.start && (NOGO[i] || SR[i] > rt.cap)) return false;
    }
    return true;
  }
  function dp(xs, ys, i0, i1, eps, keep) {   // Douglas-Peucker for the phone's legs
    let m = -1, md = eps;
    const ax = xs[i0], ay = ys[i0], bx = xs[i1], by = ys[i1], L = Math.hypot(bx - ax, by - ay) || 1e-9;
    for (let i = i0 + 1; i < i1; i++) { const d = Math.abs((bx - ax) * (ay - ys[i]) - (ax - xs[i]) * (by - ay)) / L; if (d > md) { md = d; m = i; } }
    if (m > 0) { dp(xs, ys, i0, m, eps, keep); keep.push(m); dp(xs, ys, m, i1, eps, keep); }
  }
  const CLS = s => s <= 10 ? 0 : s <= 20 ? 1 : 2;
  function buildRoute(pp, rt) {
    let xs = pp.cells.map(i => rX(i % RW)), ys = pp.cells.map(i => rY((i / RW) | 0));
    xs[0] = xOf(rt.lon); ys[0] = yOf(rt.lat);
    if (xs.length > 7) {   // a 5-point running mean, ends kept, as in team8_simulate.py
      const sx = xs.slice(), sy = ys.slice();
      for (let i = 2; i < xs.length - 2; i++) { sx[i] = (xs[i - 2] + xs[i - 1] + xs[i] + xs[i + 1] + xs[i + 2]) / 5; sy[i] = (ys[i - 2] + ys[i - 1] + ys[i] + ys[i + 1] + ys[i + 2]) / 5; }
      // the mean pulls a bend toward the inside, and around a lake the inside is water: a leg that leaves the cells the
      // planner walked puts both of its ends back on their cell centres
      for (let pass = 0; pass < 4; pass++) {
        let moved = false;
        for (let k = 1; k < xs.length; k++) {
          if (legOK(sx[k - 1], sy[k - 1], sx[k], sy[k], rt)) continue;
          for (const q of [k - 1, k]) if (sx[q] !== xs[q] || sy[q] !== ys[q]) { sx[q] = xs[q]; sy[q] = ys[q]; moved = true; }
        }
        if (!moved) break;
      }
      xs = sx; ys = sy;
    }
    if (pp.pin) { xs.push(xOf(pp.pin[0])); ys.push(yOf(pp.pin[1])); }   // the last step, checked clear, onto the pin itself
    const n = xs.length, zs = xs.map((x, k) => zAt(x, ys[k])), path = [], ts = [0], dist = [0], sl = [];
    let t = 0, L = 0, climb = 0, desc = 0, smax = 0;
    for (let k = 0; k < n; k++) {
      path.push([lonOf(xs[k]), latOf(ys[k]), zs[k]]);
      const s = SR[rCell(xs[k], ys[k])]; sl.push(k === 0 ? Math.min(s, rt.cap) : s); if (k && s > smax) smax = s;
      if (k) {
        const l = Math.hypot(xs[k] - xs[k - 1], ys[k] - ys[k - 1]), dz = zs[k] - zs[k - 1];
        t += tobler(l, dz) / rt.speed; L += l; ts.push(t); dist.push(L); if (dz > 0) climb += dz; else desc -= dz;
      }
    }
    // runs of one difficulty (easy up to 10 deg, moderate to 20, steep above), runs under 40 m join the one before
    let runs = [];
    for (let k = 1; k < n; k++) {
      const c = Math.max(CLS(sl[k - 1]), CLS(sl[k])), last = runs[runs.length - 1];
      if (last && last.cls === c) last.i1 = k; else runs.push({cls: c, i0: k - 1, i1: k});
    }
    for (let q = 0; q < runs.length && runs.length > 1;) {
      const r = runs[q];
      if (dist[r.i1] - dist[r.i0] >= 40) { q++; continue; }
      if (q > 0) runs[q - 1].i1 = r.i1; else runs[1].i0 = r.i0;
      runs.splice(q, 1);
    }
    runs = runs.reduce((a, r) => { const l = a[a.length - 1]; if (l && l.cls === r.cls) l.i1 = r.i1; else a.push({...r}); return a; }, []);
    runs.forEach(r => {
      r.len = dist[r.i1] - dist[r.i0]; r.at = dist[r.i0]; r.dz = zs[r.i1] - zs[r.i0];
      r.max = Math.round(Math.max(...sl.slice(r.i0, r.i1 + 1)));
      r.path = path.slice(r.i0, r.i1 + 1);
    });
    const keep = [0]; dp(xs, ys, 0, n - 1, 30, keep); keep.push(n - 1); keep.sort((a, b) => a - b);
    const legs = [];
    for (let q = 1; q < keep.length; q++) {
      const i0 = keep[q - 1], i1 = keep[q]; if (i1 <= i0) continue;
      const b = wrap(Math.atan2(xs[i1] - xs[i0], ys[i1] - ys[i0]) * R2D), d = dist[i1] - dist[i0], dz = zs[i1] - zs[i0];
      const steep = Math.max(...sl.slice(i0, i1 + 1)) > 20;
      const say = `Head ${DIR8[Math.round(b / 45) % 8]} (${Math.round(b)}°) for ${Math.max(10, Math.round(d / 10) * 10)} m`
        + (dz > 25 ? `, climb ${Math.round(dz)} m` : dz < -25 ? `, descend ${Math.round(-dz)} m` : '') + (steep ? ', steep' : '');
      legs.push({i0, i1, bearing_deg: Math.round(b * 10) / 10, dist_m: Math.round(d), dz_m: Math.round(dz), t0_s: ts[i0], t1_s: ts[i1], to: path[i1].slice(0, 2), say});
    }
    const R = {path, timestamps: ts, dist, length_m: Math.round(L), time_s: Math.round(t), climb_m: Math.round(climb), descent_m: Math.round(desc),
      max_slope_deg: Math.round(smax * 10) / 10, cap_deg: rt.cap, speed_factor: rt.speed, runs, legs, reached: pp.reached, gap_m: pp.gap_m, target: pp.target,
      why: pp.why || null, tail: pp.tail ? [path[n - 1], ...pp.tail.slice(1)] : null};   // the red line starts where the drawn route stops
    R.note = noteOf(R);
    return R;
  }
  const mRound = v => v >= 1000 ? `${(v / 1000).toFixed(1)} km` : `${Math.max(10, Math.round(v / 10) * 10)} m`;
  // why the route stops short of the pin, in one sentence for any screen (null when it gets there)
  function noteOf(R) {
    const here = R.length_m < 25, g = mRound(R.gap_m);
    if (R.why === 'water') return here ? `The pin is in the water. You are already at the closest shore you can reach, ${g} from the pin.`
      : `The pin is in the water. The route goes to the closest shore you can reach, ${g} from the pin.`;
    if (R.why === 'island') return here ? `That spot is cut off by water (an island or a far shore). You are already at the closest shore you can reach, ${g} across the water.`
      : `That spot is cut off by water (an island or a far shore). The route stops at the closest shore you can reach, ${g} across the water.`;
    if (R.why === 'steep') return `The last ${g} to the pin is steeper than ${R.cap_deg}° or crosses a cliff. The route stops at the closest safe point.`;
    return null;
  }
  // the route in one or two sentences, the way the team's route screen says it
  function describe(R) {
    const out = [];
    const steep = R.runs.filter(r => r.cls === 2 && r.len >= 50).sort((a, b) => b.len - a.len).slice(0, 2).sort((a, b) => a.at - b.at);
    steep.forEach(r => {
      const what = r.dz > r.len * 0.12 ? 'climb' : r.dz < -r.len * 0.12 ? 'descent' : 'traverse';
      const last = r.i1 === R.path.length - 1 && r.at > 0;
      out.push(last ? `The last ${mRound(r.len)} is a steep ${what} (${r.max}°)` : r.at < 30 ? `Starts with a steep ${what} (${r.max}°) for ${mRound(r.len)}`
        : `In ${mRound(r.at)}, a steep ${what} (${r.max}°) for ${mRound(r.len)}`);
    });
    if (!steep.length) out.push(R.runs.some(r => r.cls === 1) ? 'Nothing steeper than 20°' : 'Easy ground all the way');
    return out.join('. ') + '.';
  }
  // a 3D camera bearing that looks at a spot without a ridge in front of it
  function bestBearing(lon, lat, pitch, prefer) {
    const x = xOf(lon), y = yOf(lat), z0 = zAt(x, y) + 10, lim = 90 - pitch - 8;
    let best = prefer, bs = Infinity;
    [prefer, ...Array.from({length: 12}, (_, k) => -180 + k * 30)].forEach((b, q) => {
      const th = (b + 180) * D2R, sx = Math.sin(th), sy = Math.cos(th); let mx = -90;
      for (let d = 60; d <= 5000; d += 60) {
        const px = x + sx * d, py = y + sy * d; if (px < 0 || py < 0 || px > XW || py > YH) break;
        const g = Math.atan2(zAt(px, py) - z0, d) * R2D; if (g > mx) mx = g;
      }
      const s = Math.max(0, mx - lim) * 3 + (q ? 1 + Math.abs(angd(b, prefer)) / 60 : 0);
      if (s < bs) { bs = s; best = b; }
    });
    return best;
  }
  window.TEAM8_MESH_NAV = {ms: Math.round(performance.now() - t0), W, H, K, KR};
  return {W, H, DX, DY, B, xOf, yOf, lonOf, latOf, zAt, isWater, inside, slopeAt, landKm2, EYE, CURV,
    post: {PW, PH, PDX, PDY, bounds: PBOUNDS, K}, route: {RW, RH, KR},
    sight, solve, offsetAt, inStage, reach, pathTo, buildRoute, describe, bestBearing, randomSpot, rng, seedOf, wrap, angd};
}
window.TEAM8_NAV = {init};
})();
