"""Write data/<id>/nogo.png on the working grid (the mesh grid, twice the texture cell) for the route planner in the browser.
Red 255 = water, green 255 = cliff (any texture-cell patch steeper than 45 deg), the same rules team8_simulate.py uses,
so the map, the Python planner and the browser agree on what nobody can cross.

Usage: python -I scripts/team8_masks.py data/<id>   (then run team8_bundle.py again)
"""
import json, os, sys

import numpy as np
from PIL import Image

DATA = sys.argv[1].rstrip("/\\")
meta = json.load(open(os.path.join(DATA, "terrain_meta.json"), encoding="utf-8"))
W5, H5 = meta["grid"]["width"], meta["grid"]["height"]
rgb = np.asarray(Image.open(os.path.join(DATA, meta["files"]["terrain_rgb"])).convert("RGB")).astype(np.int64)
Z5 = -10000.0 + (rgb[..., 0] * 65536 + rgb[..., 1] * 256 + rgb[..., 2]) * 0.1
LAKE5 = np.asarray(Image.open(os.path.join(DATA, meta["files"]["water_mask"]))) > 127
DX5, DY5 = meta["grid"]["cell_m"]

F = 2
H, W = H5 // F, W5 // F
LAKE = LAKE5[:H * F, :W * F].reshape(H, F, W, F).any(axis=(1, 3))
gy, gx = np.gradient(Z5, DY5, DX5)
S5 = np.degrees(np.arctan(np.hypot(gx, gy)))
CLIFF = S5[:H * F, :W * F].reshape(H, F, W, F).max(axis=(1, 3)) > 45.0

out = np.zeros((H, W, 3), np.uint8)
out[..., 0] = np.where(LAKE, 255, 0)
out[..., 1] = np.where(CLIFF & ~LAKE, 255, 0)
Image.fromarray(out).save(os.path.join(DATA, "nogo.png"), optimize=True)
print(DATA, "nogo.png", W, "x", H, "water", round(float(LAKE.mean()) * 100, 2), "% cliff", round(float(CLIFF.mean()) * 100, 2), "%")
