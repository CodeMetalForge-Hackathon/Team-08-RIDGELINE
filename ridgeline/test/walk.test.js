'use strict';
// Walk here, the camera, the steps and the switch between them: node --test ridgeline/test/*.test.js
const test = require('node:test');
const assert = require('node:assert');
const W = require('../walk/walk_core.js');

const near = (a, b, tol, msg) => assert.ok(Math.abs(a - b) <= tol, `${msg || ''} ${a} is not within ${tol} of ${b}`);
// a quaternion for a turn of h degrees to the right about the camera frame's up axis
const qYaw = h => { const r = -h * Math.PI / 360; return [0, Math.sin(r), 0, Math.cos(r)]; };
// a camera pose at map-like (e, n) in a frame that is the map itself
const pose = (e, n, yaw) => ({ x: e, z: -n, yaw });

test('yaw from a quaternion: ahead, right, and a phone flat with its top ahead', () => {
  near(W.yawFromQuat(0, 0, 0, 1), 0, 1e-9);
  near(W.yawFromQuat(...qYaw(90)), 90, 1e-6);
  near(W.yawFromQuat(...qYaw(-30)), 330, 1e-6);
  const s = Math.SQRT1_2;
  near(W.yawFromQuat(-s, 0, 0, s), 0, 1e-6, 'flat, screen up, top ahead');
  near(W.yawFromQuat(s, 0, 0, s), 180, 1e-6, 'camera straight up, top edge back');
});

test('yaw from deviceorientation matches the phone app: flat north, held up north, held up east', () => {
  near(W.yawFromEuler(0, 0, 0), 0, 1e-9);
  near(W.yawFromEuler(0, 90, 0), 0, 1e-9);
  near(W.yawFromEuler(270, 90, 0), 90, 1e-6);
  assert.strictEqual(W.yawFromEuler(0, null, 0), null);
});

test('the link goes both ways', () => {
  const link = { d: 37, te: 2.5, tn: -1.25 };
  const m = W.toMap(link, 3, -4), c = W.toCam(link, m.e, m.n);
  near(c.x, 3, 1e-9); near(c.z, -4, 1e-9);
});

test('the first camera pose is the start, facing ahead, whatever the camera frame', () => {
  const st = W.create();
  W.camera(st, { x: 5, z: 7, yaw: 120 }, 0);
  near(st.pos.e, 0, 1e-9); near(st.pos.n, 0, 1e-9);
  near(st.facing, 0, 1e-9);
  // the camera walks 3 m in its own direction 120 deg, which is ahead on the map
  const r = 120 * Math.PI / 180;
  W.camera(st, { x: 5 + 3 * Math.sin(r), z: 7 - 3 * Math.cos(r), yaw: 120 }, 1);
  near(st.pos.e, 0, 1e-9); near(st.pos.n, 3, 1e-9);
  assert.strictEqual(st.mode, 'camera');
  near(st.walked_m, 3, 1e-9);
  near(st.r95, 0.25 + 0.02 * 3, 1e-9);
});

test('hand shake under 25 cm never counts as walking', () => {
  const st = W.create();
  W.camera(st, pose(0, 0, 0), 0);
  for (let k = 0; k < 600; k++) W.camera(st, pose(0.1 * Math.sin(k), 0.1 * Math.cos(k * 1.3), 0), k / 60);
  assert.strictEqual(st.walked_m, 0);
});

test('the camera measures the stride between steps', () => {
  const st = W.create();
  W.camera(st, pose(0, 0, 0), 0);
  for (let k = 1; k <= 20; k++) { W.camera(st, pose(0, 0.62 * k, 0), k * 0.5); W.step(st, 1, k * 0.5); }
  assert.ok(st.stride.learnt);
  near(st.stride.m, 0.62, 1e-6);
});

test('a pause between steps is not a stride', () => {
  const st = W.create();
  W.camera(st, pose(0, 0, 0), 0);
  for (let k = 1; k <= 10; k++) { W.camera(st, pose(0, 0.6 * k, 0), k * 0.5); W.step(st, 1, k * 0.5); }
  W.camera(st, pose(0, 15, 0), 20);   // carried 9 m with no steps, then a step after 15 s
  W.step(st, 1, 20);
  near(st.stride.m, 0.6, 1e-6);
});

test('camera lost: steps carry on in the direction the phone faced, and the circle grows as Auto-PACE tier C', () => {
  const st = W.create();
  W.camera(st, pose(0, 0, 0), 0);
  for (let k = 1; k <= 12; k++) { W.camera(st, pose(0, 0.7 * k, 0), k * 0.5); W.step(st, 1, k * 0.5); }
  W.camera(st, pose(0, 0.7 * 12, 90), 6.1);   // turned to face right, then the camera goes
  W.camera(st, null, 6.2);
  assert.strictEqual(st.mode, 'steps');
  const e0 = st.pos.e, r0 = st.r95;
  for (let k = 1; k <= 10; k++) W.step(st, 1, 6.2 + k * 0.5);
  near(st.pos.e - e0, 7, 1e-6, 'ten steps of 0.7 m to the right');
  const want = Math.hypot(r0, W.K95 * 7 * Math.sin(6 * Math.PI / 180));
  near(st.r95, want, 1e-9);
});

test('up from deviceorientation: flat is +z, held up is +y', () => {
  const flat = W.upFromEuler(0, 0), held = W.upFromEuler(90, 0);
  near(flat[2], 1, 1e-9); near(held[1], 1, 1e-9);
});

test('a phone going into a pocket: a turn while it tilts fast is the pocket, a turn with the tilt steady is the walker', () => {
  const st = W.create(), inHand = [0, 0.8, 0.6], inPocket = [0, -1, 0];
  W.camera(st, pose(0, 0, 0), 0);
  W.orient(st, 200, 0.01, inHand);   // the gyro's own frame is 200 deg off the map
  for (let k = 1; k <= 30; k++) { W.camera(st, pose(0, 0.02 * k, 0), k / 30); W.orient(st, 200, k / 30 + 0.001, inHand); }
  near(st.oriOff, 160, 1e-6);
  W.camera(st, null, 2);
  W.orient(st, 200, 2, inHand);   // the sensor keeps reporting, 60 times a second on a phone
  // into the pocket over 0.5 s, upside down: the phone turns 180 deg while it tilts 143 deg
  for (let k = 1; k <= 10; k++) {
    const u = k / 10, up = [0, inHand[1] + (inPocket[1] - inHand[1]) * u, inHand[2] * (1 - u)], m = Math.hypot(up[1], up[2]);
    W.orient(st, 200 + 18 * k, 2 + 0.05 * k, [0, up[1] / m, up[2] / m]);
  }
  near(W.wrap360(st.lost.pocket), 180, 1e-6);
  near(W.walkHeading(st), 0, 1e-6, 'the walk keeps the direction faced at the loss');
  W.step(st, 1, 2.7);
  near(st.pos.n - 0.6, 0.7, 1e-6);
  // the walker turns right 90 deg, the phone steady in the pocket
  for (let k = 1; k <= 10; k++) W.orient(st, 380 + 9 * k, 3 + 0.1 * k, inPocket);
  near(W.wrap360(st.lost.pocket), 180, 1e-6);
  near(W.walkHeading(st), 90, 1e-6);
});

test('a turn right after the camera goes, with the phone still in the hand, is the walker turning', () => {
  const st = W.create(), inHand = [0, 0.8, 0.6];
  W.camera(st, pose(0, 0, 0), 0);
  for (let k = 0; k <= 30; k++) { W.camera(st, pose(0, 0.02 * k, 0), k / 30); W.orient(st, 0, k / 30 + 0.001, inHand); }
  W.camera(st, null, 1.1);
  for (let k = 1; k <= 18; k++) W.orient(st, 10 * k, 1.1 + 0.05 * k, inHand);   // turns around in 0.9 s
  near(W.walkHeading(st), 180, 1e-6);
});

test('camera back on its own map: snaps to it, and the circle shrinks only to what the disagreement allows', () => {
  const st = W.create();
  W.camera(st, pose(0, 0, 0), 0);
  W.camera(st, pose(0, 2, 0), 1);
  W.orient(st, 0, 1.01);
  W.camera(st, null, 2);
  for (let k = 1; k <= 4; k++) W.step(st, 1, 2 + k * 0.5);   // 2.8 m ahead on steps
  const rSteps = st.r95;
  W.camera(st, pose(0.2, 4.6, 0), 5);   // the camera saw the walk too, 0.2 m apart from the steps
  assert.strictEqual(st.mode, 'camera');
  near(st.pos.e, 0.2, 1e-9); near(st.pos.n, 4.6, 1e-9);
  assert.ok(st.r95 < rSteps && st.r95 > 0.25, `circle ${st.r95}`);
  assert.match(st.events[st.events.length - 1].text, /own map/);
});

test('camera back after starting over: the map carries on from the steps, with no jump', () => {
  const st = W.create();
  W.camera(st, pose(0, 0, 0), 0);
  W.camera(st, pose(0, 3, 0), 1);
  W.orient(st, 0, 1.01);
  W.camera(st, null, 2);
  for (let k = 1; k <= 6; k++) W.step(st, 1, 2 + k * 0.5);   // 4.2 m ahead on steps
  const est = { ...st.pos }, rSteps = st.r95;
  // ARCore restarts its frame: it says the phone is back at 0, 0 and faces 30 deg in its new frame
  W.camera(st, { x: 0, z: 0, yaw: 30 }, 6);
  near(st.pos.e, est.e, 1e-9); near(st.pos.n, est.n, 1e-9);
  near(st.facing, 0, 1e-9, 'the facing stays what the gyro said');
  near(st.r95, rSteps, 1e-9, 'no anchor, no shrink');
  // and walking 1 m along the new frame's 30 deg is ahead on the map
  const r = 30 * Math.PI / 180;
  W.camera(st, { x: Math.sin(r), z: -Math.cos(r), yaw: 30 }, 7);
  near(st.pos.e, est.e, 1e-9); near(st.pos.n, est.n + 1, 1e-9);
});

test('camera back on its own map when the steps were off past the circle: the camera wins', () => {
  const st = W.create();
  W.camera(st, pose(0, 0, 0), 0);
  W.camera(st, pose(0, 3, 0), 1);
  W.orient(st, 0, 1.01);
  W.camera(st, null, 2);
  for (let k = 1; k <= 8; k++) W.step(st, 1, 2 + k * 0.5);   // 5.6 m ahead on steps, circle about 1.5 m
  const rSteps = st.r95;
  W.camera(st, pose(2.2, 8.0, 0), 7);   // the walk really drifted right, and the camera saw it
  near(st.pos.e, 2.2, 1e-9); near(st.pos.n, 8.0, 1e-9);
  near(st.r95, rSteps, 1e-9, 'they disagree by more than the circle, so it does not shrink');
});

test('camera back where it got lost after a real walk: it restarted, the steps carry the walk', () => {
  const st = W.create();
  W.camera(st, pose(0, 0, 0), 0);
  W.camera(st, pose(0, 3, 0), 1);
  W.orient(st, 0, 1.01);
  W.camera(st, null, 2);
  for (let k = 1; k <= 4; k++) W.step(st, 1, 2 + k * 0.5);
  const est = { ...st.pos };
  W.camera(st, pose(0.1, 3.1, 0), 5);   // inside twice the circle, but it never saw the 2.8 m walked
  near(st.pos.e, est.e, 1e-9); near(st.pos.n, est.n, 1e-9);
  assert.match(st.events[st.events.length - 1].text, /started over/);
});

test('a turn while pocketing points the steps the wrong way, and the camera back facing the way the gyro says wins', () => {
  const st = W.create(), inHand = [0, 0.8, 0.6], inPocket = [0, -1, 0];
  W.camera(st, pose(0, 0, 0), 0);
  for (let k = 0; k <= 30; k++) { W.camera(st, pose(0, 0.1 * k, 0), k / 30); W.orient(st, 0, k / 30 + 0.001, inHand); }
  W.camera(st, null, 1.1);
  W.orient(st, 0, 1.1, inHand);
  // into the pocket over 0.5 s while the walker turns 60 deg left: the phone turns 180 - 60 = 120 deg, all read as the pocket
  for (let k = 1; k <= 10; k++) {
    const u = k / 10, up = [0, 0.8 - 1.8 * u, 0.6 * (1 - u)], m = Math.hypot(up[1], up[2]);
    W.orient(st, 12 * k, 1.1 + 0.05 * k, [0, up[1] / m, up[2] / m]);
  }
  near(W.wrap360(st.lost.pocket), 120, 1e-6);
  for (let k = 1; k <= 8; k++) { W.orient(st, 120, 1.6 + 0.5 * k, inPocket); W.step(st, 1, 1.6 + 0.5 * k); }
  const truth = { e: -5.6 * Math.sin(Math.PI / 3), n: 3 + 5.6 * Math.cos(Math.PI / 3) };   // 5.6 m at 300 deg
  const off = W.dist(st.pos, truth);
  assert.ok(off > 2 * st.r95 + 1, `the steps are ${off} m off, past twice the circle plus 1 m`);
  W.orient(st, 300, 5.8, inHand);   // out of the pocket, pointing the way the walker goes
  W.camera(st, pose(truth.e, truth.n, 300), 5.9);
  near(st.pos.e, truth.e, 1e-9); near(st.pos.n, truth.n, 1e-9);
  assert.match(st.events[st.events.length - 1].text, /own map/);
});

test('camera back far off and facing another way than the gyro says: it started over', () => {
  const st = W.create(), inHand = [0, 0.8, 0.6];
  W.camera(st, pose(0, 0, 0), 0);
  for (let k = 0; k <= 30; k++) { W.camera(st, pose(0, 0.1 * k, 0), k / 30); W.orient(st, 0, k / 30 + 0.001, inHand); }
  W.camera(st, null, 1.1);
  for (let k = 1; k <= 8; k++) { W.orient(st, 0, 1.1 + 0.5 * k, inHand); W.step(st, 1, 1.1 + 0.5 * k); }
  const est = { ...st.pos };
  W.camera(st, { x: 4, z: -3, yaw: 90 }, 5.3);   // 6.9 m from the estimate, its ahead 90 deg off the gyro's
  near(st.pos.e, est.e, 1e-9); near(st.pos.n, est.n, 1e-9);
  assert.match(st.events[st.events.length - 1].text, /started over/);
});

test('a frame reset while tracking re-anchors on the next pose', () => {
  const st = W.create();
  W.camera(st, pose(0, 0, 0), 0);
  W.camera(st, pose(0, 2, 0), 1);
  W.lose(st, 1.1, 'reset');
  W.camera(st, pose(10, 10, 0), 1.2);   // the jumped frame
  near(st.pos.e, 0, 1e-9); near(st.pos.n, 2, 1e-9);
});

test('steps only: ahead is where the phone points at the start, and no heading spreads the circle', () => {
  const st = W.create();
  W.beginSteps(st, 0);
  W.step(st, 1, 0.5);   // no orientation yet
  near(st.pos.n, 0, 1e-9);
  near(st.r95, 0.25 + 0.7, 1e-9);
  W.orient(st, 75, 1);
  near(st.facing, 0, 1e-9);
  W.step(st, 1, 1.5);
  near(st.pos.n, 0.7, 1e-9);
});

test('on the X: the error is the distance from the start, and the next trial starts clean', () => {
  const st = W.create();
  W.camera(st, pose(0, 0, 0), 0);
  for (let k = 1; k <= 20; k++) W.camera(st, pose(0, 0.5 * k, 0), k * 0.5);
  for (let k = 1; k <= 20; k++) W.camera(st, pose(0.03 * k, 10 - 0.5 * k, 180), 10 + k * 0.5);
  const rec = W.onTheX(st, 20);
  near(rec.err_m, 0.6, 1e-9);
  near(rec.walked_m, st.walked_m, 1e-9);
  assert.strictEqual(rec.steps_share, 0);
  near(st.pos.e, 0, 1e-9);
  near(st.r95, 0.25, 1e-9);
  W.camera(st, pose(0.6, 0.5, 180), 21);   // the camera keeps its frame, the link carries the correction
  near(st.pos.e, 0, 1e-9); near(st.pos.n, 0.5, 1e-9);
  const rec2 = W.onTheX(st, 22);
  near(rec2.walked_m, 0.5, 1e-9);
});

test('arrival buzzes once per target, under the camera within 0.6 m, and lets go only 0.8 m farther out', () => {
  const st = W.create();
  W.camera(st, pose(0, 0, 0), 0);
  const tgt = { e: 0, n: 5 };
  let g = W.guide(st, tgt, 'p1');
  assert.strictEqual(g.say, 'Straight ahead');
  near(g.dist_m, 5, 1e-9);
  W.camera(st, pose(0, 4.5, 0), 1);
  g = W.guide(st, tgt, 'p1');
  assert.ok(g.arrived && g.buzz);
  assert.ok(!W.guide(st, tgt, 'p1').buzz, 'once');
  W.camera(st, pose(0, 3.9, 0), 2);
  assert.ok(W.guide(st, tgt, 'p1').arrived, 'still within 1.4 m');
  W.camera(st, pose(0, 3.5, 0), 3);
  assert.ok(!W.guide(st, tgt, 'p1').arrived);
  W.camera(st, pose(0, 3.5, 90), 4);
  assert.strictEqual(W.guide(st, tgt, 'p1').say, 'Turn left 90°');
});

test('trial table and its numbers', () => {
  const trials = [
    { err_m: 0.3, walked_m: 20, steps_share: 0, r95_m: 0.65, secs: 40 },
    { err_m: 0.5, walked_m: 25, steps_share: 0.4, r95_m: 2.1, secs: 52 },
    { err_m: 2.4, walked_m: 20, steps_share: 1, r95_m: 5.2, secs: 45, sim: true },
  ];
  const s = W.trialStats(trials);
  assert.strictEqual(s.n, 3);
  near(s.median_m, 0.5, 1e-9);
  near(s.max_m, 2.4, 1e-9);
  near(s.median_pct, 2, 1e-9);
  const md = W.trialMarkdown(trials);
  assert.match(md, /\| 2 \| 25\.0 m \| 0\.50 m \| 2\.0% \| mixed, 40% steps \|/);
  assert.match(md, /simulated/);
  assert.match(md, /Median 0\.50 m off, worst 2\.40 m, over 3 loops/);
  assert.strictEqual(W.trialStats([]), null);
});
