/* Team-8 live link, judge screen side.

   When this page is served by tools/live_server.py, GET /state is polled every 0.5 s. Whatever the phone
   (mockups/phone_screen with shared/solver.js) sent is drawn on the paper map:
   - a pencil line from each locked peak, along the bearing the phone measured, turned by the one heading offset the
     solver found (the phone's heading frame is unknown, no compass is used),
   - the triangle where the first three good lines cross (the navigator's "cocked hat"),
   - the 95% bubble (a circle with the area of the solver's 95% region) around the fix,
   - on Reveal, the error against the hidden demo spot from data/moran/solver_grid.json. The phone never gets that spot.

   index.html calls TEAM8_LIVE.start(ctx) once. While TEAM8_LIVE.on is true it takes its layers, panel texts, camera
   points and the small phone from here. Clicking a step or the map leaves the live view, the next phone event brings
   it back. On file:// or a plain static server (no /state) nothing happens. */
(function () {
'use strict';
const POLL_MS = 500, GRID_URL = 'data/moran/solver_grid.json', D2R = Math.PI / 180;
const EVAL_URL = 'data/moran/solver_eval.json';   // written by tests/solver.test.mjs
const L = {on: false, reveal: false, seq: 0, st: null, G: null, grid: null, ctx: null, fails: 0, lagMs: null, warned: false};
const T = {ev: null, open: false, auto: false, el: null, key: '', why: '', retryAt: 0};   // the trust card
let timer = 0;

function start(ctx) {
  L.ctx = ctx;
  if (!/^https?:$/.test(location.protocol)) return;
  poll();
}

async function poll() {
  clearTimeout(timer);
  const t0 = performance.now();
  let next = POLL_MS;
  try {
    const r = await fetch('/state', {cache: 'no-store'});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const st = await r.json();
    L.fails = 0;
    if (st && st.seq && st.seq !== L.seq) await take(st);
    next = Math.max(40, POLL_MS - (performance.now() - t0));   // a fixed 0.5 s rate, not 0.5 s plus the request
  } catch (err) {
    L.fails += 1;
    if (L.fails > 3) next = 5000;   // a plain static server has no /state: look again now and then, quietly
  }
  timer = setTimeout(poll, next);
}

async function loadGrid() {
  if (L.grid) return L.grid;
  const g = await (await fetch(GRID_URL, {cache: 'no-cache'})).json();
  L.grid = {peaks: new Map(g.peaks.map(p => [p.id, p])), demo: g.demo};
  return L.grid;
}

async function take(st) {
  const c = L.ctx;
  L.seq = st.seq;
  if (c.M.id !== 'moran') {
    if (!L.warned) { L.warned = true; c.toast('A phone is live on Mount Moran. Open ?area=moran to follow it.'); }
    return;
  }
  try { await loadGrid(); } catch (err) { c.toast('Live phone: cannot read ' + GRID_URL); return; }
  loadEval();
  const prev = L.st, wasOn = L.on;
  L.st = {session: st.session || null, event: st.event || 'lock', locks: Array.isArray(st.locks) ? st.locks : [], result: st.result || null,
    baro: st.baro || null, opts: st.opts || null, source: st.source || null, sent_at: st.sent_at || null,
    route: st.route && st.route.dest ? st.route : null};
  L.lagMs = st.sent_at ? Math.max(0, Date.now() - Date.parse(st.sent_at)) : null;
  if (!prev || prev.session !== L.st.session) L.reveal = false;
  L.on = true;
  build();
  window.TEAM8_LIVE_LAST = {seq: st.seq, event: L.st.event, locks: L.st.locks.length, lag_ms: L.lagMs,
    r95_m: L.G.r95, err_m: L.G.err, route: L.G.routeKey, shown_at: new Date().toISOString()};
  const reframe = !wasOn || !prev || prev.locks.length !== L.st.locks.length || prev.session !== L.st.session || (L.G.routeKey !== (L.prevRouteKey || ''));
  L.prevRouteKey = L.G.routeKey;
  if (reframe) c.frame();
  c.render();
  // the frame after this one has the update on screen: record when that happened (wall clock, for tests/live_e2e.mjs)
  const seq = st.seq;
  requestAnimationFrame(() => requestAnimationFrame(() => { if (window.TEAM8_LIVE_LAST && TEAM8_LIVE_LAST.seq === seq) TEAM8_LIVE_LAST.painted_at_ms = Date.now(); }));
}

// ---------------------------------------------------------------- geometry, in the viewer's metres (east, north)
function crossAt(a, b, fixXY) {   // where two pencil lines cross; null when they are nearly parallel or cross far away
  const den = a.u[0] * b.u[1] - a.u[1] * b.u[0];
  if (Math.abs(den) < Math.sin(4 * D2R)) return null;
  const dx = b.p[0] - a.p[0], dy = b.p[1] - a.p[1], t = (dx * b.u[1] - dy * b.u[0]) / den;
  const q = [a.p[0] + t * a.u[0], a.p[1] + t * a.u[1]];
  return Math.hypot(q[0] - fixXY[0], q[1] - fixXY[1]) > 5000 ? null : q;
}
function build() {
  const c = L.ctx, s = L.st, r = s.result, g = L.grid, G = {};
  const ground = (lon, lat, up) => [lon, lat, c.gz(lon, lat) + up];
  G.locks = s.locks.map((l, i) => ({...l, i, letter: 'ABCDEFGHIJ'[i] || '+', peak: g.peaks.get(l.peak_id) || null,
    resid: r && Array.isArray(r.residuals) ? r.residuals[i] || null : null}));
  G.fix = r && r.best ? ground(r.best.lon, r.best.lat, 0) : null;
  G.r95 = r && r.hdr95 ? r.hdr95.radius_m : null;
  G.area = r && r.hdr95 ? r.hdr95.area_m2 / 1e6 : null;
  G.wrong = r && r.wrong_aim ? r.wrong_aim : null;
  const wi = G.wrong && Number.isInteger(G.wrong.index) ? G.wrong.index : -1;
  G.lines = [];
  if (G.fix) {
    const fxy = c.toXY(G.fix[0], G.fix[1]);
    G.locks.forEach(l => {
      if (!l.peak || !l.resid) return;
      const back = (l.resid.true_bearing_deg + 180) * D2R, p = c.toXY(l.peak.lon, l.peak.lat), u = [Math.sin(back), Math.cos(back)];
      const len = Math.hypot(fxy[0] - p[0], fxy[1] - p[1]) + 800, n = Math.max(2, Math.ceil(len / 40)), path = [];
      for (let k = 0; k <= n; k++) { const d = len * k / n, ll = c.fromXY(p[0] + u[0] * d, p[1] + u[1] * d); path.push(ground(ll[0], ll[1], 6)); }
      G.lines.push({i: l.i, wrong: l.i === wi, path, p, u});
    });
    const ok = G.lines.filter(q => !q.wrong).slice(0, 3);
    G.tri = null; G.triSide = null;
    if (ok.length === 3) {
      const pts = [[0, 1], [1, 2], [2, 0]].map(([a, b]) => crossAt(ok[a], ok[b], fxy));
      if (pts.every(Boolean)) {
        const path = [];
        for (let e = 0; e < 3; e++) {
          const a = pts[e], b = pts[(e + 1) % 3], n = Math.max(1, Math.ceil(Math.hypot(b[0] - a[0], b[1] - a[1]) / 20));
          for (let k = 0; k < n; k++) { const ll = c.fromXY(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n); path.push(ground(ll[0], ll[1], 7)); }
        }
        path.push(path[0]);
        G.tri = path;
        G.triSide = Math.round(Math.max(...[0, 1, 2].map(e => Math.hypot(pts[(e + 1) % 3][0] - pts[e][0], pts[(e + 1) % 3][1] - pts[e][1]))));
      }
    }
  }
  const d = g.demo;
  G.truth = d ? ground(d.lon, d.lat, 0) : null;
  G.err = G.fix && G.truth ? Math.round(c.dist(G.fix, G.truth)) : null;
  G.inside = G.err !== null && G.r95 !== null && G.err <= G.r95;
  const sn = r && r.suggested_next ? g.peaks.get(r.suggested_next.peak_id) : null;
  G.next = sn && !s.locks.some(l => l.peak_id === sn.id) ? sn : null;
  // the destination and safe route the person picked on the phone (shared/route.js), coloured like the judge's route
  const R = s.route;
  G.route = R;
  G.routeKey = R ? [R.dest.name, R.ok, R.length_m || 0, R.cap_deg].join('|') : '';
  G.routeSegs = R && R.ok && Array.isArray(R.segments) ? R.segments.map(q => ({cls: q.cls | 0, path: q.path.map(p => ground(p[0], p[1], 6))})) : [];
  G.dest = R ? {pos: ground(R.dest.lon, R.dest.lat, 10), name: R.dest.name, ok: R.ok} : null;
  if (G.routeSegs.length) G.next = null;   // past the peaks: the route is the next thing
  L.G = G;
}

// ---------------------------------------------------------------- what index.html asks for while live
// Every live layer exists from "Phone ready" on, with empty data until it has something to draw: deck.gl compiles a
// layer's shaders when the layer first appears, which can cost a second on a slow GPU, and that must not land on Lock 1.
function labelled(p) {   // the sheet already prints this summit's name (index.html, M.peaks)
  const c = L.ctx;
  return (c.M.peaks || []).some(q => c.dist([q.lon, q.lat], [p.lon, p.lat]) < 150);
}
function layers() {
  const c = L.ctx, D = c.deck, G = L.G, top = c.ON_TOP, out = [];
  if (!G) return out;
  const fix = G.fix ? [{pos: [G.fix[0], G.fix[1], G.fix[2] + 8], r: G.r95 || 0}] : [];
  const rev = L.reveal && G.fix && G.truth ? [{a: [G.fix[0], G.fix[1], G.fix[2] + 9], pos: [G.truth[0], G.truth[1], G.truth[2] + 9]}] : [];
  const pk = G.locks.filter(l => l.peak).map(l => ({pos: [l.peak.lon, l.peak.lat, l.peak.elev_m + 8], letter: l.letter, name: l.peak.name,
    named: labelled(l.peak), wrong: !!(G.wrong && G.wrong.index === l.i)}));
  const nx = G.next ? [{pos: [G.next.lon, G.next.lat, G.next.elev_m + 8], name: `aim next: ${G.next.name}`}] : [];
  const RC = [[227, 171, 6], [234, 122, 28], [212, 56, 108]];   // easy, moderate, steep, as the judge route key
  const dst = G.dest ? [G.dest] : [];
  out.push(new D.PathLayer({id: 'live-route-casing', data: G.routeSegs, getPath: q => q.path, getColor: [251, 250, 246, 235], getWidth: 8, widthUnits: 'pixels',
    jointRounded: true, capRounded: true, parameters: top}));
  out.push(new D.PathLayer({id: 'live-route', data: G.routeSegs, getPath: q => q.path, getColor: q => RC[q.cls] || RC[0], getWidth: 4.5, widthUnits: 'pixels',
    jointRounded: true, capRounded: true, parameters: top}));
  out.push(new D.ScatterplotLayer({id: 'live-bubble', data: fix.filter(q => q.r), getPosition: q => q.pos, getRadius: q => q.r, radiusUnits: 'meters', filled: true,
    getFillColor: [217, 72, 28, 52], stroked: true, getLineColor: [168, 51, 12, 235], lineWidthMinPixels: 1.8, parameters: top}));
  out.push(new D.PathLayer({id: 'live-pencil-casing', data: G.lines, getPath: q => q.path, getColor: [251, 250, 246, 210], getWidth: 5.5, widthUnits: 'pixels',
    jointRounded: true, capRounded: true, parameters: top}));
  out.push(new D.PathLayer({id: 'live-pencil', data: G.lines, getPath: q => q.path, getColor: q => q.wrong ? c.RED : c.INK, getWidth: q => q.wrong ? 2.6 : 1.9,
    widthUnits: 'pixels', jointRounded: true, capRounded: true, parameters: top}));
  out.push(new D.PathLayer({id: 'live-triangle', data: G.tri ? [{path: G.tri}] : [], getPath: q => q.path, getColor: [168, 51, 12, 255], getWidth: 2.6,
    widthUnits: 'pixels', jointRounded: true, parameters: top}));
  out.push(new D.TextLayer({id: 'live-peak-marks', data: pk.concat(nx), ...c.TXT, getPosition: q => q.pos, getText: () => '▲', getSize: 15, getColor: c.INK,
    outlineWidth: 2, getAlignmentBaseline: 'center', parameters: top}));
  out.push(new D.TextLayer({id: 'live-peak-names', data: pk.filter(q => !q.named), ...c.TXT, getPosition: q => q.pos, getText: q => q.name, getSize: 15,
    fontWeight: 600, getColor: c.INK, getPixelOffset: [0, -9], getAlignmentBaseline: 'bottom', parameters: top}));
  out.push(new D.TextLayer({id: 'live-next', data: nx, ...c.TXT, getPosition: q => q.pos, getText: q => q.name, getSize: 13, fontWeight: 600,
    getColor: [42, 74, 116], getPixelOffset: [0, 11], getAlignmentBaseline: 'top', parameters: top}));
  out.push(new D.TextLayer({id: 'live-letters', data: pk, ...c.TXT, getPosition: q => q.pos, getText: q => q.letter, getSize: 13, fontWeight: 700,
    getColor: [255, 255, 255], outlineWidth: 0, background: true, getBackgroundColor: q => q.wrong ? c.RED : c.INK, backgroundPadding: [5, 3, 5, 2],
    getPixelOffset: [0, 16], getAlignmentBaseline: 'top', parameters: top}));
  out.push(new D.ScatterplotLayer({id: 'live-fix', data: fix, getPosition: q => q.pos, getRadius: 8, radiusUnits: 'pixels', filled: true,
    getFillColor: [255, 255, 255, 190], stroked: true, getLineColor: c.INK, lineWidthMinPixels: 2.4, parameters: top}));
  out.push(new D.ScatterplotLayer({id: 'live-fix-dot', data: fix, getPosition: q => q.pos, getRadius: 2.2, radiusUnits: 'pixels', getFillColor: c.INK, parameters: top}));
  out.push(new D.TextLayer({id: 'live-fix-label', data: fix, ...c.TXT, getPosition: q => q.pos, getText: () => `Phone fix ±${G.r95} m`, getSize: 13, fontWeight: 700,
    getColor: c.INK, getPixelOffset: [12, 0], getTextAnchor: 'start', getAlignmentBaseline: 'center', updateTriggers: {getText: G.r95}, parameters: top}));
  out.push(new D.LineLayer({id: 'live-err', data: rev, getSourcePosition: q => q.a, getTargetPosition: q => q.pos, getColor: c.RED, getWidth: 2.5, widthUnits: 'pixels',
    parameters: top}));
  out.push(new D.ScatterplotLayer({id: 'live-truth', data: rev, getPosition: q => q.pos, getRadius: 6, radiusUnits: 'pixels', getFillColor: c.RED, stroked: true,
    getLineColor: [255, 255, 255], lineWidthMinPixels: 2, parameters: top}));
  out.push(new D.TextLayer({id: 'live-truth-label', data: rev, ...c.TXT, getPosition: q => q.pos, getPixelOffset: [0, -12], getAlignmentBaseline: 'bottom',
    getText: () => `Hidden demo spot, ${G.err} m from the fix`, getSize: 14, getColor: [150, 25, 25], fontWeight: 700, updateTriggers: {getText: G.err}, parameters: top}));
  out.push(new D.ScatterplotLayer({id: 'live-dest', data: dst, getPosition: q => q.pos, getRadius: 7, radiusUnits: 'pixels', getFillColor: q => q.ok ? [255, 218, 115] : c.RED,
    stroked: true, getLineColor: c.INK, lineWidthMinPixels: 2.2, updateTriggers: {getFillColor: G.routeKey}, parameters: top}));
  out.push(new D.TextLayer({id: 'live-dest-label', data: dst, ...c.TXT, getPosition: q => q.pos, getText: q => q.ok ? q.name : `${q.name}: no safe route`, getSize: 14,
    fontWeight: 700, getColor: c.INK, getPixelOffset: [0, -12], getAlignmentBaseline: 'bottom', updateTriggers: {getText: G.routeKey}, parameters: top}));
  return out;
}

function framePts() {
  const c = L.ctx, G = L.G, B = c.B, pts = [];
  if (!G || !G.fix) {
    [[B[0], B[1]], [B[0], B[3]], [B[2], B[1]], [B[2], B[3]]].forEach(([lon, lat]) => pts.push([lon, lat, c.gz(lon, lat)]));
    c.NAMED.forEach(p => pts.push([p.lon, p.lat, p.elev_m]));
    return {pts, pad: 0, look: -62, key: null, pitch: 55};
  }
  const seen = G.locks.filter(l => l.peak).map(l => l.peak);
  seen.forEach(p => pts.push([p.lon, p.lat, p.elev_m, 1]));
  c.ring(pts, G.fix, Math.max(350, (G.r95 || 0) * 1.3));
  if (L.reveal && G.truth) pts.push([G.truth[0], G.truth[1], G.truth[2]]);
  if (G.dest) {   // the route is the story now: frame the fix, the route and the destination
    pts.length = 0; c.ring(pts, G.fix, Math.max(350, (G.r95 || 0) * 1.3));
    G.routeSegs.forEach(q => q.path.forEach((p, i) => { if (i % 4 === 0) pts.push([p[0], p[1], p[2]]); }));
    pts.push(G.dest.pos.slice(0, 3));
    return {pts, pad: 250, look: c.brg(G.fix, G.dest.pos), key: G.fix, pitch: 45};
  }
  const cx = seen.length ? seen.reduce((a, p) => a + p.lon, 0) / seen.length : G.fix[0], cy = seen.length ? seen.reduce((a, p) => a + p.lat, 0) / seen.length : G.fix[1];
  return {pts, pad: 250, look: seen.length ? c.brg(G.fix, [cx, cy]) : -62, key: G.fix, pitch: 42};
}

const sgn = v => (v >= 0 ? '+' : '−') + Math.abs(v).toFixed(1) + '°';
const fmtT = s => { const m = Math.max(1, Math.round(s / 60)); return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, '0')} min`; };
const angd = (a, b) => ((a - b + 540) % 360) - 180;
function copy() {
  const s = L.st, r = s.result, G = L.G, n = s.locks.length, nx = G.next ? G.next.name : null;
  const enough = r && r.enough_or_one_more === 'enough';
  // keep each text under about 150 characters, the card has room for four lines
  if (n && G.wrong) return ['Re-aim', `${G.wrong.message}${Number.isInteger(G.wrong.index) ? ' Its line is red.' : ''}`];
  const R = G.route;
  if (R && R.ok && R.here) return ['At the destination', `The phone is already at ${R.dest.name}.`];
  if (R && R.ok) return ['Route', `${R.dest.name}: ${R.length_m >= 1000 ? (R.length_m / 1000).toFixed(1) + ' km' : R.length_m + ' m'}, about ${fmtT(R.time_s)}${R.injured ? ' at injured pace' : ''}. ${R.say}`];
  if (R && !R.ok && R.reason === 'no_safe_route') return ['Stay. Signal. Wait.', `No safe way to ${R.dest.name} without water or ground steeper than ${R.cap_deg}°. The phone tells the person to stay put and signal.`];
  if (L.reveal && G.err !== null) return ['Reveal', `The hidden demo spot is ${G.err} m from the fix, ${G.inside ? 'inside' : 'outside'} the 95% bubble (±${G.r95} m). The phone never had it.`];
  if (!n) return ['Phone ready', 'The phone has the map tile, a barometer and its own aim. No GPS, no compass. Each Lock on the phone lands here.'];
  if (enough) return ['Fix ready', `Lines from ${n} peaks cross in a ${G.triSide ? G.triSide + ' m' : 'small'} triangle. No compass: the solver removes the phone's heading offset. Press Reveal.`];
  if (n === 1) return ['Lock 1', `One peak and the barometer leave a ring of possible spots.${nx ? ` Next: ${nx}.` : ''}`];
  if (n === 2) return ['Lock 2', `Two lines cross, but the heading offset is still loose. One more peak pins it down.${nx ? ` Next: ${nx}.` : ''}`];
  return [`Lock ${n}`, `The lines do not close tightly enough yet.${nx ? ` Next: ${nx}.` : ''}`];
}
function panels() {
  const c = L.ctx, $ = c.$, s = L.st, r = s.result, G = L.G, n = s.locks.length, [title, text] = copy();
  $('stepEyebrow').textContent = n ? `Live from the phone · ${n} lock${n === 1 ? '' : 's'}` : 'Live from the phone';
  $('stepTitle').textContent = title; $('stepText').textContent = c.NB(text);
  document.querySelectorAll('[data-step]').forEach(b => {
    const rev = +b.dataset.step === 7 && L.reveal;
    b.classList.toggle('act', rev); b.classList.remove('done'); b.disabled = false; b.title = +b.dataset.step === 7 ? 'Show the hidden demo spot' : 'Leave the live view';
    if (rev) b.setAttribute('aria-current', 'step'); else b.removeAttribute('aria-current');
  });
  if (r && r.hdr95) {
    $('fixEyebrow').textContent = 'You are within · 95%';
    c.countTo($('fixBig'), 'r95', r.hdr95.radius_m, v => `±${Math.round(v)} m`);
    const lag = L.lagMs !== null ? `<br>on this screen ${(L.lagMs / 1000).toFixed(1)} s after the phone sent it` : '';
    $('fixSub').innerHTML = c.NB(`${c.fmtKm2(G.area)} km² · confidence ${c.esc(r.confidence)}`) + lag;
  } else {
    $('fixEyebrow').textContent = 'Where you can be · 95%';
    c.countTo($('fixBig'), 'km2', c.NAV.landKm2, v => `${c.fmtKm2(v)} km²`);
    $('fixSub').textContent = n ? 'no spot fits these locks' : 'all the land on this sheet';
  }
  const h0 = s.locks[0] ? s.locks[0].heading_deg : 0;
  $('meter').innerHTML = [0, 1, 2].map(q => {
    const l = G.locks[q], done = !!l, bad = done && G.wrong && G.wrong.index === q, next = !done && q === n;
    const nm = l ? (l.peak ? l.peak.name : l.peak_name || l.peak_id) : next && G.next ? G.next.name : '·';
    const rd = !l ? (next ? 'aim next' : '') : bad ? 'does not fit' : q === 0 ? (Number.isFinite(l.elevation_deg) ? `tilt ${sgn(l.elevation_deg)}` : 'held') : `${sgn(angd(l.heading_deg, h0))} from A`;
    return `<div class="slot${done ? ' done' : next ? ' next' : ''}" role="listitem"><div class="top"><span class="lt"${bad ? ' style="background:#c62828;border-color:#c62828"' : ''}>${'ABC'[q]}</span><span class="nm">${c.esc(nm)}</span></div><span class="rd">${rd}</span></div>`;
  }).join('');
  const enough = r && r.enough_or_one_more === 'enough';
  const [vt, vc] = !n ? ['Aim at a peak', 'next'] : G.wrong ? ['Check the aim', 'warn'] : enough ? ['Enough peaks', 'ok'] : ['One more peak', 'next'];
  $('verdict').textContent = vt; $('verdict').className = vc;
  $('sensors').hidden = false; $('destcard').hidden = true; $('walkbar').style.display = 'none'; $('banner').style.display = 'none';
  const set = (id, on, txt) => { $(id).classList.toggle('on', on); $(id).textContent = c.NB(txt); };
  set('sBaro', !!s.baro, s.baro ? `${Math.round(s.baro.alt_m)} m ±${s.baro.band_m}` : 'not set');
  set('sGyro', n >= 2, n >= 2 ? s.locks.slice(1, 3).map((l, q) => `${'BC'[q]} ${sgn(angd(l.heading_deg, h0))}`).join('  ') : n === 1 ? 'A held' : 'waiting');
  const tilt = !s.opts || s.opts.use_elevation !== false;
  set('sTilt', n >= 1 && tilt, !tilt ? 'not used' : n ? s.locks.slice(0, 3).map(l => sgn(l.elevation_deg)).join('  ') : 'waiting');
  set('sComp', false, 'not used'); $('sComp').classList.add('off');
  const h = $('hint'), key = `live|${L.reveal}|${!!G.fix}|${n}`;
  if (h.dataset.html !== key) {
    h.dataset.html = key;   // updateHint in index.html redraws the hint when this differs from its own text
    h.classList.remove('locked');
    h.innerHTML = `${c.CROSS}<span>Live from the phone · ${n} lock${n === 1 ? '' : 's'}</span><span class="presets">${G.fix ? `<button data-live="reveal">${L.reveal ? 'Hide the spot' : 'Reveal'}</button>` : ''}${T.ev && T.ev !== 'loading' ? `<button data-live="trust" class="${T.open ? 'act' : ''}" aria-pressed="${T.open}" title="How far to trust the fix: 200 simulated fixes">Trust</button>` : ''}<button data-live="leave" title="Back to the judge demo, click the map to drop a pilot">Leave live view</button></span>`;
    h.querySelectorAll('[data-live]').forEach(b => b.onclick = () => b.dataset.live === 'reveal' ? toggleReveal() : b.dataset.live === 'trust' ? toggleTrust() : leave());
  }
  drawTrust();
  return true;
}

function phone() {
  const c = L.ctx, $ = c.$, s = L.st, r = s.result, G = L.G, n = s.locks.length, last = s.locks[n - 1];
  const enough = r && r.enough_or_one_more === 'enough';
  $('phStop').style.display = 'none'; $('arrow').style.display = ''; $('arrow').style.opacity = 0.15; $('arrow').style.transform = 'rotate(0deg)';
  $('phBig').textContent = c.NB(r && r.hdr95 ? `±${r.hdr95.radius_m} m` : n ? 'Check aim' : 'Ready');
  $('phSay').textContent = c.NB(G.wrong ? G.wrong.message : enough ? 'Enough. You are here.' : n ? `${last.peak_name || last.peak_id} locked.${G.next ? ` Next: ${G.next.name}.` : ''}` : 'Aim at the first peak and press Lock.');
  $('phBtn').textContent = 'Leave live view';
  const R = G.route;
  if (R && R.ok && !R.here && !G.wrong) {   // the phone is guiding: its first leg, as the phone says it
    $('arrow').style.opacity = 1;
    const to = (G.routeSegs[0] && G.routeSegs[0].path[Math.min(3, G.routeSegs[0].path.length - 1)]) || G.dest.pos;
    $('arrow').style.transform = `rotate(${Math.round(angd(c.brg(G.fix, to), (c.state && c.state.heading) || 0))}deg)`;   // as index.html: relative to the camera
    $('phBig').textContent = c.NB(R.length_m >= 1000 ? `${(R.length_m / 1000).toFixed(1)} km` : `${R.length_m} m`);
    $('phSay').textContent = c.NB(`${R.next || 'Follow the route'}. To ${R.dest.name}.`);
  } else if (R && !R.ok && R.reason === 'no_safe_route' && !G.wrong) {
    $('arrow').style.display = 'none'; $('phStop').style.display = '';
    $('phBig').textContent = c.NB(`No route at ${R.cap_deg}°`);
    $('phSay').textContent = c.NB(`${R.dest.name} is out of reach. Stay put and signal.`);
  }
  $('phFix').className = 'tag' + (enough ? ' ok' : ''); $('phFix').textContent = c.NB(r && r.hdr95 ? `Fix ±${r.hdr95.radius_m} m` : 'No fix');
  $('phCoords').innerHTML = G.fix ? `<span>LAT</span> ${G.fix[1].toFixed(5)}  <span>LON</span> ${G.fix[0].toFixed(5)}<br><span>MGRS</span> ${c.mgrs(G.fix[0], G.fix[1])}<br><span>LOCKS</span> ${n}  <span>HDG</span> phone frame, no compass`
    : '<span>LAT</span> ?  <span>LON</span> ?<br><span>MGRS</span> ?<br><span>HDG</span> phone frame, no compass';
  ['tape', 'sky'].forEach(id => { const cv = $(id), x = cv.getContext('2d'); x.clearRect(0, 0, cv.width, cv.height); });
  return true;
}

// ---------------------------------------------------------------- trust card (P1.4), from solver_eval.json
// What 200 simulated fixes from the demo spot say about the live ±N m: the share of test fixes that came that close,
// how the error shrinks with each peak, wrong-aim and heading-frame results, solve time, and what the test leaves out.
async function loadEval() {
  if (T.ev || T.ev === 'loading' || performance.now() < T.retryAt) return T.ev;
  T.ev = 'loading';
  try {
    const r = await fetch(EVAL_URL, {cache: 'no-cache'});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    T.ev = await r.json(); T.why = '';
  } catch (err) {
    T.ev = null; T.why = String(err && err.message || err); T.retryAt = performance.now() + 5000;   // the next phone event tries again
    console.warn('Live phone: no trust card, cannot read ' + EVAL_URL + ': ' + T.why);
    return null;
  }
  if (L.on && L.ctx) { L.ctx.$('hint').dataset.html = ''; L.ctx.render(); }   // the Trust button appears
  return T.ev;
}
const TRUST_CSS = [
  '.t8-trust{position:fixed;left:392px;bottom:92px;width:336px;z-index:6;background:var(--card);border:1px solid var(--rule);border-radius:var(--r);box-shadow:var(--shadow);padding:13px 16px 12px;color:var(--ink);font:400 13px/1.45 var(--ui)}',
  '.t8-trust[hidden]{display:none}',
  '.t8-trust .ey{margin:0 0 3px;font:600 10.5px/1.2 var(--ui);letter-spacing:.08em;text-transform:uppercase;color:var(--mute)}',
  '.t8-trust h3{margin:0;font:700 16px/1.3 var(--ui)}.t8-trust svg{display:block;width:100%;height:auto;margin:6px 0 0}.t8-trust .cap{margin:0 0 6px;color:var(--ink-2)}',
  '.t8-trust .key{margin:0 0 4px;color:var(--mute);font-size:11.5px}.t8-trust .key b{display:inline-block;width:16px;height:0;border-top:2px solid var(--ink);vertical-align:middle;margin:0 3px 0 6px}',
  '.t8-trust .key b.k2{border-top:1.3px solid #958e7f}.t8-trust .key b.k1{border-top:1.3px dashed #b3ab9b}',
  '.t8-trust .key i{display:inline-block;width:8px;height:8px;border-radius:50%;background:#c62828;vertical-align:middle;margin:0 3px 0 8px}',
  '.t8-trust dl{display:grid;grid-template-columns:1fr auto;gap:1px 12px;margin:0;padding:6px 0;border-top:1px solid var(--rule-2);border-bottom:1px solid var(--rule-2)}',
  '.t8-trust dt{color:var(--ink-2)}.t8-trust dd{margin:0;text-align:right;font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap}',
  '.t8-trust .fine{margin:6px 0 0;color:var(--mute);font-size:11.5px;line-height:1.4}',
  '.t8-trust .x{position:absolute;top:8px;right:8px;width:26px;height:26px;padding:0;justify-content:center}',
  '@media(max-width:1100px){.t8-trust{left:12px;bottom:84px;width:min(336px,calc(100vw - 24px))}}'].join('\n');
function trustEl() {
  if (T.el) return T.el;
  const st = document.createElement('style');
  st.textContent = TRUST_CSS;
  document.head.appendChild(st);
  const el = document.createElement('section');
  el.id = 'trustCard'; el.className = 't8-trust'; el.hidden = true; el.setAttribute('aria-label', 'How far to trust the fix');
  document.body.appendChild(el);
  T.el = el;
  return el;
}
function toggleTrust(force) {
  T.open = typeof force === 'boolean' ? force : !T.open;
  if (L.ctx) { L.ctx.$('hint').dataset.html = ''; L.ctx.render(); }
  drawTrust();
}
const pc = v => (v * 100 < 10 && v > 0 ? (v * 100).toFixed(1) : Math.round(v * 100)) + '%';
function trustChart(ev, r95, err) {
  const W = 308, H = 104, x0 = 30, x1 = W - 10, y0 = 10, y1 = 86;
  const sets = [1, 2, 3].map(n => (ev.by_locks || []).find(b => b.locks === n)).filter(b => b && Array.isArray(b.errors_m) && b.errors_m.length);
  if (!sets.length) return '';
  const top = Math.max(200, ...sets.filter(b => b.locks === 3).map(b => Math.ceil(b.errors_m[b.errors_m.length - 1] / 50) * 50));
  const X = m => x0 + Math.min(1, m / top) * (x1 - x0), Y = f => y1 - f * (y1 - y0);
  const curve = e => { let d = `M${X(0).toFixed(1)} ${Y(0)}`; e.forEach((m, i) => { d += `H${X(m).toFixed(1)}V${Y((i + 1) / e.length).toFixed(1)}`; }); return d + `H${X(top)}`; };
  const style = {1: 'stroke="#c4bdae" stroke-width="1.3" stroke-dasharray="3 2"', 2: 'stroke="#958e7f" stroke-width="1.3"', 3: 'stroke="#1f1d1a" stroke-width="2.2"'};
  let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Share of 200 simulated fixes within a distance of the truth, with 1, 2 and 3 peaks">`;
  [0.5, 0.9].forEach(f => { s += `<path d="M${x0} ${Y(f)}H${x1}" stroke="#e5dfd2" stroke-dasharray="2 3"/><text x="${x0 - 4}" y="${Y(f) + 3.5}" text-anchor="end" font-size="9.5" fill="#6b665b">${f * 100}%</text>`; });
  s += `<path d="M${x0} ${y0}V${y1}H${x1}" fill="none" stroke="#d5cebf"/>`;
  for (let m = 0; m <= top; m += 50) s += `<text x="${m === top ? X(m) + 4 : X(m)}" y="${y1 + 12}" text-anchor="${m === top ? 'end' : 'middle'}" font-size="9.5" fill="#6b665b">${m}${m === top ? ' m' : ''}</text>`;
  sets.forEach(b => { s += `<path d="${curve(b.errors_m)}" fill="none" ${style[b.locks]}/>`; });
  const three = sets.find(b => b.locks === 3);
  if (three && Number.isFinite(r95)) {
    const f = three.errors_m.filter(m => m <= r95).length / three.errors_m.length;
    s += `<path d="M${X(r95)} ${y0 - 2}V${y1}" stroke="#d9481c" stroke-width="1.5" stroke-dasharray="4 2"/><circle cx="${X(r95)}" cy="${Y(f)}" r="3" fill="#d9481c"/>`;
    s += `<text x="${X(r95) + 4}" y="${y1 - 4}" font-size="10" fill="#a8330c" font-weight="700">${r95} m</text>`;
  }
  if (three && Number.isFinite(err)) {
    const f = three.errors_m.filter(m => m <= err).length / three.errors_m.length;
    s += `<circle cx="${X(err)}" cy="${Y(f)}" r="4.5" fill="#c62828" stroke="#fff" stroke-width="1.5"><title>this fix, ${err} m off</title></circle>`;   // the key and the caption name it
  }
  return s + '</svg>';
}
function drawTrust() {
  const ev = T.ev;
  if (!ev || ev === 'loading' || !L.on) { if (T.el) T.el.hidden = true; return; }
  const el = trustEl(), G = L.G || {}, r95 = Number.isFinite(G.r95) ? G.r95 : null, err = L.reveal && Number.isFinite(G.err) ? G.err : null;
  el.hidden = !T.open;
  const key = [T.open, r95, err].join('|');
  if (!T.open || key === T.key) return;
  T.key = key;
  const t3 = ev.three_locks || {}, by = n => (ev.by_locks || []).find(b => b.locks === n) || {}, wa = ev.wrong_aim || {}, ti = ev.timing || {}, nz = ev.noise || {};
  const fr = Array.isArray(ev.frame_independence) ? ev.frame_independence : [];
  const E = Array.isArray(t3.errors_m) && t3.errors_m.length ? t3.errors_m : null;
  const within = r95 !== null && E ? E.filter(m => m <= r95).length / E.length : null, p95 = E ? E[Math.ceil(0.95 * E.length) - 1] : null;
  // ±N m is the radius of a circle with the 95% region's area. The region is not a circle around the fix, so a circle
  // of that radius holds fewer than 95% of the test fixes: say so, and give the radius that did hold 19 in 20.
  const cap = (within !== null ? `${pc(within)} of the 3-peak test fixes were within ${r95} m of the truth, 19 in 20 within ${p95} m. ` : E ? `19 in 20 of the 3-peak test fixes were within ${p95} m of the truth. ` : '')
    + `The 95% region is not a circle, it held the truth in ${pc(t3.truth_in_95_share)}.${err !== null ? ` This fix is ${err} m off.` : ''}`;
  const ms = v => (Number.isFinite(v) ? (v < 10 ? v.toFixed(1) : Math.round(v)) : '?');
  el.innerHTML = `<button class="x" data-trust="close" aria-label="Close">×</button><p class="ey">Tested · ${ev.trials} simulated fixes</p>`
    + `<h3>${r95 !== null ? `How far to trust ±${r95} m` : 'How far to trust the fix'}</h3>${trustChart(ev, r95, err)}`
    + `<p class="key">Fixes this close<b></b>3 peaks<b class="k2"></b>2<b class="k1"></b>1${err !== null ? '<i></i>this fix' : ''}</p><p class="cap">${cap}</p><dl>`
    + `<dt>Median error, 1 · 2 · 3 peaks</dt><dd>${by(1).median_err_m} · ${by(2).median_err_m} · ${by(3).median_err_m} m</dd>`
    + `<dt>Wrong peak flagged · named</dt><dd>${pc(wa.detectable_noisy_flagged_share)} · ${pc(wa.detectable_noisy_named_share)}</dd>`
    + `<dt>False alarm, no wrong peak</dt><dd>${pc(wa.false_alarm_share_without_swap)}</dd>`
    + `<dt>Same cell in any heading frame</dt><dd>${fr.filter(f => f.err_m === 0).length} of ${fr.length}</dd>`
    + `<dt>Solve time, median · max</dt><dd>${ms(ti.median_ms)} · ${ms(ti.max_ms)} ms</dd></dl>`
    + `<p class="fine">Synthetic: one 30 m DEM makes the locks and the model (noise ${nz.heading_sigma_deg}° heading, ${nz.elevation_sigma_deg}° tilt, ${nz.baro_sigma_m} m barometer). No DEM, lens or sensor bias, no misjudged summits, no real phone.</p>`;
  el.querySelector('[data-trust="close"]').onclick = () => toggleTrust(false);
}

function eyePos() { const G = L.G; return G && G.fix ? [G.fix[0], G.fix[1], G.fix[2]] : null; }
function toggleReveal() {
  const c = L.ctx;
  if (!L.G || !L.G.fix) { c.toast('No fix from the phone yet.'); return; }
  L.reveal = !L.reveal;
  if (L.reveal && !T.auto && T.ev && T.ev !== 'loading') { T.auto = true; T.open = true; c.$('hint').dataset.html = ''; }   // the first Reveal opens the trust card once
  c.frame(); c.render();
}
function leave(quiet) {
  const c = L.ctx;
  L.on = false; L.reveal = false;
  if (T.el) T.el.hidden = true;
  if (quiet) return;
  c.toast('Left the live view. The next lock on the phone brings it back.');
  c.frame(); c.render();
}

window.TEAM8_LIVE = {start, layers, framePts, panels, phone, eyePos, toggleReveal, leave, toggleTrust,
  get trust() { return {loaded: !!T.ev && T.ev !== 'loading', state: typeof T.ev === 'object' && T.ev ? 'ok' : String(T.ev), open: T.open, why: T.why}; }, get on() { return L.on; }, get reveal() { return L.reveal; }, get state() { return L.st; }, get geometry() { return L.G; }};
})();
