"""ATAK hand-off tests: the real server on free ports, a fix posted like the phone, the CoT read back.

Run from the repo root:
    python -I -m unittest discover -s ridgeline/test -p "test_cot.py" -v

Python standard library only. The server under test gets free ports (never 8768 or 8087), a TCP client
plays ATAK connected to a TAK Server, and a small listener plays ATAK's TCP input for push mode
(like commo, it reads one whole connection as one message).
"""
from __future__ import annotations

import base64
import importlib.util
import json
import math
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

RIDGELINE = Path(__file__).resolve().parents[1]
SERVER = RIDGELINE / 'server.py'
GRID = json.loads((RIDGELINE / 'data' / 'moran.json').read_text(encoding='utf-8'))
DEMO = GRID['demo']
R95 = 280

_spec = importlib.util.spec_from_file_location('ridgeline_cot_under_test', RIDGELINE / 'cot.py')
cot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cot)

UIDS = {'RIDGELINE-1', 'RIDGELINE-1.r95', 'RIDGELINE-1.route', 'RIDGELINE-1.hide-1', 'RIDGELINE-1.hide-2',
        'RIDGELINE-1.signal-1', 'RIDGELINE-1.gps'}


# ---------- what the phone posts ----------

def route_like_phone() -> dict:
    """routeForBoard() shape: runs of [lon, lat] paths that share their joints."""
    lat0, lon0 = DEMO['lat'], DEMO['lon']
    path = [[round(lon0 + 0.0012 * i, 6), round(lat0 - 0.0004 * i - 0.00002 * i * i, 6)] for i in range(41)]
    return {'dest': {'lon': path[-1][0], 'lat': path[-1][1], 'label': 'Rendezvous'}, 'health': 'healthy', 'cap_deg': 30,
            'stay': False, 'say': 'Head east down the drainage, then follow the lakeshore.', 'length_m': 4100,
            'time_s': 5400, 'reached': True, 'runs': [{'cls': 0, 'path': path[:21]}, {'cls': 2, 'path': path[20:]}]}


def phone_body(n: int = 3, status: str = 'enough', summary: bool = True, extras: bool = True, route: bool = True) -> dict:
    locks = [{'peak_id': w['peak_id'], 'heading_deg': (w['bearing_deg'] + 7.0) % 360, 'elevation_deg': w['elevation_deg'],
              'at': f'2026-11-08T08:00:0{i}Z'} for i, w in enumerate(DEMO['wall'][:n])]
    s = {'status': status, 'r95_m': R95, 'lat': DEMO['lat'], 'lon': DEMO['lon'], 'n': n}
    if extras:
        s['gps'] = {'lat': DEMO['lat'] + 0.02, 'lon': DEMO['lon'] + 0.01, 'verdict': 'REJECTED', 'dist_m': 2300}
        s['marks'] = [
            {'kind': 'HIDE', 'rank': 2, 'lat': DEMO['lat'] + 0.001, 'lon': DEMO['lon'] - 0.001, 'why': 'Boulder field, out of the wind'},
            {'kind': 'HIDE', 'rank': 1, 'lat': DEMO['lat'] - 0.001, 'lon': DEMO['lon'] + 0.002, 'why': 'Tree line <30 m> & shade\x07'},
            {'kind': 'SIGNAL', 'rank': 1, 'lat': DEMO['lat'] + 0.002, 'lon': DEMO['lon'] + 0.001, 'why': 'Open ridge, seen from the valley'},
        ]
        s['mgrs'] = '12T VP 2345 5678'
    return {'source': 'phone', 'obs': {'baro_m': DEMO['baro_m'], 'locks': locks},
            'summary': s if summary else None, 'route': route_like_phone() if route else None}


# ---------- helpers ----------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def ground_m(lat: float, lon: float) -> float:
    """Independent decode of data/moran.json, the solver.js way (east and north meters, then the cell)."""
    w, s, e, n = GRID['bounds']
    nx, ny, dx, dy = GRID['nx'], GRID['ny'], GRID['dx_m'], GRID['dy_m']
    east, north = (lon - w) / (e - w) * nx * dx, (lat - s) / (n - s) * ny * dy
    i = math.floor(ny - north / dy) * nx + math.floor(east / dx)
    raw = base64.b64decode(GRID['elev_dm_u16'])
    return int.from_bytes(raw[2 * i:2 * i + 2], 'little') / 10


def http(url: str, body=None, timeout: float = 5.0) -> bytes:
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def by_uid(events) -> dict:
    return {e.get('uid'): e for e in events}


def deleted(events) -> set:
    out = set()
    for e in events:
        if e.get('type') == 't-x-d-d':
            assert e.find('detail/__forcedelete') is not None, 'delete without __forcedelete'
            link = e.find('detail/link')
            assert link.get('relation') and link.get('type'), 'ATAK ignores a delete link without relation and type'
            out.add(link.get('uid'))
    return out


class AtakClient:
    """Plays ATAK on a TAK Server stream. Every event must parse on its own (nothing between events)."""

    def __init__(self, port: int):
        self.sock = socket.create_connection(('127.0.0.1', port), timeout=5)
        self.buf, self.events = b'', []

    def read_until(self, done, timeout: float = 5.0) -> bool:
        end = time.time() + timeout
        while not done(self.events) and time.time() < end:
            self.sock.settimeout(max(0.05, end - time.time()))
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                break
            if not data:
                break
            self.buf += data
            *whole, self.buf = self.buf.split(b'</event>')
            self.events += [ET.fromstring((w + b'</event>').decode('utf-8')) for w in whole]
        return done(self.events)

    def take(self) -> list:
        out, self.events = self.events, []
        return out

    def close(self) -> None:
        self.sock.close()


class FakeTcpInput:
    """Plays ATAK's TCP input (commo TcpSocketManagement): everything one connection sends is ONE message."""

    def __init__(self):
        self.srv = socket.create_server(('127.0.0.1', 0))
        self.port = self.srv.getsockname()[1]
        self.messages, self.lock = [], threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        while True:
            try:
                sock, _ = self.srv.accept()
            except OSError:
                return
            data = b''
            with sock:
                sock.settimeout(5)
                while True:
                    try:
                        chunk = sock.recv(65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    data += chunk
            with self.lock:
                self.messages.append(data)

    def take(self) -> list:
        with self.lock:
            out, self.messages = self.messages, []
        return out


# ---------- the server, end to end ----------

class ServerToAtak(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeTcpInput()
        cls.http_port, cls.cot_port = free_port(), free_port()
        cls.log = tempfile.TemporaryFile()
        cls.proc = subprocess.Popen(
            [sys.executable, '-I', str(SERVER), '--port', str(cls.http_port), '--cot-port', str(cls.cot_port),
             '--atak-push', f'127.0.0.1:{cls.fake.port}'],
            cwd=str(RIDGELINE.parent), stdout=cls.log, stderr=subprocess.STDOUT)
        cls.url = f'http://127.0.0.1:{cls.http_port}'
        end = time.time() + 15
        while time.time() < end:
            if cls.proc.poll() is not None:
                break
            try:
                if json.loads(http(cls.url + '/api/atak', timeout=1)).get('listening'):
                    return
            except OSError:
                pass
            time.sleep(0.1)
        cls.tearDownClass()
        raise RuntimeError('server did not start')

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.fake.srv.close()
        cls.log.close()

    def setUp(self):
        http(self.url + '/api/reset', {})
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()

    def client(self) -> AtakClient:
        c = AtakClient(self.cot_port)
        self.clients.append(c)
        return c

    def post(self, body: dict) -> None:
        self.assertTrue(json.loads(http(self.url + '/api/state', body))['ok'])

    def cot_lines(self) -> list:
        text = http(self.url + '/api/cot').decode('utf-8')
        return [ET.fromstring(line) for line in text.splitlines() if line.strip()]

    def test_fix_reaches_atak_and_reset_deletes_it(self):
        a = self.client()
        self.post(phone_body())
        self.assertTrue(a.read_until(lambda ev: UIDS <= set(by_uid(ev))), f'missing {UIDS - set(by_uid(a.events))}')
        ev = by_uid(a.take())
        for e in ev.values():
            self.assertEqual(e.get('version'), '2.0')
            t0 = time.mktime(time.strptime(e.get('time')[:19], '%Y-%m-%dT%H:%M:%S'))
            t1 = time.mktime(time.strptime(e.get('stale')[:19], '%Y-%m-%dT%H:%M:%S'))
            self.assertAlmostEqual(t1 - t0, 600, delta=1)

        # the survivor: a friendly ground point, machine fused, ce = 1 sigma, hae = ground + eye on the ellipsoid
        me = ev['RIDGELINE-1']
        p = me.find('point')
        self.assertTrue(me.get('type').startswith('a-f-G'))
        self.assertEqual(me.get('how'), 'm-f')
        self.assertAlmostEqual(float(p.get('lat')), DEMO['lat'], places=6)
        self.assertAlmostEqual(float(p.get('lon')), DEMO['lon'], places=6)
        self.assertAlmostEqual(float(p.get('ce')), R95 / 2.4477, delta=0.06)
        self.assertAlmostEqual(ground_m(DEMO['lat'], DEMO['lon']), DEMO['ground_m'], delta=0.05)
        self.assertAlmostEqual(float(p.get('hae')), DEMO['ground_m'] + 1.7 + cot.GEOID_M, delta=0.06)
        self.assertEqual(me.find('detail/contact').get('callsign'), 'RIDGELINE 1')
        self.assertIn(f'Terrain fix from 3 peaks, no GPS, 95% radius {R95} m', me.find('detail/remarks').text)

        # the 95% circle: ATAK's drawing circle, radius in meters on ellipse major and minor
        ring = ev['RIDGELINE-1.r95']
        self.assertEqual(ring.get('type'), 'u-d-c-c')
        ell = ring.find('detail/shape/ellipse')
        self.assertEqual((float(ell.get('major')), float(ell.get('minor')), ell.get('angle')), (R95, R95, '360'))
        self.assertAlmostEqual(float(ring.find('point').get('lat')), DEMO['lat'], places=6)
        self.assertIsNotNone(ring.find("detail/shape/link[@type='b-x-KmlStyle']/Style/LineStyle/color"))

        # the route: an open polyline, lat,lon in the order the phone walked it
        line = ev['RIDGELINE-1.route']
        self.assertEqual(line.get('type'), 'u-d-f')
        pts = [lk.get('point') for lk in line.findall('detail/link')]
        path = [p for run in route_like_phone()['runs'] for p in run['path']]
        self.assertEqual(len(pts), 41)   # 42 posted, the joint between the two runs once
        self.assertEqual(pts[0], f'{path[0][1]:.6f},{path[0][0]:.6f}')
        self.assertEqual(pts[-1], f'{path[-1][1]:.6f},{path[-1][0]:.6f}')
        self.assertNotEqual(pts[0], pts[-1])   # equal ends would make ATAK close and fill it
        self.assertIn('Rendezvous', line.find('detail/contact').get('callsign'))

        # marks and the rejected GPS as spot markers
        names = {u: e.find('detail/contact').get('callsign') for u, e in ev.items() if e.get('type') == 'b-m-p-s-m'}
        self.assertEqual(names, {'RIDGELINE-1.hide-1': 'HIDE 1', 'RIDGELINE-1.hide-2': 'HIDE 2',
                                 'RIDGELINE-1.signal-1': 'SIGNAL 1', 'RIDGELINE-1.gps': 'GPS REJECTED'})
        hide1 = ev['RIDGELINE-1.hide-1']   # rank 1 is HIDE 1, whatever the order the phone sent them in
        self.assertAlmostEqual(float(hide1.find('point').get('lat')), DEMO['lat'] - 0.001, places=6)
        self.assertEqual(ev['RIDGELINE-1.gps'].find('detail/color').get('argb'), str(-65536))
        self.assertEqual(ev['RIDGELINE-1.gps'].get('how'), 'm-g')

        # a client that connects later gets the current set at once, and /api/cot holds the same set
        b = self.client()
        self.assertTrue(b.read_until(lambda e: UIDS <= set(by_uid(e)), timeout=2))
        self.assertEqual(set(by_uid(self.cot_lines())), UIDS)
        st = json.loads(http(self.url + '/api/atak'))
        self.assertEqual((st['clients'], st['connected']), (2, True))
        self.assertIn('server', st['mode'])
        self.assertGreaterEqual(st['sent'], 2 * len(UIDS))
        self.assertIsNone(st['last_error'])

        # ping, pong
        t = cot.cot_time(time.time())
        a.sock.sendall(f'<event version="2.0" uid="ANDROID-test-ping" type="t-x-c-t" how="m-g" time="{t}" start="{t}" '
                       f'stale="{t}"><point lat="0" lon="0" hae="0" ce="9999999" le="9999999"/><detail/></event>'.encode())
        self.assertTrue(a.read_until(lambda e: any(x.get('type') == 't-x-c-t-r' for x in e), timeout=3))
        a.take()

        # reset: every uid gets a forced delete
        http(self.url + '/api/reset', {})
        self.assertTrue(a.read_until(lambda e: deleted(e) >= UIDS), f'deleted only {deleted(a.events)}')
        end = time.time() + 3
        while self.cot_lines() and time.time() < end:
            time.sleep(0.05)
        self.assertEqual(self.cot_lines(), [])

    def test_push_mode_sends_one_event_per_connection(self):
        self.fake.take()
        self.post(phone_body())
        end, seen = time.time() + 5, {}
        while time.time() < end and not UIDS <= set(seen):
            time.sleep(0.05)
            for msg in self.fake.take():
                self.assertEqual(msg.count(b'</event>'), 1, 'an ATAK TCP input reads one event per connection')
                e = ET.fromstring(msg.decode('utf-8'))
                seen[e.get('uid')] = e
        self.assertTrue(UIDS <= set(seen), f'missing {UIDS - set(seen)}')
        self.assertIn('push', json.loads(http(self.url + '/api/atak'))['mode'])
        http(self.url + '/api/reset', {})
        end, gone = time.time() + 5, set()
        while time.time() < end and not gone >= UIDS:
            time.sleep(0.05)
            gone |= deleted(ET.fromstring(m.decode('utf-8')) for m in self.fake.take())
        self.assertTrue(gone >= UIDS, f'push deleted only {gone}')

    def test_transient_posts_keep_the_fix_and_undo_clears_it(self):
        a = self.client()
        self.post(phone_body())
        self.assertTrue(a.read_until(lambda ev: UIDS <= set(by_uid(ev))))
        a.take()
        self.post(phone_body(summary=False))         # a Lock posted before the phone solved it
        self.post(phone_body(status='mismatch'))     # the phone asks for that card again
        self.post({'source': 'board-sim', 'obs': phone_body()['obs']})   # a simulated drop on the board
        a.read_until(lambda ev: False, timeout=1.0)
        self.assertEqual(deleted(a.take()), set())
        self.assertEqual(set(by_uid(self.cot_lines())), UIDS)
        self.post(phone_body(n=1, status='more'))    # undo down to one Lock: no fix any more
        self.assertTrue(a.read_until(lambda ev: deleted(ev) >= UIDS))

    def test_only_what_left_is_deleted(self):
        a = self.client()
        self.post(phone_body())
        self.assertTrue(a.read_until(lambda ev: UIDS <= set(by_uid(ev))))
        a.take()
        body = phone_body(route=False)
        body['summary']['marks'] = body['summary']['marks'][1:2]   # only the rank 1 HIDE stays
        body['summary']['gps']['verdict'] = 'AGREE'
        body['summary']['r95_m'] = 150
        self.post(body)
        self.assertTrue(a.read_until(lambda ev: 'RIDGELINE-1' in by_uid(ev) and deleted(ev)))
        a.read_until(lambda ev: False, timeout=0.3)
        ev = a.take()
        self.assertEqual(deleted(ev), {'RIDGELINE-1.route', 'RIDGELINE-1.hide-2', 'RIDGELINE-1.signal-1', 'RIDGELINE-1.gps'})
        self.assertAlmostEqual(float(by_uid(ev)['RIDGELINE-1'].find('point').get('ce')), 150 / 2.4477, delta=0.06)
        self.assertEqual(float(by_uid(ev)['RIDGELINE-1.r95'].find('detail/shape/ellipse').get('major')), 150.0)

    def test_lock_relay_to_the_board_stays_fast(self):
        a = self.client()
        self.post(phone_body())
        self.assertTrue(a.read_until(lambda ev: UIDS <= set(by_uid(ev))))
        worst = 0.0
        for i in range(5):
            version = json.loads(http(self.url + '/api/state'))['version']
            got = {}

            def wait():
                http(f'{self.url}/api/wait?v={version}', timeout=25)
                got['t'] = time.perf_counter()

            th = threading.Thread(target=wait)
            th.start()
            time.sleep(0.15)   # the board's long poll is waiting
            t0 = time.perf_counter()
            self.post(phone_body(n=2 + i % 2, status='more' if i % 2 == 0 else 'enough'))
            th.join(5)
            worst = max(worst, got['t'] - t0)
        self.assertLess(worst, 0.25, f'Lock to board took {worst:.3f} s')


# ---------- cot.py on its own ----------

class CotEvents(unittest.TestCase):
    def state(self, **kw) -> dict:
        b = phone_body(**kw)
        return {'source': b['source'], 'obs': b['obs'], 'phone': b['summary'], 'route': b['route']}

    def test_snapshot_rules(self):
        self.assertIsNone(cot.snapshot({'source': 'reset', 'obs': {'locks': []}, 'phone': None, 'route': None}))
        self.assertIsNone(cot.snapshot(dict(self.state(), obs={'baro_m': None, 'locks': []})))
        self.assertIs(cot.snapshot(self.state(summary=False)), cot.KEEP)
        self.assertIs(cot.snapshot(self.state(status='mismatch')), cot.KEEP)
        self.assertIsNone(cot.snapshot(self.state(n=1, status='more')))
        fix = cot.snapshot(self.state(n=2, status='more'))
        self.assertEqual((fix['n'], fix['r95']), (2, R95))

    def test_every_event_parses_and_escapes_text(self):
        fix = cot.snapshot(self.state())
        events = cot.build(fix, time.time())
        self.assertEqual({u for u, _ in events}, UIDS)
        for _, x in events:
            ET.fromstring(x)
        hide1 = ET.fromstring(dict(events)['RIDGELINE-1.hide-1'])
        self.assertEqual(hide1.find('detail/remarks').text, 'Tree line <30 m> & shade')
        ET.fromstring(cot.delete_event('RIDGELINE-1', time.time()))
        ET.fromstring(cot.pong(time.time()))

    def test_long_route_fits_the_atak_stream_buffer(self):
        st = self.state()
        path = [[DEMO['lon'] + 0.00002 * i, DEMO['lat'] + 0.00001 * math.sin(i / 9)] for i in range(2600)]
        st['route']['runs'] = [{'cls': 1, 'path': path}]
        x = dict(cot.build(cot.snapshot(st), time.time()))['RIDGELINE-1.route']
        links = ET.fromstring(x).findall('detail/link')
        self.assertLessEqual(len(links), cot.ROUTE_MAX_POINTS)
        self.assertLess(len(x.encode('utf-8')), 48000)
        self.assertEqual(links[0].get('point'), f'{path[0][1]:.6f},{path[0][0]:.6f}')
        self.assertEqual(links[-1].get('point'), f'{path[-1][1]:.6f},{path[-1][0]:.6f}')

    def test_no_terrain_means_unknown_height(self):
        far = cot.snapshot(dict(self.state(extras=False, route=False),
                                phone={'status': 'enough', 'r95_m': R95, 'lat': 40.0, 'lon': -100.0, 'n': 3}))
        me = ET.fromstring(dict(cot.build(far, time.time()))['RIDGELINE-1'])
        self.assertEqual(float(me.find('point').get('hae')), 9999999.0)


if __name__ == '__main__':
    unittest.main()
