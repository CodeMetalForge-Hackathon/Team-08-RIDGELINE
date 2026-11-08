/*
 * RIDGELINE Walk here, Auto-PACE at the venue. The phone walks a person to a pin on the floor and buzzes when they get
 * there, on the best source it still has, and its 95% circle says how sure it is. The logic only, no screen.
 *
 *   Map       flat metres, e to the right and n ahead as the phone faced when the walk started (up on the mini map).
 *             The camera reports its own frame (WebXR with ARCore underneath, x right, y up, z back). A link, one
 *             turn and one shift, takes it to the map, so the map never jumps when the camera starts a new frame
 *   CAMERA    visual-inertial tracking (the camera image and the motion sensors together), used as it comes. The
 *             circle grows by 2% of the distance walked under it, a usual drift for a phone indoors, which the trial
 *             log then measures
 *   STEPS     the camera is lost (a pocket, the dark, a blank wall). Steps from the accelerometer, at the stride the
 *             camera measured on this walk, in the direction the gyro says. The circle grows as Auto-PACE tier C in
 *             pace.js, 1 sigma = distance x max(8%, sin 6 deg), so 95% is 2.45 sigma, added to the circle at the switch.
 *             A phone going into a pocket turns on the way in. In the first 2 s after the camera is lost, a turn made
 *             while the phone also tilts fast (over 45 deg a second) is the pocket, and the walk keeps its direction.
 *             A turn with the tilt steady is the walker turning, then and later, and the walk turns with it
 *   Back      the camera returns. If it moved about as far as the steps did and faces the way the gyro says (or, with
 *             no gyro, lands within twice the circle plus 1 m), it found its own map again and the position snaps to
 *             it, the steps were the ones off (a turn made while pocketing misleads them). If it comes back where it
 *             got lost, or far off facing another way, it started over, and the link is turned and shifted so it
 *             carries on from the step estimate. The circle shrinks only as far as the two agree. The pocket rule
 *             cannot tell a turn of the body from a turn of the phone while the phone tilts, so cover the lens with a
 *             hand to show steps cleanly, and pocket the phone before turning
 *   On the X  standing on the tape at the start is the only anchor. The distance from the start there is the trial's
 *             error, and the position goes back to the start with the base circle
 *
 * Works in the browser (window.RidgelineWalk) and in Node (require), like pace.js and follow.js.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.RidgelineWalk = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';
  const D2R = Math.PI / 180, R2D = 180 / Math.PI;
  const K95 = Math.sqrt(-2 * Math.log(0.05));   // 2.4477, the 95% radius of a round 2D Gaussian, in sigmas
  const DEFAULTS = Object.freeze({
    base_r95_m: 0.25,      // a phone held over the tape X marks the spot this well
    cam_frac: 0.02,        // CAMERA, the circle grows by 2% of the distance walked under it
    along_frac: 0.08,      // STEPS, as Auto-PACE tier C (pace.js), stride error 8% of the distance, 1 sigma
    cross_deg: 6,          // and heading error 6 deg, 1 sigma
    idle_mps: 1,           // STEPS with the motion sensor silent, up to 1 m/s in any direction (pace.js idle)
    stride_m: 0.7,         // until the camera has measured this walker (pace.js STRIDE.healthy)
    stride_min_steps: 8,   // the measured stride is used after this many steps under the camera
    stride_lo: 0.35,
    stride_hi: 1.1,
    stride_memory: 30,     // the measured stride leans on about the last 30 steps
    step_gap_s: 2,         // a longer gap between two steps is a pause, and the move across it is not a stride
    settle_s: 2,           // after the camera is lost, a phone going into a pocket does it within this window
    pocket_tilt_dps: 45,   // and its turns while it tilts faster than this are the pocket, not the walk
    back_m: 1,             // the camera back within the larger of this and the circle may have found its own map
    same_yaw_deg: 20,      // and it did, however far off the steps are, if it faces within this of what the gyro says
    ori_follow: 0.03,      // how fast the gyro's offset to the map follows the camera, per orientation event
    trail_m: 0.25,         // a trail point every 25 cm, which also keeps hand shake out of the distance walked
    trail_max: 4000,
    arrive_cam_m: 0.6,     // arrived, under the camera
    arrive_steps_m: 1.5,   // arrived, on steps
    arrive_hyst_m: 0.8,    // and no longer arrived only this much farther out, so the buzz never stutters
  });

  const wrap360 = d => ((d % 360) + 360) % 360;
  const wrap180 = d => ((((d % 360) + 540) % 360) - 180);
  const dist = (a, b) => Math.hypot(b.e - a.e, b.n - a.n);
  const bearing = (a, b) => wrap360(Math.atan2(b.e - a.e, b.n - a.n) * R2D);
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  // the camera frame to the map. (x, -z) turned clockwise by d degrees, then shifted by (te, tn)
  function toMap(link, x, z) {
    const c = Math.cos(link.d * D2R), s = Math.sin(link.d * D2R), e0 = x, n0 = -z;
    return { e: e0 * c + n0 * s + link.te, n: -e0 * s + n0 * c + link.tn };
  }
  function toCam(link, e, n) {
    const c = Math.cos(link.d * D2R), s = Math.sin(link.d * D2R), de = e - link.te, dn = n - link.tn;
    const e0 = de * c - dn * s, n0 = de * s + dn * c;
    return { x: e0, z: -n0 };
  }

  // where a phone points, clockwise from the camera frame's ahead (its -z), from its orientation as a unit quaternion.
  // The camera's direction and the top edge, both laid flat, added together. Held up the camera leads, flat the top
  // edge leads, and in between both point the same way. Pointing straight up or down with no lean, unknown
  function yawFromQuat(qx, qy, qz, qw) {
    const fx = -2 * (qx * qz + qw * qy), fz = -(1 - 2 * (qx * qx + qy * qy));   // the camera, device -z
    const ux = 2 * (qx * qy - qw * qz), uz = 2 * (qy * qz + qw * qx);            // the top edge, device +y
    const e = fx + ux, n = -fz - uz;
    if (Math.hypot(e, n) < 0.2) return null;
    return wrap360(Math.atan2(e, n) * R2D);
  }
  // the same from a deviceorientation event (degrees), in that sensor's own frame (x east, y north for an absolute one)
  function yawFromEuler(alpha, beta, gamma) {
    if (![alpha, beta, gamma].every(Number.isFinite)) return null;
    const a = alpha * D2R, b = beta * D2R, g = gamma * D2R;
    const cx = -Math.cos(a) * Math.sin(g) - Math.sin(a) * Math.sin(b) * Math.cos(g);
    const cy = -Math.sin(a) * Math.sin(g) + Math.cos(a) * Math.sin(b) * Math.cos(g);
    const tx = -Math.sin(a) * Math.cos(b), ty = Math.cos(a) * Math.cos(b);
    const e = cx + tx, n = cy + ty;
    if (Math.hypot(e, n) < 0.2) return null;
    return wrap360(Math.atan2(e, n) * R2D);
  }

  function create(opts) {
    const o = Object.assign({}, DEFAULTS, opts || {});
    return {
      o,
      link: { d: 0, te: 0, tn: 0 },
      pos: { e: 0, n: 0 },
      r95: o.base_r95_m,
      mode: 'idle',            // camera, steps, or idle until the first source speaks
      camSeen: false,          // the camera has given a pose on this walk
      facing: null,            // where the phone points, clockwise from the map's ahead
      faceSrc: null,           // camera or gyro
      oriOff: null,            // map facing minus the orientation sensor's facing, learnt under the camera
      oriT: null,
      prevOri: null,           // the last orientation reading, its facing, time and up direction in the phone's frame
      seg: null,               // the STEPS stretch now, circle at its start, metres with and without a heading, idle s
      lost: null,              // when the camera went, its time, facing, estimate, camera position, circle, pocket turn
      cam: null,               // the last camera position in the map
      stride: { m: o.stride_m, dist: 0, steps: 0, count: 0, from: null, t: null, learnt: false },
      steps: 0,
      walked_m: 0,
      cam_m: 0,
      steps_m: 0,
      trail: [{ e: 0, n: 0, m: 'start' }],
      trailFrom: { e: 0, n: 0 },
      arrival: { key: null, on: false },
      trial: { t0: null, walked0: 0, cam0: 0, steps0: 0, max_r95: o.base_r95_m },
      events: [],
      reanchors: 0,
    };
  }

  function note(st, t, text) {
    st.events.push({ t, text });
    if (st.events.length > 30) st.events.shift();
  }

  function started(st, t) {
    if (st.trial.t0 == null) st.trial.t0 = t;
  }

  function addTrail(st, m) {
    st.trail.push({ e: st.pos.e, n: st.pos.n, m });
    if (st.trail.length > st.o.trail_max) st.trail.splice(1, st.trail.length - st.o.trail_max);
    st.trailFrom = { e: st.pos.e, n: st.pos.n };
  }

  function segR95(st) {
    const o = st.o, g = st.seg;
    const sigma = g.D * Math.max(o.along_frac, Math.sin(o.cross_deg * D2R));
    return Math.hypot(g.r0, K95 * sigma) + g.free + o.idle_mps * g.idle;
  }

  function setFacing(st, f, src) {
    st.facing = f == null ? null : wrap360(f);
    st.faceSrc = f == null ? null : src;
  }

  // turn and shift the link so the camera pose lands on the estimate, facing the way the phone is believed to face
  function reanchor(st, pose, facing) {
    if (pose.yaw != null && facing != null) st.link.d = wrap360(facing - pose.yaw);
    const m0 = toMap({ d: st.link.d, te: 0, tn: 0 }, pose.x, pose.z);
    st.link.te = st.pos.e - m0.e;
    st.link.tn = st.pos.n - m0.n;
    st.reanchors += 1;
  }

  // the camera found its own map again. Never if it comes back where it got lost after a real walk (it restarted).
  // Yes if it faces the way the gyro says and moved about as far as the steps did, however far apart the two land: a
  // turn made while pocketing points the steps the wrong way, while the camera's map never turned. Otherwise yes only
  // within twice the circle plus 1 m. yaw is the camera's own, clockwise from its -z, or null
  function sameFrame(st, m, yaw) {
    const L = st.lost, o = st.o;
    if (!L || L.reset) return false;
    const off = dist(m, st.pos), stepsMoved = dist(L.est, st.pos), camMoved = dist(L.cam, m);
    if (stepsMoved >= 1 && camMoved < 0.5 * stepsMoved) return false;
    if (yaw != null && st.faceSrc === 'gyro' && st.facing != null &&
        Math.abs(wrap180(yaw + st.link.d - st.facing)) <= o.same_yaw_deg &&
        Math.abs(camMoved - stepsMoved) <= Math.max(o.back_m, 0.5 * stepsMoved)) return true;
    return off <= Math.max(o.back_m, 2 * st.r95 + 1);
  }

  // the camera pose {x, z, yaw} in its own frame, yaw clockwise from its -z or null. null when it has no position
  function camera(st, pose, t) {
    const o = st.o;
    if (!pose) {
      if (st.mode === 'camera') lose(st, t, 'lost');
      return st;
    }
    started(st, t);
    let m = toMap(st.link, pose.x, pose.z);
    if (st.mode !== 'camera') {
      const L = st.lost;
      if (!st.camSeen && st.walked_m === 0) {
        reanchor(st, pose, 0);
        st.reanchors = 0;
        note(st, t, 'Camera tracking');
      } else if (L && sameFrame(st, m, pose.yaw)) {
        const off = dist(m, st.pos);
        st.r95 = clamp(L.r95 + off, L.r95, Math.max(st.r95, L.r95));
        st.pos = { e: m.e, n: m.n };
        addTrail(st, 'jump');   // a correction, not a walk: the trail breaks here and the distance does not count
        note(st, t, `Camera back on its own map, ${off.toFixed(1)} m from the steps`);
      } else {
        const f = st.facing != null ? st.facing : L ? L.facing : null;
        reanchor(st, pose, f);
        note(st, t, 'Camera back, started over from the step estimate');
      }
      m = toMap(st.link, pose.x, pose.z);
      st.camSeen = true;
      st.mode = 'camera';
      st.lost = null;
      st.seg = null;
      st.stride.from = null;
    }
    st.pos = { e: m.e, n: m.n };
    st.cam = { e: m.e, n: m.n, t };
    if (pose.yaw != null) setFacing(st, pose.yaw + st.link.d, 'camera');
    const d = dist(st.trailFrom, st.pos);
    if (d >= o.trail_m) {
      st.walked_m += d;
      st.cam_m += d;
      st.r95 += o.cam_frac * d;
      addTrail(st, 'camera');
    }
    st.trial.max_r95 = Math.max(st.trial.max_r95, st.r95);
    return st;
  }

  // the camera is gone (lost) or its frame jumped (reset). STEPS carries on from here
  function lose(st, t, why) {
    if (st.mode !== 'camera') {
      if (why === 'reset' && st.lost) st.lost.reset = true;
      return st;
    }
    st.mode = 'steps';
    st.lost = {
      t, facing: st.facing, est: { e: st.pos.e, n: st.pos.n },
      cam: st.cam ? { e: st.cam.e, n: st.cam.n } : { e: st.pos.e, n: st.pos.n },
      r95: st.r95, pocket: 0, reset: why === 'reset',
    };
    st.seg = { r0: st.r95, D: 0, free: 0, idle: 0 };
    st.stride.from = null;
    note(st, t, why === 'reset' ? 'Camera frame jumped, counting steps' : 'Camera lost, counting steps');
    return st;
  }

  // steps only, no camera on this phone. The phone in the hand points the way, ahead is where it points now
  function beginSteps(st, t) {
    if (st.mode !== 'idle') return st;
    started(st, t);
    st.mode = 'steps';
    st.seg = { r0: st.r95, D: 0, free: 0, idle: 0 };
    note(st, t, 'Steps only');
    return st;
  }

  // the up direction in the phone's own frame, from a deviceorientation event (degrees)
  function upFromEuler(beta, gamma) {
    if (!Number.isFinite(beta) || !Number.isFinite(gamma)) return null;
    const b = beta * D2R, g = gamma * D2R;
    return [-Math.cos(b) * Math.sin(g), Math.sin(b), Math.cos(b) * Math.cos(g)];
  }
  function tiltRate(a, b, dt) {
    if (!a || !b || !(dt > 0) || dt > 0.5) return 0;
    const dot = clamp(a[0] * b[0] + a[1] * b[1] + a[2] * b[2], -1, 1);
    return Math.acos(dot) * R2D / dt;
  }

  // the orientation sensor's facing f, in its own frame (see yawFromEuler), and the phone's up (upFromEuler) if known
  function orient(st, f, t, up) {
    if (f == null || !Number.isFinite(f)) return st;
    const P = st.prevOri;
    st.prevOri = { f, t, up: up || null };
    st.oriT = t;
    if (st.mode === 'camera') {
      if (st.faceSrc === 'camera' && st.facing != null) {
        const want = wrap360(st.facing - f);
        st.oriOff = st.oriOff == null ? want : wrap360(st.oriOff + st.o.ori_follow * wrap180(want - st.oriOff));
      }
      return st;
    }
    if (st.oriOff == null && !st.camSeen) st.oriOff = wrap360(-f);   // steps only, ahead is where the phone points first
    if (st.oriOff != null) setFacing(st, f + st.oriOff, 'gyro');
    // going into a pocket: in the first seconds after the loss, a turn made while the phone tilts fast is the pocket
    const L = st.lost;
    if (L && P && t - L.t <= st.o.settle_s && tiltRate(P.up, up, t - P.t) > st.o.pocket_tilt_dps) L.pocket = wrap180(L.pocket + wrap180(f - P.f));
    return st;
  }

  // the orientation stream changed (relative to absolute): keep the facing, find the new offset
  function reorient(st, f) {
    if (f == null || st.facing == null) { st.oriOff = null; return st; }
    st.oriOff = wrap360(st.facing - f);
    return st;
  }

  // the direction of the walk on steps: where the phone faces, less the turn it made going into the pocket
  function walkHeading(st) {
    const L = st.lost;
    if (!L) return st.facing;   // steps only, the phone in the hand points the way
    if (st.facing == null || st.faceSrc !== 'gyro') return L.facing;
    return wrap360(st.facing - L.pocket);
  }

  // n steps from the detector at time t (s)
  function step(st, n, t) {
    if (!(n > 0)) return st;
    const o = st.o, s = st.stride;
    st.steps += n;
    if (st.mode === 'camera' && st.cam) {
      // the camera measures the stride, its move between two steps, over about the last 30 steps
      if (s.from && s.t != null && t - s.t <= o.step_gap_s) {
        const d = dist(s.from, st.cam), k = Math.exp(-n / o.stride_memory);
        s.dist = s.dist * k + d;
        s.steps = s.steps * k + n;
        s.count += n;
        if (s.count >= o.stride_min_steps && s.steps > 0) { s.m = clamp(s.dist / s.steps, o.stride_lo, o.stride_hi); s.learnt = true; }
      }
      s.from = { e: st.cam.e, n: st.cam.n };
      s.t = t;
      return st;
    }
    if (st.mode !== 'steps' || !st.seg) return st;
    started(st, t);
    const h = walkHeading(st, t), L = n * s.m;
    if (h != null) {
      st.pos = { e: st.pos.e + L * Math.sin(h * D2R), n: st.pos.n + L * Math.cos(h * D2R) };
      st.seg.D += L;
    } else st.seg.free += L;   // no direction, the walk could have gone any way
    st.walked_m += L;
    st.steps_m += L;
    st.r95 = segR95(st);
    st.trial.max_r95 = Math.max(st.trial.max_r95, st.r95);
    addTrail(st, 'steps');
    return st;
  }

  // STEPS with the motion sensor silent for this long
  function idle(st, seconds) {
    if (st.mode !== 'steps' || !st.seg || !(seconds > 0)) return st;
    st.seg.idle += seconds;
    st.r95 = segR95(st);
    st.trial.max_r95 = Math.max(st.trial.max_r95, st.r95);
    return st;
  }

  // standing on the tape X. Returns the trial and starts the next one from the start
  function onTheX(st, t, extra) {
    const err = Math.hypot(st.pos.e, st.pos.n), tr = st.trial;
    const walked = st.walked_m - tr.walked0, steps_m = st.steps_m - tr.steps0;
    const rec = Object.assign({
      err_m: err, walked_m: walked, steps_share: walked > 0 ? Math.min(1, steps_m / walked) : 0,
      r95_m: st.r95, max_r95_m: tr.max_r95, secs: tr.t0 == null ? 0 : t - tr.t0, mode: st.mode,
      stride_m: st.stride.m, stride_learnt: st.stride.learnt, at: null,
    }, extra || {});
    const c = { e: -st.pos.e, n: -st.pos.n };
    st.link.te += c.e;
    st.link.tn += c.n;
    for (const p of [st.cam, st.stride.from, st.lost && st.lost.est, st.lost && st.lost.cam]) if (p) { p.e += c.e; p.n += c.n; }
    st.pos = { e: 0, n: 0 };
    st.r95 = st.o.base_r95_m;
    if (st.seg) st.seg = { r0: st.r95, D: 0, free: 0, idle: 0 };
    if (st.lost) st.lost.r95 = st.r95;
    addTrail(st, 'x');
    st.trial = { t0: t, walked0: st.walked_m, cam0: st.cam_m, steps0: st.steps_m, max_r95: st.r95 };
    st.arrival = { key: null, on: false };
    note(st, t, `On the X, ${err.toFixed(2)} m off after ${walked.toFixed(1)} m`);
    return rec;
  }

  function turnSay(turn) {
    if (turn == null) return 'Follow the map';
    const a = Math.abs(turn);
    if (a <= 12) return 'Straight ahead';
    if (a >= 150) return 'Turn around';
    return `Turn ${turn < 0 ? 'left' : 'right'} ${Math.round(a)}°`;
  }

  // the way to a target {e, n}. key names the target, so arriving buzzes once per target
  function guide(st, target, key) {
    const o = st.o, d = dist(st.pos, target), brg = bearing(st.pos, target);
    const turn = st.facing == null ? null : wrap180(brg - st.facing);
    const thr = st.mode === 'camera' ? o.arrive_cam_m : o.arrive_steps_m;
    const A = st.arrival;
    if (A.key !== key) { A.key = key; A.on = false; }
    let buzz = false;
    if (!A.on && d <= thr) { A.on = true; buzz = true; }
    else if (A.on && d > thr + o.arrive_hyst_m) A.on = false;
    return { dist_m: d, bearing_deg: brg, turn_deg: turn, say: A.on ? 'Arrived' : turnSay(turn), arrived: A.on, buzz, thr_m: thr };
  }

  const median = a => {
    const s = [...a].sort((x, y) => x - y), k = s.length;
    return k ? (k % 2 ? s[(k - 1) / 2] : (s[k / 2 - 1] + s[k / 2]) / 2) : null;
  };
  function source(r) {
    if (r.steps_share >= 0.95) return 'steps';
    if (r.steps_share <= 0.05) return 'camera';
    return `mixed, ${Math.round(r.steps_share * 100)}% steps`;
  }
  function trialStats(trials) {
    const t = (trials || []).filter(x => Number.isFinite(x.err_m));
    if (!t.length) return null;
    const pct = t.filter(x => x.walked_m > 0).map(x => 100 * x.err_m / x.walked_m);
    return {
      n: t.length, median_m: median(t.map(x => x.err_m)), max_m: Math.max(...t.map(x => x.err_m)),
      median_pct: pct.length ? median(pct) : null, walked_m: t.reduce((s, x) => s + x.walked_m, 0),
    };
  }
  function trialMarkdown(trials) {
    const t = (trials || []).filter(x => Number.isFinite(x.err_m));
    const rows = t.map((r, k) => `| ${k + 1} | ${r.walked_m.toFixed(1)} m | ${r.err_m.toFixed(2)} m | ${r.walked_m > 0 ? (100 * r.err_m / r.walked_m).toFixed(1) + '%' : 'n/a'} | ${source(r)} | ${r.r95_m.toFixed(2)} m | ${Math.round(r.secs)} s |${r.sim ? ' simulated |' : ' |'}`);
    const s = trialStats(t);
    const head = '| # | Walked | Off at the X | Of the walk | Source | 95% circle | Time | Note |\n| --- | --- | --- | --- | --- | --- | --- | --- |';
    const foot = s ? `\n\nMedian ${s.median_m.toFixed(2)} m off, worst ${s.max_m.toFixed(2)} m, over ${s.n} loops and ${s.walked_m.toFixed(0)} m walked${s.median_pct == null ? '' : `, median ${s.median_pct.toFixed(1)}% of the walk`}.` : '';
    return `${head}\n${rows.join('\n')}${foot}`;
  }

  return Object.freeze({
    DEFAULTS, K95, create, camera, lose, beginSteps, orient, reorient, step, idle, onTheX, guide, walkHeading,
    toMap, toCam, yawFromQuat, yawFromEuler, upFromEuler, trialStats, trialMarkdown, source, turnSay, bearing, dist, wrap180, wrap360,
  });
});
