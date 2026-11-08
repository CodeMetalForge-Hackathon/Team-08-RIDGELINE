// Offline check of the phone app (P1.5): load it once from tools/live_server.py in headless Chrome, stop the server,
// reload, and check the page, the scenario, the live solver and a Lock all still work from the service worker's copy.
// No dependencies: Chrome is driven over the DevTools protocol with Node's built-in WebSocket (Node 22 or newer).
//
//   node tests/pwa_offline.mjs              (CHROME=<path to chrome.exe> if it is not in the usual place)
//
// Writes OUT/pwa_result.json and screenshots (default .local/pwa). This is desktop Chrome with the server really
// stopped, not a phone in airplane mode.
import {spawn, spawnSync} from 'node:child_process';
import {mkdirSync, writeFileSync, existsSync, mkdtempSync, rmSync} from 'node:fs';
import {createServer} from 'node:net';
import {tmpdir} from 'node:os';
import {join, dirname} from 'node:path';
import {fileURLToPath} from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const OUT = process.env.OUT || join(ROOT, '.local', 'pwa');
const CHROME = process.env.CHROME || ['C:/Program Files/Google/Chrome/Application/chrome.exe', 'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  '/usr/bin/google-chrome', '/usr/bin/chromium', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'].find(p => existsSync(p));
const sleep = ms => new Promise(r => setTimeout(r, ms));
const log = (...a) => console.log(...a);
const freePort = () => new Promise((ok, no) => { const s = createServer(); s.on('error', no); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => ok(p)); }); });
const killTree = p => { if (!p || p.exitCode !== null) return; if (process.platform === 'win32') spawnSync('taskkill', ['/PID', String(p.pid), '/T', '/F'], {stdio: 'ignore'}); else p.kill(); };
mkdirSync(OUT, {recursive: true});

class Page {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.cb = new Map(); this.errors = [];
    ws.onmessage = m => {
      const d = JSON.parse(m.data);
      if (d.id && this.cb.has(d.id)) { const {ok, no} = this.cb.get(d.id); this.cb.delete(d.id); d.error ? no(new Error(d.error.message)) : ok(d.result); return; }
      if (d.method === 'Runtime.exceptionThrown') this.errors.push(d.params.exceptionDetails.exception?.description || d.params.exceptionDetails.text);
      if (d.method === 'Runtime.consoleAPICalled' && d.params.type === 'error') this.errors.push(d.params.args.map(a => a.value ?? a.description).join(' '));
    };
  }
  send(method, params = {}) { const id = ++this.id; this.ws.send(JSON.stringify({id, method, params})); return new Promise((ok, no) => this.cb.set(id, {ok, no})); }
  async eval(expr) {
    const r = await this.send('Runtime.evaluate', {expression: expr, awaitPromise: true, returnByValue: true});
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
    return r.result.value;
  }
  async until(expr, ms = 20000) {
    const t0 = Date.now();
    for (;;) { const v = await this.eval(expr).catch(() => null); if (v) return v; if (Date.now() - t0 > ms) throw new Error('timed out waiting for ' + expr); await sleep(80); }
  }
  async key(key, shift = false) {
    const K = {Enter: [13, 'Enter', '\r'], ArrowLeft: [37, 'ArrowLeft'], ArrowUp: [38, 'ArrowUp'], ArrowRight: [39, 'ArrowRight'], ArrowDown: [40, 'ArrowDown']};
    const [vk, code, text] = K[key] || [key.toUpperCase().charCodeAt(0), 'Key' + key.toUpperCase(), key];
    const base = {key, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, modifiers: shift ? 8 : 0};
    await this.send('Input.dispatchKeyEvent', {type: 'keyDown', ...base, ...(text ? {text} : {})});
    await this.send('Input.dispatchKeyEvent', {type: 'keyUp', ...base});
  }
  async frame() { await this.eval('new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))'); }
  async shot(file) { const r = await this.send('Page.captureScreenshot', {format: 'png'}); writeFileSync(join(OUT, file), Buffer.from(r.data, 'base64')); }
  async load(url) { await this.send('Page.navigate', {url}); await this.until(`document.readyState === 'complete' && location.href === ${JSON.stringify(url)}`, 15000); }
}
async function json(url) { const r = await fetch(url); if (!r.ok) throw new Error(`${url}: ${r.status}`); return r.json(); }
async function waitHttp(url, ms) { const t0 = Date.now(); for (;;) { try { const r = await fetch(url); if (r.ok) return; } catch {} if (Date.now() - t0 > ms) throw new Error('no answer from ' + url); await sleep(150); } }

// steer with the arrow keys until the phone says "On the summit", reading only what the phone shows
async function aim(phone) {
  for (let i = 0; i < 600; i++) {
    const txt = await phone.eval(`document.getElementById('aim-direction').textContent`);
    if (/On the summit/.test(txt)) return i;
    const m = /(Turn|Tilt) (right|left|up|down) ([\d.]+)/.exec(txt);
    if (!m) throw new Error('cannot read the aim hint: ' + txt);
    await phone.key({right: 'ArrowRight', left: 'ArrowLeft', up: 'ArrowUp', down: 'ArrowDown'}[m[2]], m[1] === 'Turn' ? +m[3] < 2 : +m[3] < 1.5);
    await phone.frame();
  }
  throw new Error('could not aim in 600 key presses');
}

async function main() {
  if (!CHROME) throw new Error('Chrome not found, set CHROME');
  const port = await freePort(), base = `http://127.0.0.1:${port}`, app = `${base}/mockups/phone_screen/`;
  const res = {started: new Date().toISOString(), base, checks: {}};
  const server = spawn('python', [join(ROOT, 'tools', 'live_server.py'), '--port', String(port)], {cwd: ROOT, stdio: 'ignore'});
  const prof = mkdtempSync(join(tmpdir(), 'ridgeline-pwa-')), dport = await freePort();
  let chrome = null, phone = null;
  try {
    await waitHttp(`${base}/health`, 10000);
    if ((await json(`${base}/health`)).pid !== server.pid) throw new Error(`port ${port} is answered by another server`);
    chrome = spawn(CHROME, ['--headless=new', `--remote-debugging-port=${dport}`, `--user-data-dir=${prof}`, '--no-first-run', '--no-default-browser-check',
      '--window-size=430,932', 'about:blank'], {stdio: 'ignore'});
    await waitHttp(`http://127.0.0.1:${dport}/json/version`, 15000);
    res.chrome = (await json(`http://127.0.0.1:${dport}/json/version`)).Browser;
    let t = null;
    for (let i = 0; i < 40 && !t; i++) { t = (await json(`http://127.0.0.1:${dport}/json/list`)).find(x => x.type === 'page'); if (!t) await sleep(150); }
    const ws = new WebSocket(t.webSocketDebuggerUrl); await new Promise((ok, no) => { ws.onopen = ok; ws.onerror = no; });
    phone = new Page(ws);
    await phone.send('Runtime.enable'); await phone.send('Page.enable');
    await phone.send('Emulation.setDeviceMetricsOverride', {width: 430, height: 932, deviceScaleFactor: 1, mobile: false});

    // 1. first load, online: the service worker installs, keeps its copy and takes over the page
    const t0 = Date.now();
    await phone.load(app);
    await phone.until(`!!navigator.serviceWorker.controller && /^Works offline/.test(document.getElementById('offline-status').textContent)`, 20000);
    res.checks.online = {ms_to_offline_ready: Date.now() - t0, status: await phone.eval(`document.getElementById('offline-status').textContent`),
      live: await phone.eval(`document.getElementById('live-status').textContent`),
      cached: await phone.eval(`(async () => { const out = {}; for (const k of await caches.keys()) out[k] = (await (await caches.open(k)).keys()).map(r => new URL(r.url).pathname); return out; })()`)};
    const man = await phone.send('Page.getAppManifest');
    const inst = await phone.send('Page.getInstallabilityErrors').catch(e => ({error: String(e)}));
    res.checks.manifest = {url: man.url, errors: man.errors, name: man.data ? JSON.parse(man.data).short_name : null,
      installability_errors: inst.installabilityErrors ? inst.installabilityErrors.map(e => e.errorId) : inst};
    log('online:', JSON.stringify({ms: res.checks.online.ms_to_offline_ready, status: res.checks.online.status, manifest: res.checks.manifest}));

    // 2. the server stops: nothing answers on the port any more
    killTree(server); await sleep(400);
    res.checks.server_down = await fetch(`${base}/health`).then(() => false, () => true);

    // 3. reload with no server: page, scenario, live solver and grid all come from the copy
    const t1 = Date.now();
    await phone.send('Page.reload', {ignoreCache: false});
    await phone.until(`document.readyState === 'complete' && !!data && /^Live solver ready/.test(document.getElementById('live-status').textContent)`, 20000);
    res.checks.offline_reload = {ms: Date.now() - t1, title: await phone.eval('document.title'), live: await phone.eval(`document.getElementById('live-status').textContent`),
      status: await phone.eval(`document.getElementById('offline-status').textContent`), screen: await phone.eval('screen')};
    log('offline reload:', JSON.stringify(res.checks.offline_reload));

    // 4. a Lock offline: J (GPS off), Enter (start the fix), aim with the arrow keys, Enter (Lock); the solver runs on the phone
    await phone.key('J'); await phone.frame(); await phone.key('Enter'); await phone.until(`screen === 'aim'`, 5000);
    const presses = await aim(phone);
    await phone.key('Enter');
    await phone.until(`locks.length === 1 && !!liveRes`, 5000);
    await phone.until(`/offline/i.test(document.getElementById('link-status').textContent)`, 5000).catch(() => null);
    res.checks.offline_lock = {presses, meter: await phone.eval(`document.getElementById('lock-count')?.textContent`), fix: await phone.eval(`document.getElementById('fix-line')?.textContent`),
      solve_ms: await phone.eval('liveRes.ms'), link: await phone.eval(`document.getElementById('link-status').textContent`)};
    log('offline lock:', JSON.stringify(res.checks.offline_lock));
    await phone.shot('phone_offline_lock.png');

    // 5. the page's other address, index.html, also opens offline, and a file the copy does not have gets a clear 503
    await phone.load(app + 'index.html');
    res.checks.offline_index_html = await phone.until(`document.readyState === 'complete' && !!data && document.title`, 15000);
    res.checks.not_cached = await phone.eval(`fetch('./no-such-file.json').then(r => r.status + ' ' + r.statusText, e => 'rejected: ' + e.message)`);
    await phone.eval(`document.getElementById('settings-open').click()`); await phone.frame();
    await phone.shot('phone_offline_settings.png');
    res.page_errors = phone.errors;

    const c = res.checks;
    res.summary = {offline_ready_online: /^Works offline/.test(c.online.status), installable: Array.isArray(c.manifest.installability_errors) && c.manifest.installability_errors.length === 0,
      manifest_ok: !!c.manifest.name && c.manifest.errors.length === 0, server_down: c.server_down, offline_reload: /^Live solver ready/.test(c.offline_reload.live),
      offline_lock: /1/.test(c.offline_lock.meter || '') && Number.isFinite(c.offline_lock.solve_ms), link_says_offline: /offline/i.test(c.offline_lock.link),
      offline_index_html: /RIDGELINE/.test(c.offline_index_html || ''), not_cached_503: /^503/.test(c.not_cached), page_errors: res.page_errors.length};
    log('summary', JSON.stringify(res.summary));
  } catch (err) {
    res.error = String(err && err.stack || err);
    if (phone) res.page_errors = phone.errors;
    console.error(res.error);
  } finally {
    res.finished = new Date().toISOString();
    writeFileSync(join(OUT, 'pwa_result.json'), JSON.stringify(res, null, 2));
    killTree(chrome); killTree(server);
    await sleep(500);
    try { rmSync(prof, {recursive: true, force: true}); } catch {}
  }
  const s = res.summary, ok = s && Object.entries(s).every(([k, v]) => k === 'page_errors' ? v === 0 : v === true);
  log(ok ? `PWA OFFLINE PASSED, results in ${OUT}` : 'PWA OFFLINE FAILED');
  process.exit(ok ? 0 : 1);
}
main();
