/*
 * RIDGELINE Follow. A locked route becomes one instruction at a time, as on the guidance mockup: the next route point,
 * the turn to make toward it from where the phone points, the straight distance to it, and what is left of the route.
 *
 *   Waypoints   the route simplified with Ramer, Douglas and Peucker (no kept chord strays more than 15 m from the
 *               walked line), plus the end of every planner leg, so a straight arrow never cuts a bend the route avoids
 *   Progress    the position is put on the route at its nearest point, searched near the last progress first, so a
 *               route that doubles back never jumps to its return half
 *   Off route   only when the distance from the route is over 60 m and over half the 95% radius. A wide circle never
 *               says off route, it says the position is uncertain, which is what a re-aim is for
 *   Never       a reroute on its own. The phone offers one and waits for a tap
 *
 * Local metres everywhere (e east, n north), so the same code serves the Moran route and the walk mode.
 * Works in the browser (window.RidgelineFollow) and in Node (require), like pace.js.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.RidgelineFollow = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';
  const DEFAULTS = Object.freeze({
    simplify_m: 15,      // the largest gap between the route and a straight arrow along it
    reach_m: 20,         // a waypoint counts as passed this far before it, along the route
    offroute_m: 60,      // closer than this to the route is on it, whatever the circle
    offroute_r95: 0.5,   // and farther than this share of the 95% radius before the phone says off route
    arrive_m: 25,        // this close to the end, along or straight, is arrived
    jump_slack_m: 60,    // progress may move this far along the route between two updates for free
    jump_cost: 0.5,      // and beyond that each metre of jump weighs as half a metre of distance from the route
    straight_deg: 12,    // a turn this small reads Straight ahead
    around_deg: 150,     // a turn this large reads Turn around
  });
  const R2D = 180 / Math.PI;
  const wrap360 = d => ((d % 360) + 360) % 360;
  const wrap180 = d => ((((d % 360) + 540) % 360) - 180);
  const bearing = (a, b) => wrap360(Math.atan2(b.e - a.e, b.n - a.n) * R2D);
  const dist = (a, b) => Math.hypot(b.e - a.e, b.n - a.n);
  const COMPASS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
  const compass = b => COMPASS[Math.round(wrap360(b) / 45) % 8];

  // Ramer, Douglas and Peucker on [{e, n}]: the indexes to keep, both ends always kept
  function simplify(pts, eps) {
    const n = pts.length;
    if (n < 3) return pts.map((p, i) => i);
    const keep = new Uint8Array(n); keep[0] = 1; keep[n - 1] = 1;
    const stack = [[0, n - 1]];
    while (stack.length) {
      const [i0, i1] = stack.pop();
      const a = pts[i0], b = pts[i1], dx = b.e - a.e, dy = b.n - a.n, L = Math.hypot(dx, dy);
      let worst = -1, at = -1;
      for (let k = i0 + 1; k < i1; k++) {
        const p = pts[k];
        const d = L > 1e-9 ? Math.abs(dx * (p.n - a.n) - dy * (p.e - a.e)) / L : dist(a, p);
        if (d > worst) { worst = d; at = k; }
      }
      if (at > 0 && worst > eps) { keep[at] = 1; stack.push([i0, at], [at, i1]); }
    }
    const out = [];
    for (let k = 0; k < n; k++) if (keep[k]) out.push(k);
    return out;
  }

  // route: { pts: [{e, n}], times: [s] (optional, one per point), legs: [{ i1, say }] (optional) }
  function build(route, opts) {
    const o = Object.assign({}, DEFAULTS, opts || {});
    const pts = route.pts.map(p => ({ e: +p.e, n: +p.n }));
    if (pts.length < 2) throw new Error('A route needs two points');
    const cum = [0];
    for (let k = 1; k < pts.length; k++) cum.push(cum[k - 1] + dist(pts[k - 1], pts[k]));
    const times = Array.isArray(route.times) && route.times.length === pts.length ? route.times.map(Number) : null;
    const legs = (route.legs || []).filter(l => Number.isInteger(l.i1) && l.i1 > 0 && l.i1 < pts.length);
    const idx = new Set(simplify(pts, o.simplify_m));
    for (const l of legs) idx.add(l.i1);
    idx.delete(0);
    const wps = [];
    for (const i of [...idx].sort((a, b) => a - b)) {
      const last = wps[wps.length - 1];
      if (last && cum[i] - last.s < 10 && i !== pts.length - 1) continue;   // two ends of one bend count once
      if (last && cum[i] - last.s < 10) wps.pop();   // the end wins over a bend just before it
      wps.push({ i, s: cum[i], e: pts[i].e, n: pts[i].n });
    }
    return { o, pts, cum, times, legs, total: cum[cum.length - 1], total_s: times ? times[times.length - 1] : null, wps, s: 0, arrived: false };
  }

  // the point of the route that best explains pos: near it, and near the last progress. A jump along the route costs
  // half its length beyond 60 m, so the return half of a hairpin never wins while the walk is on the way out, and a
  // re-fix that really moved the position along the route still does
  function locate(f, pos) {
    const { pts, cum, o } = f;
    let best = null;
    for (let k = 0; k < pts.length - 1; k++) {
      const a = pts[k], b = pts[k + 1], dx = b.e - a.e, dy = b.n - a.n, L2 = dx * dx + dy * dy;
      const u = L2 > 0 ? Math.max(0, Math.min(1, ((pos.e - a.e) * dx + (pos.n - a.n) * dy) / L2)) : 0;
      const pe = a.e + u * dx, pn = a.n + u * dy;
      const c = { k, u, s: cum[k] + u * Math.sqrt(L2), d: Math.hypot(pos.e - pe, pos.n - pn), e: pe, n: pn };
      c.score = c.d + o.jump_cost * Math.max(0, Math.abs(c.s - f.s) - o.jump_slack_m);
      if (!best || c.score < best.score) best = c;
    }
    return best;
  }

  function timeAt(f, loc) {
    if (!f.times) return null;
    const t0 = f.times[loc.k], t1 = f.times[Math.min(loc.k + 1, f.times.length - 1)];
    return t0 + (t1 - t0) * loc.u;
  }

  function turnSay(turn, brg) {
    if (turn == null) return `Head ${Math.round(wrap360(brg))}° ${compass(brg)}`;
    const a = Math.abs(turn);
    if (a <= DEFAULTS.straight_deg) return 'Straight ahead';
    if (a >= DEFAULTS.around_deg) return 'Turn around';
    return `Turn ${turn < 0 ? 'left' : 'right'} ${Math.round(a)}°`;
  }

  // pos {e, n, r95_m}, heading: true bearing the phone points, or null when unknown
  function update(f, pos, heading) {
    const o = f.o, loc = locate(f, pos);
    f.s = loc.s;
    const end = f.pts[f.pts.length - 1];
    let wi = f.wps.findIndex(w => w.s - loc.s > o.reach_m);
    if (wi < 0) wi = f.wps.length - 1;
    const wp = f.wps[wi];
    const brg = bearing(pos, wp), turn = Number.isFinite(heading) ? wrap180(brg - heading) : null;
    const r95 = Number.isFinite(pos.r95_m) ? pos.r95_m : 0;
    const off = loc.d > o.offroute_m && loc.d > o.offroute_r95 * r95;
    const left_m = Math.max(0, f.total - loc.s), toEnd = dist(pos, end);
    const arrived = !off && (left_m <= o.arrive_m || toEnd <= o.arrive_m);
    if (arrived) f.arrived = true;
    const t = timeAt(f, loc);
    const leg = f.legs.find(l => l.i1 > loc.k) || f.legs[f.legs.length - 1] || null;
    return {
      wp_index: wi, wp_count: f.wps.length, wp, wp_m: dist(pos, wp), wp_along_m: Math.max(0, wp.s - loc.s), last: wi === f.wps.length - 1,
      bearing_deg: brg, turn_deg: turn, say: arrived ? 'Arrived' : turnSay(turn, brg), compass: compass(brg),
      along_m: loc.s, left_m, total_m: f.total, done: f.total > 0 ? Math.min(1, loc.s / f.total) : 1,
      left_s: t == null || f.total_s == null ? null : Math.max(0, f.total_s - t),
      off_m: loc.d, offroute: off, arrived, to_end_m: toEnd, on: { e: loc.e, n: loc.n },
      leg_say: leg && leg.say ? leg.say : null,
    };
  }

  return Object.freeze({ DEFAULTS, simplify, build, locate, update, turnSay, bearing, wrap180, wrap360, compass });
});
