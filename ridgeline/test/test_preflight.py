"""Tests for the pre-demo check's parsers (no phone, adb or server needed).

Run from the repo root:
    python -I -m unittest discover -s ridgeline/test -p "test_preflight.py" -v
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    'ridgeline_preflight', Path(__file__).resolve().parents[1] / 'scripts' / 'preflight.py')
pf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pf)


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


if __name__ == '__main__':
    unittest.main()
