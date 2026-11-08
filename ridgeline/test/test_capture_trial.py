"""Fixture-only checks for the local trial log. No live server is contacted."""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "capture_trial.py"
SPEC = importlib.util.spec_from_file_location("capture_trial", SCRIPT)
trial = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(trial)
MODEL = {"area": "fixture", "demo": {"lat": 43.85, "lon": -110.78}}
STATE = {"version": 3, "updated": 1000, "source": "phone",
         "obs": {"baro_m": 2446.1, "locks": [{"peak_id": str(i), "at": f"lock-{i}",
                                           "heading_deg": 30 + i * 25, "elevation_deg": 10}
                                          for i in range(3)]},
         "phone": {"n": 3, "status": "enough", "lat": 43.85, "lon": -110.78, "r95_m": 280}}


def record(state=STATE, **kwargs):
    opts = dict(trial_id="b-01", condition="wall fixture", input_mode="sensor", reference="wall-cards")
    opts.update(kwargs)
    return trial.make_record(state, MODEL, **opts)


class CaptureTrialTest(unittest.TestCase):
    def test_wall_comparison_and_declarations(self):
        r = record()
        self.assertEqual(r["wall_reference_error_m"], 0)
        self.assertTrue(r["inside_reported_95_circle"])
        self.assertEqual(r["input_mode_basis"], "operator_declared")
        self.assertEqual(r["barometer_mode"], "simulated")
        self.assertEqual(r["lock_count"], 3)
        self.assertIsNone(r["operator_completion_time_s"])
        self.assertIsNone(r["phone_to_board_latency_ms"])
        self.assertEqual(r["hpd_region_membership"], "not_measured")

    def test_outside_circle_is_retained(self):
        state = copy.deepcopy(STATE)
        state["phone"]["lat"] += 0.01
        r = record(state)
        self.assertGreater(r["wall_reference_error_m"], 1000)
        self.assertFalse(r["inside_reported_95_circle"])
        self.assertTrue(r["new_attempt"])

    def test_no_fix_and_mismatch_are_retained(self):
        empty = record({"obs": {"locks": []}})
        self.assertEqual(empty["capture_status"], "no_fix")
        self.assertIsNone(empty["mismatch"])
        state = copy.deepcopy(STATE)
        state["phone"]["status"] = "mismatch"
        r = record(state)
        self.assertTrue(r["mismatch"])
        self.assertEqual(r["capture_status"], "mismatch")
        self.assertTrue(r["new_attempt"])

    def test_stale_summary_is_not_a_fix(self):
        state = copy.deepcopy(STATE)
        state["phone"]["n"] = 2
        self.assertEqual(record(state)["capture_status"], "no_fix")

    def test_non_wall_reference_does_not_invent_accuracy(self):
        self.assertIsNone(record(reference="none")["wall_reference_error_m"])
        state = copy.deepcopy(STATE)
        state["source"] = "board-sim"
        self.assertIsNone(record(state)["wall_reference_error_m"])

    def test_duplicates_are_logged_but_not_new_attempts(self):
        first = record()
        again = record(trial_id="b-02", previous=[first])
        self.assertFalse(again["new_attempt"])
        self.assertEqual(again["duplicate_of"], "b-01")
        self.assertIn("state_fingerprint_repeated", again["duplicate_reasons"])
        state = copy.deepcopy(STATE)
        state.update(version=4, updated=1001)
        changed_route_only = record(state, trial_id="b-03", previous=[first])
        self.assertIn("observations_fingerprint_repeated", changed_route_only["duplicate_reasons"])
        state["obs"]["locks"][0]["at"] = "new-lock"
        self.assertTrue(record(state, trial_id="b-04", previous=[first])["new_attempt"])

    def test_settings_and_new_labels_cannot_inflate_trial_count(self):
        first = record()
        mutations = {
            "gps": lambda s: s["phone"].update(gps={"lat": 43.9, "lon": -110.7, "verdict": "REJECTED"}),
            "marks": lambda s: s["phone"].update(marks=[{"kind": "HIDE", "lat": 43.86, "lon": -110.77}]),
            "health": lambda s: s.update(route={"health": "injured", "cap_deg": 18}),
            "barometer": lambda s: s["obs"].update(baro_m=2500),
            "resolve": lambda s: s["phone"].update(lat=43.86, lon=-110.79, r95_m=400),
        }
        for name, mutate in mutations.items():
            with self.subTest(setting=name):
                state = copy.deepcopy(STATE)
                state.update(version=4, updated=1001)
                mutate(state)
                r = record(state, trial_id="another-" + name, condition="renamed condition",
                           input_mode="keyboard", notes="changed notes", previous=[first])
                self.assertFalse(r["new_attempt"])
                self.assertEqual(r["duplicate_of"], "b-01")
                self.assertIn("observations_fingerprint_repeated", r["duplicate_reasons"])

    def test_new_lock_timestamp_counts_even_with_identical_values(self):
        first = record()
        state = copy.deepcopy(STATE)
        state.update(version=4, updated=1001)
        state["obs"]["locks"][2]["at"] = "fresh-lock-2"
        r = record(state, trial_id="b-02", previous=[first])
        self.assertTrue(r["new_attempt"])
        self.assertIsNone(r["duplicate_of"])
        self.assertNotEqual(r["observations_fingerprint"], first["observations_fingerprint"])

    def test_network_error_and_no_fix_append_without_erasing_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model.json"
            model.write_text(json.dumps(MODEL), encoding="utf-8")
            args = argparse.Namespace(model=model, output=root / "log.jsonl", trial_id="b-01",
                                      condition="fixture", input_mode="keyboard", reference="wall-cards",
                                      notes="No phone was used", barometer_mode="simulated",
                                      server="http://localhost:8768", timeout=1)
            def fail(*_):
                raise ConnectionRefusedError("fixture offline")
            trial.capture(args, fetcher=fail)
            args.trial_id = "b-02"
            trial.capture(args, fetcher=lambda *_: {"obs": {"locks": []}})
            args.trial_id = "b-03"
            trial.capture(args, fetcher=lambda *_: STATE)
            rows = trial.load_records(args.output)
            self.assertEqual([r["capture_status"] for r in rows], ["communication_error", "no_fix", "fix"])
            self.assertEqual([r["attempt_index"] for r in rows], [1, 2, 3])
            self.assertTrue(all(r["new_attempt"] for r in rows))
            self.assertEqual(rows[2]["input_mode"], "keyboard")
            args.trial_id = "b-04"
            bad = copy.deepcopy(STATE)
            bad["obs"]["baro_m"] = float("nan")
            trial.capture(args, fetcher=lambda *_: bad)
            self.assertEqual(trial.load_records(args.output)[3]["capture_status"], "invalid_state")

    def test_non_loopback_rejected_before_request(self):
        with mock.patch.object(trial.urllib.request, "build_opener") as opener:
            with self.assertRaises(ValueError):
                trial.read_state("https://example.com", 1)
            opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
