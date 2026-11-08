"""RIDGELINE local server: the phone page, the big-screen board, the solver data and the Lock relay.

Run from the repo root:   python -I ridgeline/server.py            (port 8768)
Phone over USB:           adb reverse tcp:8768 tcp:8768, then open http://localhost:8768 on the phone
Big screen on the PC:     http://localhost:8768/board

The phone posts its observations (barometer and Locks) to /api/state after every change.
The board long-polls /api/wait (it returns the moment a Lock arrives) and runs the same solver.js,
so both show the same answer.
Nothing leaves this PC. The server binds to 127.0.0.1 only.
"""
from __future__ import annotations

import argparse
import json
import math
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
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
    '/data/moran.json': HERE / 'data' / 'moran.json',
    '/topo/moran.jpg': REPO / 'terrain_viewer' / 'data' / 'moran' / 'topo.jpg',
}
DIRS = {'/phone/': HERE / 'phone', '/board/': HERE / 'board', '/cards/': HERE / 'cards',
        '/tv/': REPO / 'terrain_viewer'}   # read only: the viewer's route planner and terrain bundle
TYPES = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.json': 'application/json',
         '.jpg': 'image/jpeg', '.png': 'image/png', '.svg': 'image/svg+xml', '.css': 'text/css; charset=utf-8',
         '.webmanifest': 'application/manifest+json'}
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
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8768)
    ap.add_argument('--host', default='127.0.0.1')
    args = ap.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f'RIDGELINE on http://localhost:{args.port}  (board: /board)', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
