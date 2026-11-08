"""Protocol and loopback integration checks, not hardware qualification."""
import importlib.util
import json
import math
from pathlib import Path
import threading
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("box_server", ROOT / "box_server.py")
box = importlib.util.module_from_spec(spec)
spec.loader.exec_module(box)


class Protocol(unittest.TestCase):
    def setUp(self):
        self.scene = box.load_scene(ROOT / "box_scene.json")
        self.b = box.Bridge(self.scene, demo=True)
        self.t = 0

    def feed(self, **kw):
        self.t += 100
        d = dict(t=self.t, yaw=350, pitch=14, btn=0, long=0, gps=0)
        d.update(kw)
        return self.b.ingest(json.dumps(d))

    def calibrate(self):
        self.feed(long=1)
        self.feed()

    def test_short_before_calibration_is_not_lock(self):
        self.feed(btn=1)
        self.assertEqual(self.b.locks, [])

    def test_long_only_calibrates(self):
        self.calibrate()
        self.assertEqual(self.b.locks, [])
        self.assertEqual(self.b.target()["id"], self.scene["target_order"][0])

    def test_yaw_wrap_and_actual_pitch(self):
        self.calibrate()
        self.feed(yaw=10, pitch=8.25, btn=1)
        lock = self.b.locks[0]
        self.assertAlmostEqual(lock["bearing_true"], (self.b.reference["bearing_true"] + 20) % 360)
        self.assertEqual(lock["elev_deg"], 8.25)
        self.assertEqual(set(lock), {"peak", "bearing_true", "elev_deg", "time"})

    def test_duplicate_and_held_pulses_do_not_repeat(self):
        self.calibrate()
        self.feed(btn=1)
        self.feed(t=self.t, btn=1)
        self.feed(btn=1)
        self.assertEqual(len(self.b.locks), 1)
        self.feed()
        self.feed(btn=1)
        self.assertEqual(len(self.b.locks), 2)

    def test_recalibration_clears_locks(self):
        self.calibrate()
        self.feed(btn=1)
        self.feed(long=1, yaw=40)
        self.assertEqual(self.b.locks, [])
        self.assertEqual(self.b.yaw_zero, 40)

    def test_device_restart_invalidates_calibration(self):
        self.calibrate()
        self.feed(t=0, btn=1)
        self.assertIsNone(self.b.yaw_zero)
        self.assertEqual(self.b.locks, [])

    def test_millis_wrap_preserves_calibration(self):
        self.calibrate()
        self.feed(t=0xFFFFFFF0)
        self.feed(t=30)
        self.assertEqual(self.b.yaw_zero, 350)

    def test_disconnect_invalidates(self):
        self.calibrate()
        self.b.connection(False, "test_disconnect")
        self.assertFalse(self.b.state()["calibrated"])
        self.assertFalse(self.b.state()["connected"])

    def test_three_locks_are_not_a_position_fix(self):
        self.calibrate()
        for _ in range(4):
            self.feed(btn=1)
            self.feed()
        self.assertEqual(len(self.b.locks), 3)
        self.assertEqual([v["peak"] for v in self.b.locks], [self.b.peaks[p]["name"] for p in self.scene["target_order"]])
        self.assertEqual(self.b.state()["status"], "awaiting_estimator")
        self.assertFalse(self.b.state()["position_fix"])

    def test_invalid_packets(self):
        bad = ['null', '[]', '{}', '{', 'x' * 2049]
        for extra in [dict(yaw=float('nan')), dict(pitch=91), dict(t=-1), dict(t=True), dict(btn=2), dict(btn=1, long=1), dict(yaw='4')]:
            d = dict(t=123, yaw=0, pitch=0, btn=0, long=0, gps=1)
            d.update(extra)
            bad.append(json.dumps(d))
        for raw in bad:
            with self.subTest(raw=raw[:80]):
                self.assertFalse(self.b.ingest(raw))
        self.assertEqual(self.b.errors, len(bad))

    def test_slow_client_gets_disconnected_and_snapshot_on_reconnect(self):
        q = self.b.subscribe()
        for _ in range(140):
            self.feed()
        self.assertNotIn(q, self.b.clients)
        self.assertIsNone(q.get_nowait())
        fresh = self.b.subscribe().get_nowait().decode()
        self.assertIn('event: state', fresh)

    def test_poster_geometry(self):
        r = 2.5
        for p in self.scene["peaks"]:
            theta = math.radians(p["bearing_true"] - self.b.reference["bearing_true"])
            x, y = r * math.sin(theta), r * math.cos(theta)
            self.assertAlmostEqual(math.hypot(x, y), r)
            rise = r * math.tan(math.radians(p["elev_deg"]))
            self.assertAlmostEqual(math.degrees(math.atan2(rise, math.hypot(x, y))), p["elev_deg"])
        self.assertAlmostEqual(r * math.tan(math.radians(14.654)), 0.65371790436)


class HTTP(unittest.TestCase):
    def setUp(self):
        self.b = box.Bridge(box.load_scene(ROOT / 'box_scene.json'), demo=True)
        self.server = box.Server(('127.0.0.1', 0), self.b)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)

    def test_sse_snapshot_and_lock_and_cors(self):
        req = urllib.request.Request(self.url + '/events', headers={'Origin': 'http://localhost:9000'})
        with urllib.request.urlopen(req, timeout=3) as r:
            self.assertIn('text/event-stream', r.headers['Content-Type'])
            self.assertEqual(r.headers['Access-Control-Allow-Origin'], 'http://localhost:9000')
            def event():
                lines=[]
                while True:
                    line=r.readline().decode().strip()
                    if not line and lines:
                        return '\n'.join(lines)
                    if line:
                        lines.append(line)
            self.assertIn('retry:', event())
            self.assertIn('event: state', event())
            for t, lng, btn in [(100,1,0),(200,0,0),(300,0,1)]:
                self.b.ingest(json.dumps(dict(t=t,yaw=350,pitch=14,btn=btn,long=lng,gps=0)))
            packets = []
            for _ in range(20):
                packet = event()
                packets.append(packet)
                if 'event: state\n' in packet:
                    data = json.loads(next(line[6:] for line in packet.splitlines() if line.startswith('data: ')))
                    if len(data['locks']) == 1:
                        break
            locks = [p for p in packets if 'event: lock\n' in p]
            self.assertEqual(len(locks), 1)
            self.assertIn('Pk 3666', locks[0])

    def test_exposure_is_limited(self):
        with urllib.request.urlopen(self.url + '/status') as r:
            self.assertTrue(json.load(r)['demo'])
        for path, headers, code in [('/README.md',{},404),('/../STATUS.md',{},404),('/status',{'Origin':'https://example.com'},403),('/status',{'Host':'attacker.example'},403)]:
            with self.subTest(path=path,headers=headers):
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(urllib.request.Request(self.url+path,headers=headers))
                self.assertEqual(cm.exception.code,code)


if __name__ == '__main__':
    unittest.main(verbosity=2)
