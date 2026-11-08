"""RIDGELINE local server: the phone page, the big-screen board, the solver data and the Lock relay.

Run from the repo root:   python -I ridgeline/server.py            (port 8768)
Phone over USB:           adb reverse tcp:8768 tcp:8768, then open http://localhost:8768 on the phone
Big screen on the PC:     http://localhost:8768/board

The phone posts its observations (barometer and Locks) to /api/state after every change.
The board long-polls /api/wait (it returns the moment a Lock arrives) and runs the same solver.js,
so both show the same answer.
Nothing leaves this PC. The server binds to 127.0.0.1 only.

ATAK (Cursor on Target, events built by cot.py)
  TAK Server mode, always on:  ATAK connects to 127.0.0.1:--cot-port (default 8087) over plain TCP, no SSL.
    Phone over USB: adb reverse tcp:8087 tcp:8087, then in ATAK add a TAK Server 127.0.0.1, port 8087, TCP.
    ATAK gets the current fix on connect, every change, and a resend every 15 s.
  Push mode, optional:  --atak-push 127.0.0.1:4242 (with adb forward tcp:4242 tcp:4242) opens one short TCP
    connection per event to an ATAK TCP input, on every change and every 15 s.
  GET /api/cot   the current events, one per line
  GET /api/atak  {mode, clients, connected, sent, last_sent, last_error, ...}
GET /api/build  {code, started, folder}: which server.py and cot.py this process runs.
  Python reads them once at start, so after a pull the server keeps the old code until restarted.
  scripts/preflight.py compares code with the files on disk.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import importlib.util
import itertools
import json
import math
import socket
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
# python -I does not put this folder on sys.path, so cot.py is loaded by its path
_spec = importlib.util.spec_from_file_location('ridgeline_cot', HERE / 'cot.py')
cot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cot)
FILES = {
    '/': HERE / 'phone' / 'index.html',
    '/board': HERE / 'board' / 'index.html',
    '/board/': HERE / 'board' / 'index.html',
    '/cards': HERE / 'cards' / 'index.html',
    '/cards/': HERE / 'cards' / 'index.html',
    '/trust': HERE / 'trust' / 'index.html',
    '/trust/': HERE / 'trust' / 'index.html',
    '/data/trust.json': HERE / 'data' / 'trust.json',
    '/solver.js': HERE / 'solver.js',
    '/hide.js': HERE / 'hide.js',            # HIDE and SIGNAL candidates (terrain only)
    '/skyline.js': HERE / 'skyline.js',      # expected horizon toward the next peak
    '/pace.js': HERE / 'pace.js',            # Auto-PACE, the fix keeps living as you walk
    '/follow.js': HERE / 'follow.js',        # Follow, the locked route one instruction at a time
    '/walk': HERE / 'walk' / 'index.html',   # Walk here, Auto-PACE at the venue (camera, then steps)
    '/walk/': HERE / 'walk' / 'index.html',
    '/sw.js': HERE / 'phone' / 'sw.js',      # service worker at the root so its scope is the whole app
    '/manifest.webmanifest': HERE / 'phone' / 'manifest.webmanifest',
    '/data/moran.json': HERE / 'data' / 'moran.json',
    '/topo/moran.jpg': REPO / 'terrain_viewer' / 'data' / 'moran' / 'topo.jpg',
}
DIRS = {'/phone/': HERE / 'phone', '/board/': HERE / 'board', '/cards/': HERE / 'cards', '/walk/': HERE / 'walk',
        '/tv/': REPO / 'terrain_viewer'}   # read only: the viewer's route planner and terrain bundle
TYPES = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.json': 'application/json',
         '.jpg': 'image/jpeg', '.png': 'image/png', '.svg': 'image/svg+xml', '.css': 'text/css; charset=utf-8',
         '.webmanifest': 'application/manifest+json'}
CODE = (HERE / 'server.py', HERE / 'cot.py')   # read once at start


def code_id(paths=CODE) -> str:
    """Short id of the Python this server runs. Line endings are ignored, so a CRLF checkout matches."""
    h = hashlib.sha256()
    for path in paths:
        h.update(path.name.encode('utf-8') + b'\0' + path.read_bytes().replace(b'\r\n', b'\n') + b'\0')
    return h.hexdigest()[:12]


BUILD = {'code': code_id(), 'started': time.time(), 'folder': str(REPO)}
STATE = {'version': 0, 'updated': None, 'source': None, 'obs': {'baro_m': None, 'locks': []}, 'phone': None, 'route': None}
GUARD = threading.Lock()
CHANGED = threading.Condition(GUARD)


def clean_obs(obs: dict) -> dict:
    locks = []
    for lock in (obs.get('locks') or [])[:12]:
        heading = float(lock['heading_deg'])
        if not math.isfinite(heading):
            raise ValueError('heading')
        elevation = lock.get('elevation_deg')
        locks.append({
            'peak_id': str(lock['peak_id'])[:16],
            'heading_deg': heading % 360.0,
            'elevation_deg': float(elevation) if isinstance(elevation, (int, float)) and math.isfinite(elevation) else None,
            'at': str(lock.get('at', ''))[:40],
        })
    baro = obs.get('baro_m')
    return {'baro_m': float(baro) if isinstance(baro, (int, float)) and math.isfinite(baro) else None, 'locks': locks}


def finite(v):
    return float(v) if isinstance(v, (int, float)) and math.isfinite(v) else None


def clean_route(route) -> dict | None:
    """The route the phone planned (team8_nav.js), kept small and numeric for the board."""
    if not isinstance(route, dict):
        return None
    dest = route.get('dest') if isinstance(route.get('dest'), dict) else {}
    lon, lat = finite(dest.get('lon')), finite(dest.get('lat'))
    runs = []
    for run in (route.get('runs') or [])[:200]:
        if not isinstance(run, dict):
            continue
        pts = [[float(p[0]), float(p[1])] for p in (run.get('path') or [])[:3000]
               if isinstance(p, (list, tuple)) and len(p) >= 2 and finite(p[0]) is not None and finite(p[1]) is not None]
        runs.append({'cls': int(finite(run.get('cls')) or 0) % 3, 'path': pts})
    return {
        'dest': {'lon': lon, 'lat': lat, 'label': str(dest.get('label', ''))[:40]} if lon is not None and lat is not None else None,
        'health': str(route.get('health', ''))[:12], 'cap_deg': finite(route.get('cap_deg')), 'stay': bool(route.get('stay')),
        'say': str(route.get('say') or '')[:400], 'length_m': finite(route.get('length_m')), 'time_s': finite(route.get('time_s')),
        'reached': bool(route.get('reached')), 'runs': runs,
    }


class Atak:
    """Feeds ATAK with Cursor on Target, off the request threads so the Lock relay to the board stays fast.

    One watcher thread wakes on every STATE change (and every RESEND_S), rebuilds the events with cot.py
    and writes them to every connected ATAK (TAK Server mode) and to the push target (push mode).
    Uids that disappear get a t-x-d-d delete event.
    """
    RESEND_S = 15.0
    IO_TIMEOUT_S = 5.0
    PUSH_TIMEOUT_S = 2.0
    MAX_PENDING = 1 << 20    # bytes of unfinished input kept per client

    def __init__(self, host: str, port: int, push: tuple[str, int] | None = None):
        self.host, self.port, self.push = host, port, push
        self.lock = threading.Lock()
        self.clients = {}        # id -> {'sock', 'peer', 'wlock'}
        self.current = []        # the events ATAK should hold now (xml strings)
        self.uids = []
        self.fix = None
        self.fix_key = None      # cot.position_key of the fix ATAK holds
        self.fix_at = None       # when that position first arrived, so ATAK can read its age
        self.listening = False
        self.sent = 0
        self.last_sent = None
        self.last_error = None
        self.last_error_at = None
        self.bind_error = None
        self.push_ok_at = None
        self._push_cv = threading.Condition()
        self._push_deletes = []
        self._push_due = False
        self._ids = itertools.count(1)

    # ---- threads ----
    def start(self) -> None:
        if self.port:
            threading.Thread(target=self._listen, name='atak-listen', daemon=True).start()
        if self.push:
            threading.Thread(target=self._push_loop, name='atak-push', daemon=True).start()
        threading.Thread(target=self._watch, name='atak-watch', daemon=True).start()

    def _watch(self) -> None:
        seen, last_full = None, 0.0
        while True:
            with CHANGED:
                CHANGED.wait_for(lambda: STATE['version'] != seen,
                                 timeout=max(0.05, last_full + self.RESEND_S - time.monotonic()))
                changed = STATE['version'] != seen
                seen = STATE['version']
                # POST replaces these values and never edits them in place, so the references stay valid
                # after the lock is released, and the request threads wait for nothing here
                state = {k: STATE[k] for k in ('source', 'obs', 'phone', 'route')}
            try:
                snap = cot.snapshot(state) if changed else cot.KEEP
                if self._tick(snap, time.monotonic() - last_full >= self.RESEND_S):
                    last_full = time.monotonic()
            except Exception as error:   # the ATAK feed must never take the server down
                self._error(f'build: {error!r}')
                last_full = time.monotonic()

    def _tick(self, snap, periodic: bool) -> bool:
        """Send when the fix changed or a resend is due. True when a full set went out."""
        changed = snap is not cot.KEEP and snap != self.fix
        if changed:
            self.fix = snap
            key = cot.position_key(snap)
            if key != self.fix_key:
                self.fix_key, self.fix_at = key, time.time()
        if not changed and not periodic:
            return False
        now = time.time()
        events = cot.build(self.fix, now, fix_at=self.fix_at)
        keep = {uid for uid, _ in events}
        gone = [uid for uid in self.uids if uid not in keep]
        payload = [cot.delete_event(uid, now) for uid in gone] + [x for _, x in events]
        with self.lock:
            self.uids = [uid for uid, _ in events]
            self.current = [x for _, x in events]
            clients = list(self.clients.values())
        for client in clients:
            self._send(client, payload)
        if self.push and (payload or gone):
            with self._push_cv:
                self._push_deletes += [u for u in gone if u not in self._push_deletes]
                self._push_due = True
                self._push_cv.notify()
        return True

    def _listen(self) -> None:
        try:
            srv = socket.create_server((self.host, self.port))
        except OSError as error:
            self.bind_error = f'CoT port {self.port} not open: {why(error)}'
            self._error(self.bind_error)
            print(self.bind_error, flush=True)
            return
        self.listening = True
        while True:
            try:
                sock, addr = srv.accept()
            except OSError as error:
                self._error(f'accept: {why(error)}')
                time.sleep(0.5)
                continue
            sock.settimeout(self.IO_TIMEOUT_S)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            client = {'sock': sock, 'peer': f'{addr[0]}:{addr[1]}', 'wlock': threading.Lock(), 'id': next(self._ids)}
            # register and send the current set under the client's write lock, so a change that lands
            # meanwhile reaches this client after the older set, never before it
            with client['wlock']:
                with self.lock:
                    self.clients[client['id']] = client
                    first = list(self.current)
                self._write(client, first)
            threading.Thread(target=self._read, args=(client,), name='atak-read', daemon=True).start()

    def _read(self, client: dict) -> None:
        """Read and drop what ATAK sends (its own position and so on), answer pings with a pong."""
        buf = b''
        try:
            while True:
                try:
                    data = client['sock'].recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    break
                msgs, buf = cot.split_stream(buf + data)
                if any(cot.is_ping(m) for m in msgs):
                    self._send(client, [cot.pong(time.time())], count=False)
                if len(buf) > self.MAX_PENDING:
                    buf = b''
        except OSError:
            pass
        self._drop(client)

    def _push_loop(self) -> None:
        """Push mode: an ATAK TCP input reads one event per connection, so each event gets its own."""
        host, port = self.push
        while True:
            with self._push_cv:
                self._push_cv.wait_for(lambda: self._push_due)
                deletes, self._push_deletes, self._push_due = self._push_deletes, [], False
            now = time.time()
            with self.lock:
                payload = [cot.delete_event(uid, now) for uid in deletes] + list(self.current)
            done = 0
            for event in payload:
                try:
                    with socket.create_connection((host, port), timeout=self.PUSH_TIMEOUT_S) as s:
                        s.sendall(event.encode('utf-8'))
                        s.shutdown(socket.SHUT_WR)
                    done += 1
                except OSError as error:
                    self._error(f'push {host}:{port}: {why(error)}')
                    break
            with self.lock:
                self.sent += done
                if done:
                    self.last_sent = time.time()
                if done == len(payload):
                    self.push_ok_at = time.time()
                    self._clear_error('push')

    # ---- sending ----
    def _send(self, client: dict, events: list, count: bool = True) -> None:
        with client['wlock']:
            self._write(client, events, count)

    def _write(self, client: dict, events: list, count: bool = True) -> None:
        """Write events back to back. ATAK splits the stream at </event>, so nothing goes between them."""
        if not events:
            return
        try:
            client['sock'].sendall(''.join(events).encode('utf-8'))
        except OSError:   # ATAK went away (closed, reset, or stopped reading): drop it, not an error
            self._drop(client)
            return
        if count:
            with self.lock:
                self.sent += len(events)
                self.last_sent = time.time()

    def _drop(self, client: dict) -> None:
        with self.lock:
            self.clients.pop(client['id'], None)
        try:
            client['sock'].close()
        except OSError:
            pass

    def _error(self, message: str) -> None:
        with self.lock:
            self.last_error, self.last_error_at = message[:200], time.time()

    def _clear_error(self, kind: str) -> None:
        """A later success clears an old push error (the board shows last_error in red). Call under lock."""
        if self.last_error and self.last_error.startswith(kind) and self.last_error != self.bind_error:
            self.last_error = self.last_error_at = None

    # ---- HTTP views ----
    def cot_text(self) -> str:
        with self.lock:
            return ''.join(x + '\n' for x in self.current)

    def status(self) -> dict:
        now = time.time()
        with self.lock:
            pushing = bool(self.push_ok_at and now - self.push_ok_at < 2 * self.RESEND_S + 5)
            return {
                'mode': '+'.join(m for m, on in (('server', bool(self.port)), ('push', bool(self.push))) if on) or 'off',
                'clients': len(self.clients),
                'connected': bool(self.clients) or pushing,
                'sent': self.sent,
                'last_sent': self.last_sent,
                'last_error': self.last_error,
                'last_error_at': self.last_error_at,
                'listening': self.listening,
                'cot_port': self.port or None,
                'push': f'{self.push[0]}:{self.push[1]}' if self.push else None,
                'peers': [c['peer'] for c in self.clients.values()],
                'events': len(self.current),
                'uids': list(self.uids),
            }


ATAK: Atak | None = None


def why(error: OSError) -> str:
    """A short English reason (Windows gives localized messages) for the board's ATAK line."""
    for kind, name in ((ConnectionRefusedError, 'connection refused'), (ConnectionResetError, 'connection reset'),
                       (ConnectionAbortedError, 'connection aborted'), (BrokenPipeError, 'connection closed'),
                       (TimeoutError, 'timed out')):
        if isinstance(error, kind):
            return name
    if error.errno in (errno.EADDRINUSE, 10048) or getattr(error, 'winerror', None) == 10048:
        return 'port already in use'
    if error.errno in (errno.EACCES, 10013) or getattr(error, 'winerror', None) == 10013:
        return 'port not allowed'
    return f'{type(error).__name__} {error.errno}'


def host_port(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(':')
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise argparse.ArgumentTypeError('use HOST:PORT, for example 127.0.0.1:4242')
    return host.strip('[]'), int(port)


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if self.command == 'POST':
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode('utf-8'), 'application/json')

    def _file(self, path: Path) -> None:
        if not path.is_file():
            self._send(404, b'not found', 'text/plain')
            return
        self._send(200, path.read_bytes(), TYPES.get(path.suffix.lower(), 'application/octet-stream'))

    def do_GET(self):  # noqa: N802
        path, _, query = self.path.partition('?')
        if path == '/api/state':
            with GUARD:
                body = json.dumps(STATE)
            self._send(200, body.encode('utf-8'), 'application/json')
            return
        if path == '/api/wait':
            # long poll for the board: returns as soon as the version differs from ?v=, or after 20 s
            since = int(parse_qs(query).get('v', ['-1'])[0] or -1)
            with CHANGED:
                CHANGED.wait_for(lambda: STATE['version'] != since, timeout=20)
                body = json.dumps(STATE)
            self._send(200, body.encode('utf-8'), 'application/json')
            return
        if path == '/api/cot':
            # the CoT events ATAK should hold now, one per line (empty when there is no fix)
            self._send(200, (ATAK.cot_text() if ATAK else '').encode('utf-8'), 'text/plain; charset=utf-8')
            return
        if path == '/api/build':
            self._json(BUILD)
            return
        if path == '/api/atak':
            self._json(ATAK.status() if ATAK else {'mode': 'off', 'clients': 0, 'connected': False, 'sent': 0,
                                                   'last_sent': None, 'last_error': None})
            return
        if path in FILES:
            self._file(FILES[path])
            return
        for prefix, base in DIRS.items():
            if path.startswith(prefix):
                target = (base / path[len(prefix):]).resolve()
                if base.resolve() in target.parents:
                    self._file(target)
                    return
        self._send(404, b'not found', 'text/plain')

    def do_POST(self):  # noqa: N802
        path = self.path.split('?', 1)[0]
        try:
            length = min(int(self.headers.get('Content-Length', 0)), 65536)
            body = json.loads(self.rfile.read(length) or b'{}')
            if path == '/api/state':
                obs = clean_obs(body.get('obs') or {})
                with CHANGED:
                    STATE['version'] += 1
                    STATE['updated'] = time.time()
                    STATE['source'] = str(body.get('source', 'phone'))[:16]
                    STATE['obs'] = obs
                    STATE['phone'] = body.get('summary') if isinstance(body.get('summary'), dict) else None
                    STATE['route'] = clean_route(body.get('route'))
                    version = STATE['version']
                    CHANGED.notify_all()
                self._json({'ok': True, 'version': version})
                return
            if path == '/api/reset':
                with CHANGED:
                    STATE['version'] += 1
                    STATE['updated'] = time.time()
                    STATE['source'] = 'reset'
                    STATE['obs'] = {'baro_m': None, 'locks': []}
                    STATE['phone'] = None
                    STATE['route'] = None
                    version = STATE['version']
                    CHANGED.notify_all()
                self._json({'ok': True, 'version': version})
                return
        except (ValueError, KeyError, TypeError) as error:
            self._json({'ok': False, 'error': str(error)}, 400)
            return
        self._send(404, b'not found', 'text/plain')


def main() -> None:
    global ATAK
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8768)
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--cot-port', type=int, default=8087,
                    help='TCP port ATAK connects to as a TAK Server (plain TCP, no SSL). 0 turns it off.')
    ap.add_argument('--atak-push', type=host_port, default=None, metavar='HOST:PORT',
                    help='also push every change to an ATAK TCP input, e.g. 127.0.0.1:4242 after adb forward tcp:4242 tcp:4242')
    args = ap.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    ATAK = Atak(args.host, args.cot_port, args.atak_push)
    ATAK.start()
    print(f'RIDGELINE on http://localhost:{args.port}  (board: /board)', flush=True)
    if args.cot_port:
        print(f'ATAK: TAK Server (TCP, no SSL) on {args.host}:{args.cot_port}  (phone: adb reverse tcp:{args.cot_port} tcp:{args.cot_port})', flush=True)
    if args.atak_push:
        print(f'ATAK: pushing to {args.atak_push[0]}:{args.atak_push[1]}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
