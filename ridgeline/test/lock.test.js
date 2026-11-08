'use strict';
// Lock input guard on the phone: node --test ridgeline/test/lock.test.js
// doLock lives in the inline script of phone/index.html. The test cuts it out of the page as written and runs it
// with stand-ins for the sensor, the solve and the screen, so it checks the code the phone really runs.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const plain = v => JSON.parse(JSON.stringify(v));   // values made inside the vm come from another realm

const html = fs.readFileSync(path.join(__dirname, '..', 'phone', 'index.html'), 'utf8');
const from = html.indexOf('let lockBusy = false;');
const to = html.indexOf('function skipTarget()');
assert.ok(from > 0 && to > from, 'doLock and its guard were not found in phone/index.html');
const lockSrc = html.slice(from, to);

// A small phone: three cards, a steady keyboard aim, a solve that moves to the next card or names a suspect.
function phone({ solve } = {}) {
  const ctx = vm.createContext({ Date, Math, console });
  vm.runInContext(`
    let locks = [], skip = 3, reduce = false, source = 'keyboard', orientMode = 'relative';
    let target = 'pk_8', result = null, aim = { h: 254.2, t: 1.5, steady: true, fresh: true, spread: 0 };
    let solves = 0, renders = 0, posts = 0, vibrations = 0, timers = [], solveImpl = null;
    const navigator = { vibrate: () => { vibrations++; return true; } };
    const setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
    const meanVec = () => aim;
    function solveNow() { solves++; solveImpl(); }
    function render() { renders++; }
    function post() { posts++; }
    ${lockSrc}
    globalThis.api = {
      tap: () => doLock(),
      flush() { const t = timers; timers = []; for (const x of t) x.fn(); return t.map(x => x.ms); },
      get locks() { return locks; }, get target() { return target; }, get result() { return result; },
      set result(v) { result = v; }, set target(v) { target = v; }, set aim(v) { aim = v; }, set solveImpl(f) { solveImpl = f; },
      get counts() { return { solves, renders, posts, vibrations, pending: timers.length }; },
      get busy() { return lockBusy; },
    };
  `, ctx);
  const api = ctx.api;
  const order = ['pk_8', 'pk_20', 'pk_13'];
  api.solveImpl = solve ? () => solve(api) : () => {
    const done = api.locks.map(l => l.peak_id);
    const next = order.find(id => !done.includes(id)) || null;
    api.result = { status: next ? 'need_more' : 'enough' };
    api.target = next;
  };
  return api;
}

test('taps during the solve after an accepted Lock record one Lock only', () => {
  const p = phone();
  p.tap(); p.tap(); p.tap();
  assert.strictEqual(p.locks.length, 1);
  assert.strictEqual(p.locks[0].peak_id, 'pk_8');
  assert.deepStrictEqual(plain(p.counts), { solves: 0, renders: 0, posts: 0, vibrations: 1, pending: 1 });
  assert.strictEqual(p.busy, true);
  assert.deepStrictEqual(plain(p.flush()), [120]);
  assert.deepStrictEqual(plain(p.counts), { solves: 1, renders: 1, posts: 1, vibrations: 1, pending: 0 });
  assert.strictEqual(p.busy, false);
  assert.strictEqual(p.target, 'pk_20');
});

test('the Lock control works again once the solve and screen update are done', () => {
  const p = phone();
  p.tap(); p.tap(); p.flush();
  p.aim = { h: 230.0, t: 1.2, steady: true, fresh: true, spread: 0 };
  p.tap(); p.tap(); p.flush();
  p.aim = { h: 310.3, t: 2.0, steady: true, fresh: true, spread: 0 };
  p.tap(); p.tap(); p.tap(); p.flush();
  assert.deepStrictEqual(plain(p.locks.map(l => [l.peak_id, l.heading_deg])), [['pk_8', 254.2], ['pk_20', 230], ['pk_13', 310.3]]);
  assert.strictEqual(p.result.status, 'enough');
  assert.strictEqual(p.counts.solves, 3);
});

test('a real bad aim still replaces the suspect Lock, and a double tap does not remove a second Lock', () => {
  const p = phone();
  p.tap(); p.flush();
  p.aim = { h: 230.0, t: 1.2, steady: true, fresh: true, spread: 0 };
  p.tap(); p.flush();
  // the solver flags Lock 2 and asks for pk_20 again
  p.result = { status: 'mismatch', suspect: { lock_index: 1, peak_id: 'pk_20', was_peak_id: 'pk_20' } };
  p.target = 'pk_20';
  p.aim = { h: 231.5, t: 1.2, steady: true, fresh: true, spread: 0 };
  p.tap(); p.tap(); p.tap();
  assert.deepStrictEqual(plain(p.locks.map(l => [l.peak_id, l.heading_deg])), [['pk_8', 254.2], ['pk_20', 231.5]]);
  p.flush();
  assert.deepStrictEqual(plain(p.locks.map(l => [l.peak_id, l.heading_deg])), [['pk_8', 254.2], ['pk_20', 231.5]]);
  assert.strictEqual(p.target, 'pk_13');
});

test('a Lock that is not accepted does not hold the control', () => {
  const p = phone();
  p.aim = { flat: true };
  p.tap();
  assert.strictEqual(p.busy, false);
  assert.strictEqual(p.locks.length, 0);
  p.aim = { h: 254.2, t: 1.5, steady: true, fresh: true, spread: 0 };
  p.tap();
  assert.strictEqual(p.locks.length, 1);
});

test('the control is released even if the solve throws', () => {
  const p = phone({ solve: () => { throw new Error('solve failed'); } });
  p.tap();
  assert.throws(() => p.flush(), /solve failed/);
  assert.strictEqual(p.busy, false);
});
