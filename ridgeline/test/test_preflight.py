"""Tests for the pre-demo check's parsers (no phone, adb or server needed).

Run from the repo root:
    python -I -m unittest discover -s ridgeline/test -p "test_preflight.py" -v
"""
from __future__ import annotations

import importlib.util
import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

RIDGELINE = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('ridgeline_preflight', RIDGELINE / 'scripts' / 'preflight.py')
pf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pf)
_spec = importlib.util.spec_from_file_location('ridgeline_server_module', RIDGELINE / 'server.py')
srv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(srv)


class Parsers(unittest.TestCase):
    def test_devices(self):
        out = '* daemon started successfully\nList of devices attached\nR3CX106B0ZH\tdevice\nemulator-5554\tunauthorized\n\n'
        self.assertEqual(pf.parse_devices(out), [('R3CX106B0ZH', 'device'), ('emulator-5554', 'unauthorized')])
        self.assertEqual(pf.parse_devices('List of devices attached\n\n'), [])

    def test_reverse(self):
        out = 'UsbFfs tcp:8768 tcp:8768\nUsbFfs tcp:8087 tcp:8087\n'
        self.assertTrue(pf.has_reverse(out, 8768))
        self.assertTrue(pf.has_reverse(out, 8087))
        self.assertFalse(pf.has_reverse(out, 8779))
        self.assertFalse(pf.has_reverse('UsbFfs tcp:8768 tcp:9000\n', 8768))   # mapped to another PC port
        self.assertFalse(pf.has_reverse('', 8768))

    def test_battery_ignores_the_change_log(self):
        out = ('Current Battery Service state:\n  AC powered: false\n  USB powered: true\n  level: 99\n'
               '  Capacity level: -1\n11-08 05:10:34.186  Sending ACTION_BATTERY_CHANGED: level:93, status:2\n')
        self.assertEqual(pf.parse_battery(out), (99, True))
        self.assertEqual(pf.parse_battery('  AC powered: false\n  USB powered: false\n  level: 15\n'), (15, False))
        self.assertEqual(pf.parse_battery(''), (None, False))

    def test_last_run(self):
        st = {'updated': 1000.0, 'source': 'phone', 'obs': {'locks': [{}, {}, {}]}}
        self.assertEqual(pf.last_run(st, 1600.0), (3, 'phone', 600.0))
        self.assertEqual(pf.last_run({'obs': {'locks': []}}, 0.0), (0, None, None))
        self.assertEqual(pf.ago(600.0), '10 min ago')
        self.assertEqual(pf.ago(30.0), '30 s ago')

    def test_no_server_is_a_fail_with_the_start_command(self):
        rows = pf.run(port=9, cot_port=9)   # nothing listens on the discard port
        server = [r for r in rows if r[1] == 'server'][0]
        self.assertEqual(server[0], pf.FAIL)
        self.assertIn('python -I ridgeline/server.py', server[2])

    def test_build_check_catches_a_server_on_old_code(self):
        now = 10_000.0
        build = {'code': 'abc', 'started': now - 600, 'folder': 'C:/elsewhere/Team-8'}
        level, say = pf.check_build(None, 'abc', now)          # server from before /api/build existed
        self.assertEqual(level, pf.FAIL)
        self.assertIn('python -I ridgeline/server.py', say)
        self.assertEqual(pf.check_build(build, 'abc', now),
                         (pf.PASS, 'the server runs the code in this folder (started 10 min ago)'))
        level, say = pf.check_build(build, 'def', now)         # pulled or edited after the server started
        self.assertEqual(level, pf.FAIL)
        self.assertIn('started from C:/elsewhere/Team-8', say)
        self.assertIn('python -I ridgeline/server.py', say)
        self.assertEqual(pf.check_build(build, None, now)[0], pf.WARN)

    def test_code_id_ignores_line_endings_and_sees_edits(self):
        with tempfile.TemporaryDirectory() as d:
            a, c = Path(d) / 'server.py', Path(d) / 'cot.py'
            a.write_bytes(b'x = 1\n')
            c.write_bytes(b'y = 2\n')
            first = srv.code_id((a, c))
            a.write_bytes(b'x = 1\r\n')
            self.assertEqual(srv.code_id((a, c)), first)
            c.write_bytes(b'y = 3\n')
            self.assertNotEqual(srv.code_id((a, c)), first)
        self.assertEqual(pf.repo_code_id(), srv.code_id())


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class AgainstARealServer(unittest.TestCase):
    """The server from this folder on a free port (never 8768), CoT off."""

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.log = tempfile.TemporaryFile()
        cls.proc = subprocess.Popen([sys.executable, '-I', str(RIDGELINE / 'server.py'), '--port', str(cls.port),
                                     '--cot-port', '0'], cwd=str(RIDGELINE.parent), stdout=cls.log,
                                    stderr=subprocess.STDOUT)
        for _ in range(100):
            if pf.get(f'http://127.0.0.1:{cls.port}/api/state', timeout=0.5)[0] == 200:
                break
            time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.log.close()

    def test_build_reports_the_code_on_disk(self):
        with urllib.request.urlopen(f'http://127.0.0.1:{self.port}/api/build', timeout=3) as r:
            build = json.loads(r.read())
        self.assertEqual(build['code'], srv.code_id())
        self.assertEqual(Path(build['folder']), RIDGELINE.parent)

    def test_preflight_passes_the_server_and_its_code(self):
        rows = {what: level for level, what, _ in pf.server_checks(self.port)}
        self.assertEqual(rows['server'], pf.PASS)
        self.assertEqual(rows['code'], pf.PASS)
        self.assertEqual(rows['last run'], pf.PASS)
        self.assertEqual(rows['atak feed'], pf.INFO)            # --cot-port 0


if __name__ == '__main__':
    unittest.main()
