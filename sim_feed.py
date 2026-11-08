"""Fake data feed: orbiting drone, drifting target, and lidar/ir/thermal frames.
Backfills 30 minutes of history first so the history sliders have something to show."""
import base64
import math
import struct
import time
import zlib

import requests

API = "http://127.0.0.1:8050/api"
LAT0, LON0 = 42.35, -70.75
T0 = time.time()
COLORS = {"thermal": (255, 80, 0, 160), "ir": (200, 0, 255, 140), "lidar": (0, 220, 90, 140)}
HALF = {"thermal": 0.010, "ir": 0.007, "lidar": 0.004}  # footprint half-size in degrees


def post(path, payload):
    try:
        requests.post(f"{API}/{path}", json=payload, timeout=5)
        return True
    except requests.ConnectionError:
        print("COP not reachable yet, retrying...")
        return False


def tiny_png(rgba, size=32):
    """Solid semi-transparent PNG as a data URI (stand-in for a real georeferenced frame)."""
    row = b"\x00" + bytes(rgba) * size
    raw = zlib.compress(row * size)

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d))

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", raw) + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(png).decode()


PNGS = {s: tiny_png(c) for s, c in COLORS.items()}


def drone_at(ts):
    ang = (ts - T0) / 20
    return {"id": "uas-1", "type": "drone", "label": "UAS-1", "ts": ts,
            "lat": LAT0 + 0.05 * math.sin(ang), "lon": LON0 + 0.05 * math.cos(ang),
            "heading": int(math.degrees(ang) % 360)}


def target_at(ts):
    return {"id": "tgt-1", "type": "target", "label": "Person in water", "ts": ts,
            "lat": LAT0 + 0.0001 * (ts - T0), "lon": LON0 + 0.00015 * (ts - T0)}


def frame_at(sensor, ts):
    d = drone_at(ts)
    h = HALF[sensor]
    return {"id": f"{sensor}-uas-1", "sensor": sensor, "url": PNGS[sensor], "ts": ts,
            "bounds": [[d["lat"] - h, d["lon"] - h], [d["lat"] + h, d["lon"] + h]]}


# ---- backfill 30 min of history (retry until the COP is up)
while True:
    now = time.time()
    assets = [f(now - k) for k in range(1800, 0, -10) for f in (drone_at, target_at)]
    frames = [frame_at(s, now - k) for s in COLORS for k in range(1800, 0, -60)]
    if post("assets", assets) and post("overlays", frames):
        print("backfilled 30 min of history")
        break
    time.sleep(2)

# ---- live loop
t = 0
while True:
    t += 1
    now = time.time()
    post("assets", [drone_at(now), target_at(now)])
    if t % 5 == 1:
        post("overlays", [frame_at("thermal", now)])
    if t % 10 == 1:
        post("overlays", [frame_at("ir", now)])
    if t % 15 == 1:
        post("overlays", [frame_at("lidar", now)])
    time.sleep(2)
