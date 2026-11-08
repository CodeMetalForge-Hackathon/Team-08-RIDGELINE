"""Append one RIDGELINE trial attempt to a local JSONL evidence log.

This only GETs /api/state. It does not reset the app or decide whether a trial
looked good enough to keep. Run it after every attempt, including failed ones.
The input mode and conditions are operator declarations, not sensor telemetry.
Wall-card error compares with a synthetic map point, never real-world accuracy.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import urllib.parse
import urllib.request

RIDGELINE = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = RIDGELINE / "data" / "moran.json"
DEFAULT_OUTPUT = RIDGELINE.parent / ".local" / "ridgeline-trials.jsonl"


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def distance_m(lat1, lon1, lat2, lon2):
    """Great-circle distance with mean Earth radius, same as the phone check."""
    a, b = math.radians(lat1), math.radians(lat2)
    dlat, dlon = b - a, math.radians(lon2 - lon1)
    h = math.sin(dlat / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(dlon / 2) ** 2
    return 2 * 6371008.8 * math.asin(min(1, math.sqrt(max(0, h))))


def read_state(server, timeout):
    parsed = urllib.parse.urlsplit(server)
    if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ValueError("--server must be a loopback http origin, such as http://localhost:8768")
    request = urllib.request.Request(server.rstrip("/") + "/api/state", headers={"Cache-Control": "no-store"})
    # Do not send local state through an environment-configured HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        body = response.read(2_000_001)
    if len(body) > 2_000_000:
        raise ValueError("State response exceeds 2 MB")
    state = json.loads(body)
    if not isinstance(state, dict):
        raise ValueError("State response must be a JSON object")
    return state


def load_records(path):
    if not path.exists():
        return []
    records = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            raise ValueError(f"Existing log line {line_no} is not an object")
        records.append(record)
    return records


def make_record(state, model, *, trial_id, condition, input_mode, reference, notes="",
                barometer_mode="simulated", previous=(), recorded_at=None, error=None):
    record = {
        "schema": "ridgeline-trial/v1", "trial_id": trial_id,
        "attempt_index": len(previous) + 1,
        "recorded_at_utc": recorded_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "condition": condition, "notes": notes,
        "input_mode": input_mode, "input_mode_basis": "operator_declared",
        "condition_basis": "operator_declared",
        "model_sha256": digest(model),
        "reference_requested": reference,
        "barometer_mode": barometer_mode, "barometer_mode_basis": "operator_declared",
        "barometer_m": None, "lock_count": None, "server_source": None,
        "state_version": None, "state_updated_unix": None,
        "phone_status": None, "mismatch": None, "estimate": None, "r95_m": None,
        "wall_reference": None, "wall_reference_error_m": None,
        "inside_reported_95_circle": None, "hpd_region_membership": "not_measured",
        "operator_completion_time_s": None, "phone_to_board_latency_ms": None,
        "accuracy_scope": "wall-card synthetic reference only; no real-mountain accuracy measurement",
        "duplicate_of": None, "duplicate_reasons": [], "new_attempt": True,
        "capture_status": "communication_error" if error else "no_fix",
        "error": error,
    }
    if state is not None:
        obs = state.get("obs") if isinstance(state.get("obs"), dict) else {}
        phone = state.get("phone") if isinstance(state.get("phone"), dict) else {}
        locks = obs.get("locks") if isinstance(obs.get("locks"), list) else []
        record.update(server_source=state.get("source"), state_version=state.get("version"),
                      state_updated_unix=state.get("updated"), lock_count=len(locks),
                      phone_status=phone.get("status"),
                      mismatch=(phone["status"] == "mismatch") if phone.get("status") else None,
                      barometer_m=obs.get("baro_m") if finite(obs.get("baro_m")) else None)
        record["state_fingerprint"] = digest({k: state.get(k) for k in ("version", "updated", "source", "obs", "phone")})
        # Only new observations make a new trial. Settings, GPS checks, marks,
        # health and a re-solve at a different barometer value can change the
        # revision and phone summary while the same Locks remain in use.
        # Keep each Lock's `at` timestamp so a fresh aim with identical numbers
        # is still a new observation. CLI labels never establish uniqueness.
        if locks:
            record["observations_fingerprint"] = digest({"source": state.get("source"), "locks": locks})
            record["observations_fingerprint_basis"] = "source_and_locks_v1"
        lat, lon, radius = phone.get("lat"), phone.get("lon"), phone.get("r95_m")
        valid = (finite(lat) and -90 <= lat <= 90 and finite(lon) and -180 <= lon <= 180
                 and finite(radius) and radius >= 0 and len(locks) > 0 and phone.get("n") == len(locks))
        if valid:
            record.update(estimate={"lat": lat, "lon": lon}, r95_m=radius,
                          capture_status="mismatch" if record["mismatch"] else "fix")
            demo = model.get("demo", {})
            if reference == "wall-cards" and state.get("source") == "phone":
                if not (finite(demo.get("lat")) and finite(demo.get("lon"))):
                    raise ValueError("Model has no valid wall-card demo reference")
                record["wall_reference"] = {"lat": demo["lat"], "lon": demo["lon"],
                                            "model_area": model.get("area"), "basis": "model.demo"}
                error_m = distance_m(lat, lon, demo["lat"], demo["lon"])
                record["wall_reference_error_m"] = round(error_m, 3)
                record["inside_reported_95_circle"] = error_m <= radius
            elif reference == "wall-cards":
                record["reference_note"] = "Not compared: only phone trials can use the declared wall-card reference."
        elif phone:
            record["error"] = "Phone summary is missing valid coordinates/radius or does not match the Lock count."
    for old in previous:
        reasons = []
        if old.get("trial_id") == trial_id:
            reasons.append("trial_id_reused")
        for key in ("state_fingerprint", "observations_fingerprint"):
            if record.get(key) and old.get(key) == record[key]:
                reasons.append(key + "_repeated")
        if reasons:
            record.update(duplicate_of=old.get("trial_id"), duplicate_reasons=reasons, new_attempt=False)
            break
    return record


def capture(args, fetcher=read_state):
    model = json.loads(args.model.read_text(encoding="utf-8"))
    previous = load_records(args.output)
    state, error = None, None
    try:
        state = fetcher(args.server, args.timeout)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    details = dict(trial_id=args.trial_id, condition=args.condition, input_mode=args.input_mode,
                   reference=args.reference, notes=args.notes, barometer_mode=args.barometer_mode,
                   previous=previous)
    try:
        record = make_record(state, model, error=error, **details)
    except (ValueError, TypeError, AttributeError) as exc:
        record = make_record(None, model, error=f"Invalid state: {type(exc).__name__}: {exc}", **details)
        record["capture_status"] = "invalid_state"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return record


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--trial-id", required=True, help="Unique attempt ID, e.g. gate-b-01 (also label failures)")
    p.add_argument("--condition", required=True, help="Operator-declared setup and conditions")
    p.add_argument("--input-mode", required=True, choices=("sensor", "keyboard", "unknown"))
    p.add_argument("--reference", required=True, choices=("wall-cards", "none"),
                   help="Use wall-cards only when aiming from the marked wall-card position")
    p.add_argument("--notes", default="")
    p.add_argument("--barometer-mode", choices=("simulated", "manual", "unknown"), default="simulated")
    p.add_argument("--server", default="http://localhost:8768")
    p.add_argument("--timeout", type=float, default=5.0)
    p.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Append-only log, defaults to ignored .local/")
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.timeout <= 0 or not math.isfinite(args.timeout):
        p.error("--timeout must be a finite positive number")
    try:
        record = capture(args)
    except (OSError, ValueError, TypeError) as exc:
        p.exit(2, f"No record written: {exc}\n")
    label = "duplicate recorded" if not record["new_attempt"] else record["capture_status"] + " recorded"
    print(f"{args.trial_id}: {label}. Local log: {args.output.resolve()}")
    print("This records an attempt, not a pass. Keep failed attempts in the same log.")
    return 2 if record["capture_status"] in {"communication_error", "invalid_state"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
