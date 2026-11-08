// End-to-end check of the live demo: tools/live_server.py, the phone page and the judge viewer, in headless desktop Chrome.
// No dependencies: Chrome is driven over the DevTools protocol with Node's built-in WebSocket (Node 22 or newer).
//
//   node tests/live_e2e.mjs                 (CHROME=<path to chrome.exe> if it is not in the usual place)
//   PORT=8770 OUT=.local/e2e node tests/live_e2e.mjs   (default: free ports picked at run time)
//
// The phone is driven only with key presses (J, Enter, the arrow keys), steering by the phone's own "Turn right 12°"
// text, the way a person would aim. Lock 3 is first aimed at the wrong peak on purpose, to see the re-aim message.
// For every lock it measures how long the viewer takes to show it. Writes OUT/result.json and screenshots.
import {spawn, spawnSync} from 'node:child_process';
import {mkdirSync, writeFileSync, existsSync, mkdtempSync, rmSync} from 'node:fs';
import {createServer} from 'node:net';
import {tmpdir} from 'node:os';
import {join, dirname} from 'node:path';
import {fileURLToPath} from 'node:url';

// a port nobody listens on right now. Another copy of the server on a fixed port once answered this test with old files.
const freePort = () => new Promise((ok, no) => { const s = createServer(); s.on('error', no); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => ok(p)); }); });
const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const PORT = +(process.env.PORT || await freePort()), DPORT = +(process.env.DEVTOOLS_PORT || 0);
const OUT = process.env.OUT || join(ROOT, '.local', 'e2e');
const BASE = `http://127.0.0.1:${PORT}`;
const CHROME = process.env.CHROME || ['C:/Program Files/Google/Chrome/Application/chrome.exe', 'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  '/usr/bin/google-chrome', '/usr/bin/chromium', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'].find(p => existsSync(p));
const sleep = ms => new Promise(r => setTimeout(r, ms));
const log = (...a) => console.log(...a);
mkdirSync(OUT, {recursive: true});

class Page {
  constructor(ws, name) {
    this.ws = ws; this.name = name; this.id = 0; this.cb = new Map(); this.errors = [];
    ws.onmessage = m => {
      const d = JSON.parse(m.data);
      if (d.id && this.cb.has(d.id)) { const {ok, no} = this.cb.get(d.id); this.cb.delete(d.id); d.error ? no(new Error(d.error.message)) : ok(d.result); return; }
      if (d.method === 'Runtime.exceptionThrown') this.errors.push(d.params.exceptionDetails.exception?.description || d.params.exceptionDetails.text);
      if (d.method === 'Runtime.consoleAPICalled' && d.params.type === 'error') this.errors.push(d.params.args.map(a => a.value ?? a.description).join(' '));
    };
  }
  static async open(url, name) { const ws = new WebSocket(url); await new Promise((ok, no) => { ws.onopen = ok; ws.onerror = no; }); return new Page(ws, name); }
  send(method, params = {}) { const id = ++this.id; this.ws.send(JSON.stringify({id, method, params})); return new Promise((ok, no) => this.cb.set(id, {ok, no})); }
  async eval(expr) {
    const r = await this.send('Runtime.evaluate', {expression: expr, awaitPromise: true, returnByValue: true});
    if (r.exceptionDetails) throw new Error(`${this.name}: ${r.exceptionDetails.exception?.description || r.exceptionDetails.text}`);
    return r.result.value;
  }
  async until(expr, ms = 20000, step = 50) {
    const t0 = Date.now();
    for (;;) { const v = await this.eval(expr).catch(() => null); if (v) return v; if (Date.now() - t0 > ms) throw new Error(`${this.name}: timed out waiting for ${expr}`); await sleep(step); }
  }
  async key(key, shift = false) {
    const K = {Enter: [13, 'Enter', '\r'], ArrowLeft: [37, 'ArrowLeft'], ArrowUp: [38, 'ArrowUp'], ArrowRight: [39, 'ArrowRight'], ArrowDown: [40, 'ArrowDown'], Backspace: [8, 'Backspace']};
    const [vk, code, text] = K[key] || [key.toUpperCase().charCodeAt(0), 'Key' + key.toUpperCase(), key];
    const base = {key, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, modifiers: shift ? 8 : 0};
    await this.send('Input.dispatchKeyEvent', {type: 'keyDown', ...base, ...(text ? {text} : {})});
    await this.send('Input.dispatchKeyEvent', {type: 'keyUp', ...base});
  }
  async keyHold(key, ms) {   // a long press on a key
    const K = {Enter: [13, 'Enter', '\r']}, [vk, code, text] = K[key];
    const base = {key, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, modifiers: 0};
    await this.send('Input.dispatchKeyEvent', {type: 'keyDown', ...base, text});
    await sleep(ms);
    await this.send('Input.dispatchKeyEvent', {type: 'keyUp', ...base});
  }
  async press(x, y, ms) {   // a mouse press at page pixels, held for ms (a tap when short, a long press when long)
    await this.send('Input.dispatchMouseEvent', {type: 'mouseMoved', x, y});
    await this.send('Input.dispatchMouseEvent', {type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: 1});
    await sleep(ms);
    await this.send('Input.dispatchMouseEvent', {type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: 1});
  }
  async frame() { await this.eval('new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))'); }
  async shot(file) { const r = await this.send('Page.captureScreenshot', {format: 'png'}); writeFileSync(join(OUT, file), Buffer.from(r.data, 'base64')); return file; }
}

async function json(url, opt) { const r = await fetch(url, opt); if (!r.ok) throw new Error(`${url}: ${r.status}`); return r.json(); }
async function waitHttp(url, ms) { const t0 = Date.now(); for (;;) { try { const r = await fetch(url); if (r.ok) return; } catch {} if (Date.now() - t0 > ms) throw new Error('no answer from ' + url); await sleep(150); } }

// one headless Chrome per page: a second window or tab in headless mode does not paint, so the phone gets its own
const procs = [], profiles = [];
async function openPage(url, w, h, name, port, extra = []) {
  const prof = mkdtempSync(join(tmpdir(), 'ridgeline-e2e-')); profiles.push(prof);
  procs.push(spawn(CHROME, ['--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${prof}`, '--no-first-run', '--no-default-browser-check',
    '--disable-background-timer-throttling', '--disable-renderer-backgrounding', '--enable-unsafe-swiftshader', `--window-size=${w},${h}`, ...extra, 'about:blank'], {stdio: 'ignore'}));
  await waitHttp(`http://127.0.0.1:${port}/json/version`, 40000);
  let t = null;
  for (let i = 0; i < 40 && !t; i++) { t = (await json(`http://127.0.0.1:${port}/json/list`)).find(x => x.type === 'page'); if (!t) await sleep(150); }
  const p = await Page.open(t.webSocketDebuggerUrl, name);
  p.browser = (await json(`http://127.0.0.1:${port}/json/version`)).Browser;
  await p.send('Runtime.enable'); await p.send('Page.enable');
  await p.send('Emulation.setDeviceMetricsOverride', {width: w, height: h, deviceScaleFactor: 1, mobile: false});
  await p.send('Page.navigate', {url});
  return p;
}

// steer with the arrow keys until the phone says "On the summit", reading only what the phone shows
// (or, for the deliberate wrong aim, toward another peak's spot in the simulated view)
async function aim(phone, wrongPeak) {
  for (let i = 0; i < 600; i++) {
    let txt;
    if (wrongPeak) {
      const d = await phone.eval(`(() => { const v = worldView(livePeak(${JSON.stringify(wrongPeak)})); return [delta(v.az, heading), v.el - elevation]; })()`);
      if (Math.abs(d[0]) <= 1 && Math.abs(d[1]) <= 1) return i;
      txt = Math.abs(d[0]) > 1 ? `Turn ${d[0] > 0 ? 'right' : 'left'} ${Math.abs(d[0])}°` : `Tilt ${d[1] > 0 ? 'up' : 'down'} ${Math.abs(d[1])}°`;
    } else {
      txt = await phone.eval(`document.getElementById('aim-direction').textContent`);
      if (/On the summit/.test(txt)) return i;
    }
    const m = /(Turn|Tilt) (right|left|up|down) ([\d.]+)/.exec(txt);
    if (!m) throw new Error('cannot read the aim hint: ' + txt);
    const big = m[1] === 'Turn' ? +m[3] >= 2 : +m[3] >= 1.5;
    await phone.key({right: 'ArrowRight', left: 'ArrowLeft', up: 'ArrowUp', down: 'ArrowDown'}[m[2]], !big);
    await phone.frame();
  }
  throw new Error('could not aim in 600 key presses');
}

async function lockAndTime(phone, viewer, expectLocks, label) {
  const seq0 = await viewer.eval('window.TEAM8_LIVE_LAST ? TEAM8_LIVE_LAST.seq : 0');
  const vt0 = await viewer.eval('performance.now()');   // to line up the viewer's long tasks with this lock
  const t0 = Date.now();
  await phone.key('Enter');
  await viewer.until(`window.TEAM8_LIVE_LAST && TEAM8_LIVE_LAST.seq > ${seq0} && TEAM8_LIVE_LAST.locks === ${expectLocks} && TEAM8_LIVE_LAST.painted_at_ms`, 8000, 20);
  await viewer.until(`(window.TEAM8_DECK.props.layers || []).some(l => l.id === 'live-fix')`, 3000, 20);
  // Enter on the phone to the first viewer frame painted with the lock (both clocks are this computer's). Watching
  // from here would add the time this script waits for a busy page to answer, which is not what the judge sees.
  const ms = (await viewer.eval('TEAM8_LIVE_LAST.painted_at_ms')) - t0, seen_ms = Date.now() - t0;
  const ph = await phone.eval(`({meter: document.getElementById('lock-count')?.textContent, fix: document.getElementById('fix-line')?.textContent,
    wrong: document.querySelector('.wrong-box span')?.textContent || null, solve_ms: liveRes && liveRes.ms, notice: document.getElementById('notice')?.textContent,
    r95_m: liveRes && liveRes.hdr95 ? liveRes.hdr95.radius_m : null})`);
  const vw = await viewer.eval(`({title: document.getElementById('stepTitle').textContent, big: document.getElementById('fixBig').textContent,
    verdict: document.getElementById('verdict').textContent, lag_ms_sent_to_shown: TEAM8_LIVE_LAST.lag_ms, r95_m: TEAM8_LIVE.geometry.r95,
    layers: TEAM8_DECK.props.layers.filter(l => l.id.startsWith('live') && l.props.data && l.props.data.length).map(l => l.id)})`);
  log(`  ${label}: viewer showed it ${ms} ms after Enter (sent to shown ${vw.lag_ms_sent_to_shown} ms) | phone ${ph.meter} · ${ph.fix}${ph.wrong ? ' · WRONG: ' + ph.wrong : ''} | viewer "${vw.title}" ±${vw.r95_m} m`);
  const long_tasks = await viewer.eval(`(window.__lt || []).filter(e => e[0] + e[1] > ${vt0}).map(e => [Math.round(e[0] - ${vt0}), e[1]])`);   // [ms after Enter, ms long]
  // the phone's POST /lock for this lock: total, time spent starting the service worker first, connect, server wait
  const post = await phone.eval(`(() => { const e = performance.getEntriesByType('resource').filter(e => /\\/lock$/.test(e.name)).at(-1);
    return e && {ms: Math.round(e.duration), sw_ms: e.workerStart ? Math.round(e.fetchStart - e.workerStart) : 0, connect_ms: Math.round(e.connectEnd - e.connectStart),
      wait_ms: Math.round(e.responseStart - e.requestStart)}; })()`);
  const polls = await viewer.eval(`performance.getEntriesByType('resource').filter(e => /\\/state$/.test(e.name) && e.startTime + e.duration > ${vt0} - 600)
    .map(e => [Math.round(e.startTime - ${vt0}), Math.round(e.duration), Math.round(e.connectEnd - e.connectStart), Math.round(e.responseStart - e.requestStart),
      Math.round((e.connectStart || e.requestStart) - e.fetchStart), Math.round(e.responseEnd - e.responseStart), e.encodedBodySize])`);   // [ms after Enter, ms long, connect, server wait, queued, body, bytes]
  return {label, viewer_update_ms: ms, seen_by_test_ms: seen_ms, long_tasks, post, polls, same_r95: ph.r95_m === vw.r95_m, phone: ph, viewer: vw};
}

async function main() {
  if (!CHROME) throw new Error('Chrome not found, set CHROME');
  const res = {started: new Date().toISOString(), chrome: CHROME, base: BASE, locks: [], checks: {}};
  const pages = [];
  const server = spawn('python', [join(ROOT, 'tools', 'live_server.py'), '--port', String(PORT)], {cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe']});
  server.stdout.on('data', d => process.env.VERBOSE && process.stdout.write('[server] ' + d));
  try {
    await waitHttp(`${BASE}/health`, 10000);
    const health = await json(`${BASE}/health`);
    if (health.pid !== server.pid) throw new Error(`port ${PORT} is answered by another server (pid ${health.pid}, ours ${server.pid}), set PORT`);
    const viewer = await openPage(`${BASE}/terrain_viewer/?area=moran`, 1600, 900, 'viewer', DPORT || await freePort());
    res.browser = viewer.browser;
    pages.push(viewer);
    await viewer.until('!!(window.TEAM8_READY && window.TEAM8_DECK)', 60000, 200);
    log('viewer ready');
    // Chrome's fake camera (a test pattern) and auto-accepted permission, for the camera crosshair
    const phone = await openPage(`${BASE}/mockups/phone_screen/`, 430, 932, 'phone', DPORT ? DPORT + 1 : await freePort(), ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream']);
    pages.push(phone);
    await phone.until(`/Live solver ready/.test(document.getElementById('live-status').textContent)`, 20000);
    res.checks.phone_live_ready = await phone.eval(`document.getElementById('live-status').textContent`);
    log('phone:', res.checks.phone_live_ready);
    await phone.key('j'); await phone.frame();
    await phone.key('Enter');
    await phone.until(`screen === 'aim'`, 5000);
    await viewer.until(`window.TEAM8_LIVE && TEAM8_LIVE.on && document.getElementById('stepTitle').textContent === 'Phone ready'`, 5000);
    // main-thread tasks over 50 ms in the viewer, to see what holds up a lock when it is slow
    await phone.eval('performance.setResourceTimingBufferSize(20000); true');
    await viewer.eval(`performance.setResourceTimingBufferSize(20000); window.__lt = []; new PerformanceObserver(l => { for (const e of l.getEntries()) __lt.push([Math.round(e.startTime), Math.round(e.duration)]); }).observe({type: 'longtask'}); true`);
    res.checks.viewer_phone_ready = true;
    await phone.shot('phone_0_aim.png'); await viewer.shot('viewer_0_ready.png');
    // P1.3: camera behind a crosshair on the Lock screen, in three stages, because headless Chrome's fake camera opens only
    // once per process (a second getUserMedia gives NotFoundError, see .local/camprobe.mjs), and in some runs its track
    // ends by itself after a few seconds:
    //  1. the real (fake) camera through getUserMedia and the permission prompt, then Camera off ends its track;
    //     on a busy machine headless Chrome sometimes hands over a track that has already ended: then the phone must
    //     say the camera did not start, and the run records that the real camera was not seen this time;
    //  2. a track that ends by itself (an 'ended' event on a canvas track): the phone notices and says so;
    //  3. an animated canvas stream stands in for the camera: it stays on through two locks, and Use this fix ends it.
    const camUp = `(() => { const v = document.getElementById('cam'); return !!(camOn && camStream && v && !v.hidden && v.srcObject === camStream && camStream.getVideoTracks().length > 0 && camStream.getVideoTracks()[0].readyState === 'live' && document.getElementById('sky-wrap').classList.contains('cam-on')); })()`;   // headless Chrome does not always decode the fake frames into the element, so frames are not required
    const camOff = `!camOn && camStream === null && document.getElementById('cam').hidden && !document.getElementById('sky-wrap').classList.contains('cam-on')`;
    await phone.eval(`document.getElementById('cam-toggle').click()`);
    // Chrome's media process can take a while to start on a busy machine
    const real = await phone.until(`(${camUp}) ? 'opened' : (!camOn && camStream === null && camLostAt > 0 ? 'did not start' : '')`, 25000);
    res.checks.camera = {real_camera: real, notice: await phone.eval(`document.getElementById('notice').textContent`)};
    if (real === 'opened') {
      Object.assign(res.checks.camera, {size: await phone.eval(`[document.getElementById('cam').videoWidth, document.getElementById('cam').videoHeight]`),
        track: await phone.eval(`camStream.getVideoTracks()[0].label`), settings: await phone.eval(`(({width, height, frameRate}) => ({width, height, frameRate}))(camStream.getVideoTracks()[0].getSettings())`),
        focus_on_main: await phone.eval(`document.activeElement === document.getElementById('main')`), skyline_hidden: await phone.eval(`getComputedStyle(document.getElementById('sky')).visibility === 'hidden'`)});
      await phone.shot('phone_1_camera.png');
      await phone.eval(`window.__camTracks = camStream.getTracks(); document.getElementById('cam-toggle').click()`);
      res.checks.camera.off_ends_tracks = await phone.eval(`${camOff} && __camTracks.length > 0 && __camTracks.every(t => t.readyState === 'ended')`);
    } else {
      res.checks.camera.failure_reported = await phone.eval(`${camOff} && /camera did not start/i.test(document.getElementById('notice').textContent)`);
      await phone.shot('phone_1_camera_failed.png');
    }
    // from here getUserMedia hands out an animated canvas stream (frames keep coming, nothing ends by itself)
    await phone.eval(`(() => { const c = document.createElement('canvas'); c.width = 160; c.height = 120; const x = c.getContext('2d'); let f = 0;
      setInterval(() => { x.fillStyle = '#4a6b52'; x.fillRect(0, 0, 160, 120); x.fillStyle = '#d8d2c0'; x.fillRect((f++ * 3) % 160, 40, 30, 30); }, 100);
      navigator.mediaDevices.getUserMedia = async () => c.captureStream(10); })()`);
    await phone.eval(`document.getElementById('cam-toggle').click()`);
    await phone.until(camUp, 4000);
    await phone.eval(`camStream.getVideoTracks()[0].dispatchEvent(new Event('ended'))`);
    res.checks.camera.ended_noticed = await phone.eval(`${camOff} && camLostAt > 0 && /camera stopped/i.test(document.getElementById('notice').textContent)`);
    await phone.eval(`document.getElementById('cam-toggle').click()`);
    await phone.until(camUp, 4000);
    log('  camera:', JSON.stringify(res.checks.camera));

    const target = () => phone.eval('liveTarget().id');
    for (let n = 1; n <= 2; n++) {
      const id = await target(); const presses = await aim(phone);
      res.locks.push({...(await lockAndTime(phone, viewer, n, `lock ${n} ${id} (${presses} key presses)`)), peak: id, presses});
      await phone.shot(`phone_${n}.png`); await viewer.shot(`viewer_${n}.png`);
    }
    res.checks.camera.on_after_two_locks = await phone.until(camUp, 3000).then(() => true, () => false);
    if (!res.checks.camera.on_after_two_locks) res.checks.camera.after_state = await phone.eval(`(() => { const v = document.getElementById('cam'); return {camOn, stream: !!camStream, same: !!v && v.srcObject === camStream, hidden: v && v.hidden, cls: document.getElementById('sky-wrap').className, tracks: camStream ? camStream.getTracks().map(t => t.kind + ':' + t.readyState) : null, screen}; })()`);
    // lock 3 on purpose at the wrong peak: the phone asks for one peak, the person aims at an already locked one
    const want = await target(), wrongAt = (await phone.eval(`locks.map(l => l.peak_id)`))[1];
    const pw = await aim(phone, wrongAt);
    const w = await lockAndTime(phone, viewer, 3, `lock 3 ${want}, aimed at ${wrongAt} instead (${pw} presses)`);
    res.locks.push({...w, peak: want, aimed_at: wrongAt, presses: pw});
    res.checks.wrong_aim_message = w.phone.wrong;
    res.checks.wrong_aim_viewer = w.viewer.title;
    res.checks.target_after_wrong = await target();
    await phone.shot('phone_3_wrong.png'); await viewer.shot('viewer_3_wrong.png');
    if (w.phone.wrong) {
      await phone.eval(`document.getElementById('reaim').click()`);
      await viewer.until(`TEAM8_LIVE_LAST.locks === 2`, 3000, 20);
      const id = await target(); const presses = await aim(phone);
      res.locks.push({...(await lockAndTime(phone, viewer, 3, `lock 3 again ${id} (${presses} presses)`)), peak: id, presses});
    }
    await phone.shot('phone_4_enough.png'); await viewer.shot('viewer_4_fix.png');
    const fin = await phone.eval(`({meter: document.getElementById('lock-count').textContent, fix: document.getElementById('fix-line').textContent,
      summary: PhoneOrientation.getLive()})`);
    res.checks.phone_final = {meter: fin.meter, fix: fin.fix, confidence: fin.summary && fin.summary.confidence, r95_m: fin.summary && fin.summary.hdr95.radius_m,
      best: fin.summary && fin.summary.best && {lat: fin.summary.best.lat, lon: fin.summary.best.lon}, solve_ms: fin.summary && fin.summary.ms};
    // Reveal on the judge screen
    await viewer.eval(`document.querySelector('[data-live="reveal"]').click()`);
    await viewer.until(`TEAM8_LIVE.reveal && (TEAM8_DECK.props.layers || []).some(l => l.id === 'live-truth')`, 3000);
    await sleep(1400);   // the camera flight
    res.checks.reveal = await viewer.eval(`({title: document.getElementById('stepTitle').textContent, text: document.getElementById('stepText').textContent,
      err_m: TEAM8_LIVE.geometry.err, r95_m: TEAM8_LIVE.geometry.r95, inside: TEAM8_LIVE.geometry.inside, triangle_side_m: TEAM8_LIVE.geometry.triSide})`);
    await viewer.shot('viewer_5_reveal.png');
    // P1.4: the first Reveal opens the trust card, built from solver_eval.json, with this fix's ±r95 and error on the chart
    res.checks.trust = await viewer.eval(`(() => { const el = document.getElementById('trustCard'), r = el && el.getBoundingClientRect();
      return el && {open: !el.hidden, title: el.querySelector('h3').textContent, caption: el.querySelector('.cap').textContent, curves: el.querySelectorAll('svg path[fill="none"]').length,
        marks_this_fix: !!el.querySelector('svg circle[r="4.5"]') && /this fix/.test(el.querySelector('.key').textContent), rows: [...el.querySelectorAll('dt')].map((d, i) => d.textContent + ': ' + el.querySelectorAll('dd')[i].textContent),
        box: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)], button_pressed: document.querySelector('[data-live="trust"]').getAttribute('aria-pressed')}; })()`);
    await viewer.eval(`document.querySelector('[data-live="trust"]').click()`);
    res.checks.trust.closes = await viewer.eval(`document.getElementById('trustCard').hidden && document.querySelector('[data-live="trust"]').getAttribute('aria-pressed') === 'false'`);
    log('  trust card:', JSON.stringify({title: res.checks.trust.title, caption: res.checks.trust.caption, box: res.checks.trust.box, closes: res.checks.trust.closes}));
    await viewer.eval(`document.querySelector('[data-live="trust"]').click()`);   // open again for the screenshots that follow
    // Backspace undoes the last lock; then a wrong aim fixed the natural way: aim at the flagged peak and Lock again
    const s0 = await viewer.eval('TEAM8_LIVE_LAST.seq');
    await phone.key('Backspace');
    await viewer.until(`TEAM8_LIVE_LAST.seq > ${s0} && TEAM8_LIVE_LAST.locks === 2`, 3000, 20);
    res.checks.undo_seen_by_viewer = true;
    const want2 = await target(), wrong2 = (await phone.eval(`locks.map(l => l.peak_id)`))[0];
    const pw2 = await aim(phone, wrong2);
    const w2 = await lockAndTime(phone, viewer, 3, `lock 3 ${want2}, aimed at ${wrong2} instead (${pw2} presses)`);
    res.locks.push({...w2, peak: want2, aimed_at: wrong2, presses: pw2});
    res.checks.second_wrong = {message: w2.phone.wrong, target_after: await target()};
    const pr = await aim(phone);
    const w3 = await lockAndTime(phone, viewer, 3, `lock ${want2} again, replaces the bad lock (${pr} presses)`);
    res.locks.push({...w3, peak: want2, presses: pr});
    res.checks.relock = {locks: await phone.eval('locks.map(l => l.peak_id)'), meter: w3.phone.meter, wrong: w3.phone.wrong};
    const fin2 = await phone.eval(`document.getElementById('lock-count').textContent`);
    await phone.shot('phone_6_relock.png'); await viewer.shot('viewer_6_relock.png');
    // Enter on the phone uses the fix, then P1.2: a destination and a safe route, followed on the judge map
    if (/Enough/.test(fin2)) {
      await phone.eval(`window.__camTracks = camStream ? camStream.getTracks() : []`);
      await phone.key('Enter');
      await phone.until(`screen === 'guide' && !!document.getElementById('destmap')`, 3000);
      res.checks.camera.stopped_off_aim = await phone.eval(`!camOn && camStream === null && __camTracks.length > 0 && __camTracks.every(t => t.readyState === 'ended')`);
      res.checks.phone_guide = await phone.eval(`[...document.querySelectorAll('.dest-list button')].map(b => b.textContent)`);
      await phone.shot('phone_7_dest.png');
      // a saved point at the able limit
      const seqA = await viewer.eval('TEAM8_LIVE_LAST.seq');
      const tA = Date.now();
      await phone.eval(`document.querySelector('[data-dest="rendezvous"]').click()`);
      await phone.until(`screen === 'guide' && liveRoute && liveRoute.ok`, 3000);
      await viewer.until(`TEAM8_LIVE_LAST.seq > ${seqA} && TEAM8_DECK.props.layers.some(l => l.id === 'live-route' && l.props.data && l.props.data.length)`, 5000, 20);
      const able = await phone.eval(`({next: document.getElementById('route-next')?.textContent, stats: document.getElementById('route-stats')?.textContent,
        say: document.getElementById('route-say')?.textContent, arrow: document.getElementById('route-arrow')?.style.transform, plan_ms: liveRoute.ms})`);
      res.checks.route_able = {...able, viewer_ms: Date.now() - tA, viewer_title: await viewer.eval(`document.getElementById('stepTitle').textContent`),
        viewer_text: await viewer.eval(`document.getElementById('stepText').textContent`)};
      log(`  route to the rendezvous: ${able.stats} | ${able.next} | on the judge map ${res.checks.route_able.viewer_ms} ms after the tap`);
      await sleep(1500);   // let the camera settle on the route
      await phone.shot('phone_8_route.png'); await viewer.shot('viewer_8_route.png');
      // injured: no safe route from here, so Stay. Signal. Wait. on both screens
      await phone.eval(`document.getElementById('injury').click()`);
      await phone.until(`screen === 'stay'`, 3000);
      await viewer.until(`document.getElementById('stepTitle').textContent === 'Stay. Signal. Wait.'`, 3000, 20);
      res.checks.injured_stay = {phone: await phone.eval(`document.querySelector('.heading h1').textContent + ' | ' + document.querySelector('.stay-title').textContent + ' | ' + document.querySelector('.danger-block .muted').textContent`),
        viewer: await viewer.eval(`document.getElementById('stepText').textContent`)};
      log('  injured:', res.checks.injured_stay.phone);
      await phone.shot('phone_9_stay.png'); await viewer.shot('viewer_9_stay.png');
      // back to able; a long press on the map locks a pin on the route, a tap does not move it, unlocking takes two taps
      await phone.eval(`document.getElementById('healthy').click()`);
      await phone.until(`screen === 'guide' && liveRoute && liveRoute.ok`, 3000);
      const xy = await phone.eval(`(() => { const cv = document.getElementById('destmap'); cv.scrollIntoView({block: 'center'}); const k = liveRoute.dist.findIndex(d => d > 900), p = liveRoute.path[k];
        const r = cv.getBoundingClientRect(), [x, y] = mapXY(p[0], p[1], r.width, r.height); return [r.left + x, r.top + y]; })()`);
      await phone.press(xy[0], xy[1], 900);
      await phone.until(`pinLocked && dest && dest.kind === 'pin'`, 3000);
      await phone.until(`screen === 'guide' || screen === 'stay'`, 2000);
      const pinAt = await phone.eval(`({lon: pin.lon, lat: pin.lat, route_ok: !!(liveRoute && liveRoute.ok), say: liveRoute && liveRoute.say})`);
      const xy2 = await phone.eval(`(() => { const cv = document.getElementById('destmap'); cv.scrollIntoView({block: 'center'}); const r = cv.getBoundingClientRect(); return [r.left + r.width * 0.2, r.top + r.height * 0.3]; })()`);
      await phone.press(xy2[0], xy2[1], 80);
      await sleep(100);
      const pinAfter = await phone.eval(`({lon: pin.lon, lat: pin.lat, locked: pinLocked, notice})`);
      await phone.eval(`document.getElementById('unlock-pin').click()`);
      const afterOne = await phone.eval('pinLocked');
      await phone.eval(`document.getElementById('unlock-yes').click()`);
      const afterTwo = await phone.eval('pinLocked');
      res.checks.pin = {locked_at: pinAt, after_tap: pinAfter, did_not_move: pinAt.lon === pinAfter.lon && pinAt.lat === pinAfter.lat,
        unlock_after_one_action_still_locked: afterOne, unlock_after_two_actions_locked: afterTwo};
      log('  pin:', JSON.stringify(res.checks.pin));
      await phone.shot('phone_10_pin.png'); await viewer.shot('viewer_10_pin.png');
      // keyboard: arrows move the cursor, a short Enter drops the pin, a held Enter locks it
      await phone.eval(`document.getElementById('destmap').focus()`);
      const c0 = await phone.eval('({...mapCursor})');
      await phone.key('ArrowLeft'); await phone.key('ArrowLeft'); await phone.key('ArrowUp');
      await phone.key('Enter');
      await sleep(100);
      const kDrop = await phone.eval(`({lon: pin.lon, lat: pin.lat, locked: pinLocked, cursor: {...mapCursor}})`);
      await phone.keyHold('Enter', 900);
      await phone.until('pinLocked', 2000);
      res.checks.keyboard_pin = {cursor_start: c0, dropped: kDrop, moved_west: kDrop.lon < c0.lon, moved_north: kDrop.lat > c0.lat,
        dropped_unlocked: kDrop.locked === false, locked_by_hold: await phone.eval('pinLocked'), screen: await phone.eval('screen')};
      log('  keyboard pin:', JSON.stringify(res.checks.keyboard_pin));
    }
    // leaving and coming back
    await viewer.eval(`document.querySelector('[data-live="leave"]').click()`);
    res.checks.viewer_left_live = await viewer.eval(`!TEAM8_LIVE.on`);
    // a route update from the phone after Leave is kept but does not take the screen back (only a lock does)
    {
      const seq0 = await viewer.eval('TEAM8_LIVE_LAST ? TEAM8_LIVE_LAST.seq : 0');
      await phone.eval(`postLive('route')`);
      await sleep(1200);
      res.checks.route_after_leave_stays_off = await viewer.eval(`!TEAM8_LIVE.on`);
    }
    // P1.1 is the teammate's judge flow (PR #5): a click on the map drops a pilot there, makes noisy locks from that
    // spot, solves and shows the error. Checked here so the live mode is known not to break it: a real mouse click at
    // the screen point of the demo spot (solver_grid.json, with its ground height). The viewer keeps its state private,
    // so a new drop is seen as a new window.TEAM8_SCEN, which the viewer writes on every drop.
    {
      await viewer.eval('window.__scen0 = window.TEAM8_SCEN || null');
      const pt = await viewer.eval(`fetch('data/moran/solver_grid.json').then(r => r.json()).then(g => { const d = g.demo;
        const [x, y] = TEAM8_DECK.getViewports()[0].project([d.lon, d.lat, d.elev_m]); return {lon: d.lon, lat: d.lat, x: Math.round(x), y: Math.round(y)}; })`);
      await viewer.press(pt.x, pt.y, 60);
      const how = await viewer.until(`!!window.TEAM8_SCEN && window.TEAM8_SCEN !== window.__scen0`, 5000).then(() => 'mouse click', () => 'no drop: the mouse click did not reach the map');
      await sleep(2500);   // let the autoplay walk through the locks to the fix
      res.checks.judge_click = {how, at_px: [pt.x, pt.y], ...(await viewer.eval(`({live_on: TEAM8_LIVE.on,
        fix: window.TEAM8_SCEN && {err_m: TEAM8_SCEN.err_m, r95_m: TEAM8_SCEN.r95_m, inside95: TEAM8_SCEN.inside95, visible_peaks: TEAM8_SCEN.visible.length, solve_ms: TEAM8_SCEN.solve_ms},
        title: document.getElementById('stepTitle').textContent, big: document.getElementById('fixBig').textContent})`))};
      log('  judge click:', JSON.stringify(res.checks.judge_click));
      await viewer.shot('viewer_11_judge_click.png');
    }
    res.checks.phone_errors = phone.errors; res.checks.viewer_errors = viewer.errors;
    const ms = res.locks.map(l => l.viewer_update_ms);
    res.summary = {locks: res.locks.length, viewer_update_ms_max: Math.max(...ms), viewer_update_ms_median: ms.slice().sort((a, b) => a - b)[Math.floor(ms.length / 2)],
      within_1s: ms.every(v => v < 1000), wrong_aim_flagged: !!res.checks.wrong_aim_message, reveal_err_m: res.checks.reveal.err_m,
      reveal_inside_95: res.checks.reveal.inside, page_errors: phone.errors.length + viewer.errors.length,
      same_r95_phone_and_viewer: res.locks.every(l => l.same_r95), target_is_flagged_peak: res.checks.target_after_wrong === want,
      second_wrong_flagged: !!res.checks.second_wrong.message, relock_replaced: res.checks.relock.locks.length === 3 && /Enough/.test(res.checks.relock.meter),
      phone_used_fix: !!(res.checks.phone_guide && res.checks.phone_guide.length),
      route_shown_on_viewer_ms: res.checks.route_able ? res.checks.route_able.viewer_ms : null,
      injured_stay_signal_wait: !!(res.checks.injured_stay && /Stay/.test(res.checks.injured_stay.phone)),
      pin_locked_by_long_press: !!(res.checks.pin && res.checks.pin.did_not_move && res.checks.pin.after_tap.locked),
      unlock_needs_two_actions: !!(res.checks.pin && res.checks.pin.unlock_after_one_action_still_locked === true && res.checks.pin.unlock_after_two_actions_locked === false),
      keyboard_pin: !!(res.checks.keyboard_pin && res.checks.keyboard_pin.moved_west && res.checks.keyboard_pin.dropped_unlocked && res.checks.keyboard_pin.locked_by_hold)};
    const cam = res.checks.camera;
    const realOk = cam && (cam.real_camera === 'opened' ? /fake_device/.test(cam.track) && cam.focus_on_main && cam.skyline_hidden && cam.off_ends_tracks : cam.failure_reported === true);
    res.summary.camera = !!(realOk && cam.ended_noticed && cam.on_after_two_locks && cam.stopped_off_aim);
    res.summary.real_camera_opened = !!(cam && cam.real_camera === 'opened');   // informational: headless Chrome does not always start it
    const tr = res.checks.trust;
    res.summary.trust_card = !!(tr && tr.open && tr.curves >= 3 && tr.marks_this_fix && new RegExp('±' + res.checks.reveal.r95_m + ' m').test(tr.title) && /19 in 20/.test(tr.caption) && tr.closes);
    const jc = res.checks.judge_click;
    res.summary.judge_click = !!(res.checks.route_after_leave_stays_off && jc && jc.how === 'mouse click' && !jc.live_on && jc.fix && Number.isFinite(jc.fix.err_m) && Number.isFinite(jc.fix.r95_m));
    log('summary', JSON.stringify(res.summary));
  } catch (err) {
    res.error = String(err && err.stack || err);
    res.debug = {};
    for (const p of pages) {
      res.debug[p.name] = {errors: p.errors, loading: await p.eval(`(document.getElementById('loading') || {}).textContent || ''`).catch(e => String(e)),
        url: await p.eval('location.href').catch(() => null)};
      if (p.name === 'phone') res.debug.phone.state = await p.eval(`({screen, camOn, stream: !!camStream, tracks: camStream ? camStream.getTracks().map(t => t.readyState) : null, notice,
        video: (() => { const v = document.getElementById('cam'); return v && {hidden: v.hidden, w: v.videoWidth, h: v.videoHeight, rs: v.readyState, paused: v.paused, src: !!v.srcObject}; })()})`).catch(e => String(e));
      if (p.name === 'viewer') res.debug.viewer.hint = await p.eval(`({html: document.getElementById('hint').innerHTML, key: document.getElementById('hint').dataset.html, on: TEAM8_LIVE.on, reveal: TEAM8_LIVE.reveal, trust: TEAM8_LIVE.trust})`).catch(e => String(e));
      await p.shot(`fail_${p.name}.png`).catch(() => null);
    }
    console.error(res.error, JSON.stringify(res.debug, null, 1));
  } finally {
    res.finished = new Date().toISOString();
    writeFileSync(join(OUT, 'result.json'), JSON.stringify(res, null, 2));
    // on Windows, killing chrome.exe leaves its renderer and GPU children running: take the whole tree
    procs.forEach(p => { if (process.platform === 'win32' && p.pid) spawnSync('taskkill', ['/PID', String(p.pid), '/T', '/F'], {stdio: 'ignore'}); else p.kill(); });
    server.kill();
    await sleep(500);
    profiles.forEach(prof => { try { rmSync(prof, {recursive: true, force: true}); } catch {} });
  }
  const s = res.summary;
  const ok = s && s.within_1s && s.wrong_aim_flagged && !s.page_errors && s.same_r95_phone_and_viewer && s.target_is_flagged_peak && s.second_wrong_flagged &&
    s.relock_replaced && s.phone_used_fix && s.route_shown_on_viewer_ms < 1500 && s.injured_stay_signal_wait && s.pin_locked_by_long_press &&
    s.unlock_needs_two_actions && s.keyboard_pin && s.camera && s.trust_card && s.judge_click;
  if (!ok) { console.error('E2E FAILED'); process.exitCode = 1; } else log('E2E PASSED, results in', OUT);
}
main().catch(e => { console.error(e); process.exitCode = 1; });
