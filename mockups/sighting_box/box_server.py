"""Loopback USB-to-SSE bridge. No localization is performed here."""
from __future__ import annotations

import argparse
import json
import math
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_line(raw):
    if len(raw) > 2048:
        raise ValueError("line exceeds 2048 bytes")
    d = json.loads(raw)
    if not isinstance(d, dict):
        raise ValueError("expected object")
    for key in ("t", "yaw", "pitch", "btn", "long", "gps"):
        if key not in d or isinstance(d[key], bool) or not isinstance(d[key], (int, float)):
            raise ValueError("invalid " + key)
        if not math.isfinite(d[key]):
            raise ValueError("nonfinite " + key)
    if not isinstance(d["t"], int) or not 0 <= d["t"] <= 0xFFFFFFFF:
        raise ValueError("t must be unsigned milliseconds")
    if not -90 <= d["pitch"] <= 90 or abs(d["yaw"]) > 1e7:
        raise ValueError("invalid angle")
    if any(d[k] not in (0, 1) for k in ("btn", "long", "gps")):
        raise ValueError("flags must be 0 or 1")
    if d["btn"] and d["long"]:
        raise ValueError("short and long release cannot coincide")
    return {k: d[k] for k in ("t", "yaw", "pitch", "btn", "long", "gps")}


def load_scene(path):
    s = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    peaks = s["peaks"]
    if not peaks or len({p["id"] for p in peaks}) != len(peaks):
        raise ValueError("scene requires unique peaks")
    ids = {p["id"] for p in peaks}
    if s["reference_peak_id"] not in ids or not s["target_order"]:
        raise ValueError("scene reference/order missing")
    if any(i not in ids for i in s["target_order"]):
        raise ValueError("unknown target")
    for p in peaks:
        if not isinstance(p["name"], str) or not 1 <= len(p["name"]) <= 40:
            raise ValueError("invalid peak name")
        if not all(isinstance(p[k], (int, float)) and not isinstance(p[k], bool) and math.isfinite(p[k]) for k in ("bearing_true", "elev_deg")):
            raise ValueError("invalid scene angle")
        if not 0 <= p["bearing_true"] < 360 or not -89 < p["elev_deg"] < 89:
            raise ValueError("scene angle out of bounds")
    return s


class Bridge:
    def __init__(self, scene, demo=False):
        self.scene, self.demo = scene, demo
        self.peaks = {p["id"]: p for p in scene["peaks"]}
        self.reference = self.peaks[scene["reference_peak_id"]]
        self.mutex = threading.RLock()
        self.clients = set()
        self.commands = queue.Queue(maxsize=32)
        self.seq = 0
        self.session = str(uuid.uuid4())
        self.connected = False
        self.errors = 0
        self.last_error = None
        self.last_t = None
        self.previous_btn = self.previous_long = 0
        self.yaw_zero = None
        self.locks = []
        self.orientation = None

    def target(self):
        if self.yaw_zero is None:
            return self.reference
        order = self.scene["target_order"]
        return self.peaks[order[len(self.locks)]] if len(self.locks) < len(order) else None

    def state(self):
        with self.mutex:
            return {"session": self.session, "demo": self.demo, "connected": self.connected,
                    "calibrated": self.yaw_zero is not None, "reference": self.reference,
                    "target": self.target(), "locks": list(self.locks), "orientation": self.orientation,
                    "gps_simulated": True, "height_source": "fixed_scene", "source_stage": self.scene.get("source_stage"),
                    "status": "awaiting_estimator" if len(self.locks) == len(self.scene["target_order"]) else "observing",
                    "position_fix": False, "invalid_lines": self.errors, "last_error": self.last_error}

    def packet(self, event, data):
        self.seq += 1
        return f"id: {self.session}:{self.seq}\nevent: {event}\ndata: {json.dumps(data, ensure_ascii=False, allow_nan=False)}\n\n".encode("utf-8")

    def emit(self, event, data):
        with self.mutex:
            p = self.packet(event, data)
            for q in tuple(self.clients):
                try:
                    q.put_nowait(p)
                except queue.Full:
                    self.clients.discard(q)
                    while not q.empty():
                        q.get_nowait()
                    q.put_nowait(None)  # force reconnect and authoritative state snapshot

    def subscribe(self):
        with self.mutex:
            q = queue.Queue(maxsize=128)
            self.clients.add(q)
            q.put_nowait(self.packet("state", self.state()))
            return q

    def unsubscribe(self, q):
        with self.mutex:
            self.clients.discard(q)

    def send_target(self):
        p = self.target()
        command = {"cmd": "aim", "peak": p["name"], "calibrate": self.yaw_zero is None} if p else {"cmd": "await_estimator"}
        if self.commands.full():
            self.commands.get_nowait()
        self.commands.put_nowait(command)
        self.emit("target", {"target": p, "calibrate": self.yaw_zero is None, "demo": self.demo})

    def reset(self, reason):
        with self.mutex:
            self.session = str(uuid.uuid4())
            self.yaw_zero = None
            self.locks = []
            self.orientation = None
            self.last_t = None
            self.previous_btn = self.previous_long = 0
            while not self.commands.empty():
                self.commands.get_nowait()
            self.emit("reset", {"reason": reason, "session": self.session, "demo": self.demo})
            self.send_target()
            self.emit("state", self.state())

    def connection(self, connected, reason):
        with self.mutex:
            self.connected = connected
            self.reset(reason)

    def ingest(self, raw):
        try:
            d = parse_line(raw)
        except (ValueError, TypeError, UnicodeError, OverflowError) as exc:
            with self.mutex:
                self.errors += 1
                self.last_error = str(exc)
            return False
        with self.mutex:
            if self.last_t is not None:
                if d["t"] == self.last_t:
                    return False
                if d["t"] < self.last_t:
                    wrapped = self.last_t > 0xFFFF0000 and d["t"] < 0x10000
                    if not wrapped:
                        self.reset("device_clock_restarted")
            self.last_t = d["t"]
            long_edge = d["long"] == 1 and not self.previous_long
            short_edge = d["btn"] == 1 and not self.previous_btn
            self.previous_long, self.previous_btn = d["long"], d["btn"]
            if long_edge:
                self.yaw_zero = d["yaw"]
                self.locks = []
                self.emit("calibration", {"peak": self.reference["name"], "bearing_true": self.reference["bearing_true"], "yaw_zero": self.yaw_zero, "time": stamp(), "demo": self.demo})
                self.send_target()
            bearing = None if self.yaw_zero is None else (self.reference["bearing_true"] + d["yaw"] - self.yaw_zero) % 360
            self.orientation = {"t": d["t"], "yaw": d["yaw"], "bearing_true": bearing, "elev_deg": d["pitch"], "gps": d["gps"], "time": stamp(), "demo": self.demo}
            self.emit("orientation", self.orientation)
            if short_edge:
                target = self.target()
                if self.yaw_zero is None:
                    self.emit("notice", {"message": "calibration_required", "demo": self.demo})
                elif target is not None:
                    lock = {"peak": target["name"], "bearing_true": round(bearing, 3), "elev_deg": d["pitch"], "time": stamp()}
                    self.locks.append(lock)
                    self.emit("lock", lock)
                    self.send_target()
            if short_edge or long_edge:
                self.emit("state", self.state())
            return True


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, bridge):
        self.bridge = bridge
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def headers_for(self, content_type, length=None):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        origin = self.headers.get("Origin")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def do_GET(self):
        host = urlsplit("http://" + self.headers.get("Host", "")).hostname
        origin = self.headers.get("Origin")
        if host not in ("localhost", "127.0.0.1", "::1"):
            self.send_error(403, "loopback host required")
            return
        if origin:
            o = urlsplit(origin)
            if o.scheme not in ("http", "https") or o.hostname not in ("localhost", "127.0.0.1", "::1"):
                self.send_error(403, "loopback origin required")
                return
        path = urlsplit(self.path).path
        if path == "/events":
            q = self.server.bridge.subscribe()
            try:
                self.connection.settimeout(15)
                self.headers_for("text/event-stream; charset=utf-8")
                self.wfile.write(b"retry: 1000\n\n")
                self.wfile.flush()
                while True:
                    try:
                        packet = q.get(timeout=5)
                    except queue.Empty:
                        packet = b": heartbeat\n\n"
                    if packet is None:
                        break
                    self.wfile.write(packet)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionError, TimeoutError, OSError):
                pass
            finally:
                self.server.bridge.unsubscribe(q)
                self.close_connection = True
            return
        if path == "/status":
            data = json.dumps(self.server.bridge.state(), ensure_ascii=False).encode("utf-8")
            self.headers_for("application/json; charset=utf-8", len(data))
            self.wfile.write(data)
            return
        files = {"/": "guide.html", "/guide.html": "guide.html", "/poster_sheet.html": "poster_sheet.html", "/box_scene.json": "box_scene.json"}
        filename = files.get(path)
        if not filename or not (ROOT / filename).is_file():
            self.send_error(404)
            return
        data = (ROOT / filename).read_bytes()
        self.headers_for("application/json; charset=utf-8" if filename.endswith(".json") else "text/html; charset=utf-8", len(data))
        self.wfile.write(data)


def demo_loop(bridge, stop):
    bridge.connection(True, "demo_started")
    frame = 0
    while not stop.is_set():
        i = frame % 260  # 26-second synthetic repeat
        if i == 0 and frame:
            bridge.reset("demo_cycle")
        target_index = min(2, max(0, (i - 50) // 50))
        p = bridge.reference if i < 50 else bridge.peaks[bridge.scene["target_order"][target_index]]
        yaw = (350 + p["bearing_true"] - bridge.reference["bearing_true"]) % 360
        d = {"t": frame * 100, "yaw": yaw, "pitch": p["elev_deg"], "btn": int(i in (90, 140, 190)), "long": int(i == 30), "gps": int(i < 20)}
        bridge.ingest(json.dumps(d))
        frame += 1
        stop.wait(0.1)


def serial_loop(bridge, port, baud, stop):
    import serial  # optional dependency, hardware mode only
    while not stop.is_set():
        try:
            with serial.Serial(port, baudrate=baud, timeout=0.2, write_timeout=1) as usb:
                bridge.connection(True, "serial_connected")
                last_valid = time.monotonic()
                stale = False
                pending = bytearray()
                dropping = False
                while not stop.is_set():
                    chunk = usb.read(usb.in_waiting or 1)
                    for byte in chunk:
                        if byte == 10:
                            if not dropping and pending:
                                if bridge.ingest(bytes(pending)):
                                    last_valid = time.monotonic()
                                    if stale:
                                        bridge.connected = True
                                        bridge.emit("state", bridge.state())
                                        stale = False
                            pending.clear()
                            dropping = False
                        elif not dropping:
                            pending.append(byte)
                            if len(pending) > 2048:
                                pending.clear()
                                dropping = True
                                bridge.errors += 1
                    if not stale and time.monotonic() - last_valid > 3:
                        bridge.connection(False, "serial_stale")
                        stale = True
                    while not bridge.commands.empty():
                        usb.write((json.dumps(bridge.commands.get_nowait()) + "\n").encode("utf-8"))
        except (serial.SerialException, OSError) as exc:
            bridge.last_error = str(exc)
            bridge.connection(False, "serial_disconnected")
            stop.wait(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--demo", action="store_true")
    source.add_argument("--port", help="Arduino serial name, for example COM5")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--http-port", type=int, default=8765)
    parser.add_argument("--scene", type=Path, default=ROOT / "box_scene.json")
    args = parser.parse_args()
    if args.port:
        try:
            import serial
        except ImportError:
            parser.error("Hardware mode needs pyserial. Demo mode needs no installation.")
    bridge = Bridge(load_scene(args.scene), args.demo)
    stop = threading.Event()
    server = Server(("127.0.0.1", args.http_port), bridge)
    worker = threading.Thread(target=demo_loop if args.demo else serial_loop, args=(bridge, stop) if args.demo else (bridge, args.port, args.baud, stop), daemon=True)
    worker.start()
    print(f"{'DEMO synthetic' if args.demo else 'USB'}  http://localhost:{args.http_port}/guide.html", flush=True)
    print("SSE /events | state /status | Ctrl+C to stop. No position solver.", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        worker.join(timeout=3)


if __name__ == "__main__":
    main()
