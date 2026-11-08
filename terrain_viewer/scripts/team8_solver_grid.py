"""RIDGELINE live solver grid: one JSON file the browser and Node solver both read.

  python -I scripts/team8_solver_grid.py data/moran [--out data/moran/solver_grid.json]

Reads the files team8_terrain_prep.py already wrote for the tile (terrain_meta.json, terrain_rgb.png at the 5 m
texture cell, water_mask.png), so no network and no rasterio are needed. Needs numpy, scipy and Pillow.

Writes solver_grid.json with
  * a 30 m elevation grid (6 x 6 mean of the 5 m texture grid, row 0 north, edges = bounds)
  * water and 30 m slope on the same grid (for the route planner)
  * the peak list, found with the same rules as find_true_peaks in topological_navigation/sprint2_localization.py
    (smoothed elevation at least 2,500 m, highest point in an 800 m window, at least 150 m above the 1 km mean)
  * one line-of-sight mask per peak: from every 30 m cell, eye 1.7 m above the cell, to the summit, earth curvature
    with standard refraction k = 0.13 (the same model as team8_simulate.py), terrain sampled every 10 m on a 10 m grid
  * a synthetic demo spot where three visible peaks span about 100 degrees of azimuth, with its true bearings and
    elevation angles, a synthetic barometer reading, and the skyline seen from it (for the keyboard simulation)

Typed arrays are little-endian and base64 encoded, the header says how to decode each one.
Everything here is synthetic. The demo spot is a made-up point, not a real person or position.
"""
import argparse, base64, json, math, os, time
import numpy as np
from scipy import ndimage
from PIL import Image

T0 = time.time()


def log(*a):
    print(f"[{time.time() - T0:6.1f}s]", *a, flush=True)


ap = argparse.ArgumentParser()
ap.add_argument("data_dir")
ap.add_argument("--out", default=None)
ap.add_argument("--block", type=int, default=6, help="texture cells per solver cell (6 x 5 m = 30 m)")
ap.add_argument("--seed", type=int, default=8)
A = ap.parse_args()
DATA = A.data_dir
OUT = A.out or os.path.join(DATA, "solver_grid.json")

EYE_M = 1.7
R_E = 6371000.0
K_REF = 0.13
CURV = (1.0 - K_REF) / (2.0 * R_E)        # drop in metres = CURV * d^2

meta = json.load(open(os.path.join(DATA, "terrain_meta.json"), encoding="utf-8"))
west, south, east, north = meta["bounds"]
W5, H5 = meta["grid"]["width"], meta["grid"]["height"]
DX5, DY5 = meta["grid"]["cell_m"]
rgb = np.asarray(Image.open(os.path.join(DATA, meta["files"]["terrain_rgb"])).convert("RGB")).astype(np.int64)
Z5 = (-10000.0 + (rgb[..., 0] * 65536 + rgb[..., 1] * 256 + rgb[..., 2]) * 0.1).astype(np.float64)
del rgb
WATER5 = np.asarray(Image.open(os.path.join(DATA, meta["files"]["water_mask"]))) > 127
assert Z5.shape == (H5, W5), Z5.shape

# metres per degree from the grid itself, the same constants the viewer uses (MPLON, MPLAT)
MPLON = DX5 / ((east - west) / W5)
MPLAT = DY5 / ((north - south) / H5)

# ------------------------------------------------------------ 30 m grid
F = A.block
H, W = H5 // F, W5 // F
assert H * F == H5 and W * F == W5, "prep writes grids in multiples of 6"
DX, DY = DX5 * F, DY5 * F
Z = Z5.reshape(H, F, W, F).mean(axis=(1, 3))
WATER = WATER5.reshape(H, F, W, F).mean(axis=(1, 3)) >= 0.5
gy, gx = np.gradient(Z, DY, DX)
SLOPE = np.degrees(np.arctan(np.hypot(gx, gy)))
ROWS, COLS = np.mgrid[0:H, 0:W]
XE = (COLS + 0.5) * DX                     # metres east of the west edge
YN = (H - ROWS - 0.5) * DY                 # metres north of the south edge
log("solver grid", (H, W), "cell", round(DX, 2), round(DY, 2), "m")

# terrain for line of sight: 10 m grid (2 x 2 mean of the 5 m grid)
Z10 = Z5.reshape(H5 // 2, 2, W5 // 2, 2).mean(axis=(1, 3))
H10, W10 = Z10.shape
DX10, DY10 = DX5 * 2, DY5 * 2
Z10f = Z10.astype(np.float32)


def z10_at(xe, yn):
    """Bilinear terrain height at metres east / north of the SW corner, 10 m grid."""
    c = xe / DX10 - 0.5
    r = (H10 - 0.5) - yn / DY10
    c = np.clip(c, 0, W10 - 1.001)
    r = np.clip(r, 0, H10 - 1.001)
    c0 = c.astype(np.int32)
    r0 = r.astype(np.int32)
    fc = (c - c0).astype(np.float32)
    fr = (r - r0).astype(np.float32)
    i = r0 * W10 + c0
    zf = Z10f.ravel()
    a = zf[i]
    b = zf[i + 1]
    cc = zf[i + W10]
    d = zf[i + W10 + 1]
    top = a + (b - a) * fc
    bot = cc + (d - cc) * fc
    return top + (bot - top) * fr


# ------------------------------------------------------------ peaks, as find_true_peaks
def find_true_peaks(elevation_data, cell_m, min_altitude=2500, isolation_meters=800, min_prominence_proxy=150):
    """Port of find_true_peaks (topological_navigation/sprint2_localization.py) to any cell size.

    The original runs on a 1 m DEM, so its pixel sizes are metres: gaussian sigma 5 px = 5 m, maximum_filter
    size 800 px = an 800 m window, uniform_filter size 1000 px = a 1 km window. Here the same metres are turned
    into pixels of this grid."""
    sigma_px = 5.0 / cell_m
    smoothed = ndimage.gaussian_filter(elevation_data, sigma=sigma_px)
    win = int(round(isolation_meters / cell_m)) | 1
    is_local_max = smoothed == ndimage.maximum_filter(smoothed, size=win)
    is_high_enough = smoothed >= min_altitude
    regional_mean = ndimage.uniform_filter(smoothed, size=int(round(1000.0 / cell_m)) | 1)
    macro_tpi = smoothed - regional_mean
    is_prominent = macro_tpi >= min_prominence_proxy
    mask = is_local_max & is_high_enough & is_prominent
    return mask, np.argwhere(mask), smoothed, macro_tpi


CELL5 = (DX5 + DY5) / 2
mask5, pix5, SM5, TPI5 = find_true_peaks(Z5, CELL5)
log("find_true_peaks candidates", len(pix5))

edge5 = int(math.ceil(150.0 / CELL5))      # a top cut by the tile edge is not a summit we can name
SM30 = SM5.reshape(H, F, W, F).max(axis=(1, 3))
named = meta.get("peaks", [])
peaks = []
for r5, c5 in pix5:
    if r5 < edge5 or c5 < edge5 or r5 >= H5 - edge5 or c5 >= W5 - edge5:
        continue
    # refine to the raw 5 m maximum within 15 m, so the target is the true top
    r0, r1, c0, c1 = max(0, r5 - 3), min(H5, r5 + 4), max(0, c5 - 3), min(W5, c5 + 4)
    blk = Z5[r0:r1, c0:c1]
    br, bc = np.unravel_index(np.argmax(blk), blk.shape)
    pr, pc = r0 + br, c0 + bc
    if any(abs(p["r5"] - pr) * DY5 < 60 and abs(p["c5"] - pc) * DX5 < 60 for p in peaks):
        continue                            # plateau ties of the same top
    lon = west + (pc + 0.5) * (east - west) / W5
    lat = north - (pr + 0.5) * (north - south) / H5
    z = float(Z5[pr, pc])
    # isolation actually measured: distance to the nearest higher ground on the smoothed 30 m grid
    hr, hc = np.nonzero(SM30 > float(SM5[r5, c5]) + 0.5)
    yy, xx = (H5 - pr - 0.5) * DY5, (pc + 0.5) * DX5
    iso = float(np.min(np.hypot(YN[hr, hc] - yy, XE[hr, hc] - xx))) if hr.size else None
    best, bd = None, 1e9
    for q in named:
        d = math.hypot((q["lon"] - lon) * MPLON, (q["lat"] - lat) * MPLAT)
        if d < bd:
            best, bd = q, d
    if best is not None and bd <= 250:
        name, verified, src = best["name"], bool(best.get("verified")), best.get("source")
    else:
        name, verified, src = f"Pk {z:.0f}", False, None
    peaks.append(dict(r5=int(pr), c5=int(pc), lon=round(lon, 6), lat=round(lat, 6), elev_m=round(z, 1), name=name,
                      verified=verified, source=src, name_offset_m=round(bd) if bd <= 250 else None,
                      tpi_m=round(float(TPI5[r5, c5]), 1), isolation_m=None if iso is None else round(iso)))
peaks.sort(key=lambda p: -p["elev_m"])
seen = {}
for p in peaks:                               # unique, readable ids
    base = "".join(ch if ch.isalnum() else "-" for ch in p["name"].lower()).strip("-")
    while "--" in base:
        base = base.replace("--", "-")
    k = seen.get(base, 0)
    seen[base] = k + 1
    p["id"] = base if k == 0 else f"{base}-{k + 1}"
NP = len(peaks)
assert 3 <= NP <= 32, NP
log("peaks", [(p["id"], p["elev_m"], p["isolation_m"]) for p in peaks])

# peak positions in metres (x east, y north of the SW corner), the solver uses the same local frame
for p in peaks:
    p["x_m"] = round((p["lon"] - west) * MPLON, 2)
    p["y_m"] = round((p["lat"] - south) * MPLAT, 2)
    p["row"] = int(min(H - 1, max(0, (north - p["lat"]) / (north - south) * H)))
    p["col"] = int(min(W - 1, max(0, (p["lon"] - west) / (east - west) * W)))

# ------------------------------------------------------------ line of sight from every 30 m cell to every peak
X = XE.ravel().astype(np.float64)
Y = YN.ravel().astype(np.float64)
EYE = (Z.ravel() + EYE_M).astype(np.float64)
NC = X.size
STEP = 10.0
NEAR, FAR = 20.0, 30.0                        # skip the ground under your feet and the summit block itself
VIS = np.zeros((NP, NC), bool)
for k, p in enumerate(peaks):
    dx, dy = p["x_m"] - X, p["y_m"] - Y
    d = np.hypot(dx, dy)
    ux, uy = dx / np.maximum(d, 1e-9), dy / np.maximum(d, 1e-9)
    zt = p["elev_m"] - CURV * d * d           # summit height in the tangent plane of the eye
    slope = (zt - EYE) / np.maximum(d, 1e-9)  # straight sight line, metres per metre
    vis = np.ones(NC, bool)
    nsteps = np.floor((d - FAR) / STEP).astype(np.int64)
    idx = np.nonzero(nsteps >= 2)[0]
    s = 2
    while idx.size:
        dist = s * STEP
        xs = X[idx] + ux[idx] * dist
        ys = Y[idx] + uy[idx] * dist
        ground = z10_at(xs, ys) - CURV * dist * dist
        line = EYE[idx] + slope[idx] * dist
        blocked = ground > line
        vis[idx[blocked]] = False
        keep = (~blocked) & (nsteps[idx] > s)
        idx = idx[keep]
        s += 1
    vis[d < 60] = True                        # standing on the summit
    VIS[k] = vis
    p["visible_share"] = round(float(vis[~WATER.ravel()].mean()), 3)
    log(f"  los {p['id']:<16} visible from {100 * p['visible_share']:.1f}% of land, {s} steps")

# ------------------------------------------------------------ predicted bearings and elevation angles
def predict(ix, k):
    p = peaks[k]
    dx, dy = p["x_m"] - X[ix], p["y_m"] - Y[ix]
    d = np.hypot(dx, dy)
    az = (np.degrees(np.arctan2(dx, dy)) + 360.0) % 360.0
    el = np.degrees(np.arctan2(p["elev_m"] - CURV * d * d - EYE[ix], d))
    return az, el, d


# ------------------------------------------------------------ demo spot: three visible peaks spanning about 100 deg
# A Fisher-information proxy for the 95% radius (unknown heading offset, sigma 3 deg az and 1.5 deg el) ranks spots,
# the live solver then checks the winner in tests/solver.test.mjs.
SIG_AZ, SIG_EL = math.radians(3.0), math.radians(1.5)
land = (~WATER.ravel()) & (SLOPE.ravel() < 25.0)
margin = 400.0
inner = (X > margin) & (X < W * DX - margin) & (Y > margin) & (Y < H * DY - margin)
base_ok = land & inner
log("demo candidates", int((base_ok & (VIS.sum(axis=0) >= 3)).sum()))

# per peak, for every cell: bearing, elevation angle, distance, and the Jacobian rows wrt the observer (x, y)
AZ = np.zeros((NP, NC))
EL = np.zeros((NP, NC))
DD = np.zeros((NP, NC))
JA = np.zeros((NP, 2, NC))                  # d az / d(x, y), radians per metre
JE = np.zeros((NP, 2, NC))                  # d el / d(x, y)
for k, p in enumerate(peaks):
    ddx, ddy = p["x_m"] - X, p["y_m"] - Y
    r2 = np.maximum(ddx * ddx + ddy * ddy, 1.0)
    r = np.sqrt(r2)
    AZ[k] = (np.degrees(np.arctan2(ddx, ddy)) + 360.0) % 360.0
    hgt = p["elev_m"] - CURV * r2 - EYE
    EL[k] = np.degrees(np.arctan2(hgt, r))
    DD[k] = r
    JA[k, 0], JA[k, 1] = -ddy / r2, ddx / r2
    dedr = -hgt / (r2 + hgt * hgt)
    JE[k, 0], JE[k, 1] = dedr * (-ddx / r), dedr * (-ddy / r)
OKP = VIS & (DD > 900) & (DD < 6500) & (EL > 2.0) & (EL < 30.0) & base_ok[None, :]

best = None
wa, we = 1.0 / SIG_AZ ** 2, 1.0 / SIG_EL ** 2
pref = np.array([2.0 if p["verified"] else 1.0 for p in peaks])
from itertools import combinations
for tri in combinations(range(NP), 3):
    ok = OKP[tri[0]] & OKP[tri[1]] & OKP[tri[2]]
    ix = np.nonzero(ok)[0]
    if ix.size == 0:
        continue
    a = np.sort(np.stack([AZ[t, ix] for t in tri]), axis=0)
    gaps = np.stack([a[1] - a[0], a[2] - a[1], a[0] + 360.0 - a[2]])
    span = 360.0 - gaps.max(axis=0)
    inner_gap = np.sort(gaps, axis=0)[:2].min(axis=0)      # the two gaps inside the span
    good = (span >= 92.0) & (span <= 108.0) & (inner_gap >= 25.0)
    if not good.any():
        continue
    ix, span = ix[good], span[good]
    # information on (x, y, heading offset), then the offset marginalised out (Schur complement)
    Ixx = sum(wa * JA[t, 0, ix] ** 2 + we * JE[t, 0, ix] ** 2 for t in tri)
    Iyy = sum(wa * JA[t, 1, ix] ** 2 + we * JE[t, 1, ix] ** 2 for t in tri)
    Ixy = sum(wa * JA[t, 0, ix] * JA[t, 1, ix] + we * JE[t, 0, ix] * JE[t, 1, ix] for t in tri)
    fx = sum(wa * JA[t, 0, ix] for t in tri)
    fy = sum(wa * JA[t, 1, ix] for t in tri)
    foo = 3 * wa
    Ixx, Iyy, Ixy = Ixx - fx * fx / foo, Iyy - fy * fy / foo, Ixy - fx * fy / foo
    det = Ixx * Iyy - Ixy * Ixy
    r95 = np.where(det > 0, math.sqrt(5.991) * np.maximum(det, 1e-30) ** -0.25, np.inf)
    score = r95 / float(pref[list(tri)].prod()) ** 0.15
    j = int(np.argmin(score))
    if best is None or score[j] < best[0]:
        best = (float(score[j]), float(r95[j]), int(ix[j]), list(tri), float(span[j]))
assert best is not None, "no demo spot found"
_, r95, DIX, DK, SPAN = best
ix = np.array([DIX])
dr, dc = divmod(DIX, W)
demo_lon = west + (dc + 0.5) * (east - west) / W
demo_lat = north - (dr + 0.5) * (north - south) / H
order = sorted(DK, key=lambda k: float(predict(ix, k)[0][0]))
demo_peaks = []
for k in order:
    az, el, dd = predict(ix, k)
    demo_peaks.append(dict(peak_id=peaks[k]["id"], name=peaks[k]["name"], bearing_true_deg=round(float(az[0]), 2),
                           elevation_deg=round(float(el[0]), 2), dist_m=round(float(dd[0]))))
log("demo spot", dr, dc, round(demo_lat, 6), round(demo_lon, 6), "z", round(float(Z[dr, dc]), 1), "span", round(SPAN, 1),
    "proxy r95", round(r95), [(q["peak_id"], q["bearing_true_deg"], q["elevation_deg"], q["dist_m"]) for q in demo_peaks])

# skyline from the demo spot (what the keyboard simulation shows), 0.5 deg steps, from the 10 m terrain
az_step = 0.5
azs = np.arange(0, 360, az_step)
sky = np.full(azs.size, -90.0)
sky_range = np.zeros(azs.size)
ex, ey, ez = X[DIX], Y[DIX], EYE[DIX]
dists = np.arange(30.0, 16000.0, 10.0)
for i, a in enumerate(np.radians(azs)):
    xs, ys = ex + np.sin(a) * dists, ey + np.cos(a) * dists
    inside = (xs >= 0) & (xs <= W * DX) & (ys >= 0) & (ys <= H * DY)
    if not inside.any():
        continue
    last = np.nonzero(inside)[0].max() + 1
    xs, ys, ds = xs[:last], ys[:last], dists[:last]
    ang = np.degrees(np.arctan2(z10_at(xs, ys) - CURV * ds * ds - ez, ds))
    j = int(np.argmax(ang))
    sky[i] = round(float(ang[j]), 2)
    sky_range[i] = round(float(ds[j]))
baro_noise = 6.0                               # synthetic barometer error added to the demo reading
demo = dict(
    note="Synthetic demo spot for the RIDGELINE live solve. Made up, not a real person or position. Hidden on the "
         "judge map until Reveal.",
    row=int(dr), col=int(dc), cell=int(DIX), lon=round(demo_lon, 6), lat=round(demo_lat, 6),
    elev_m=round(float(Z[dr, dc]), 1), eye_m=round(float(EYE[DIX]), 1),
    baro_alt_m=round(float(EYE[DIX]) + baro_noise, 1), baro_band_m=30.0, baro_noise_added_m=baro_noise,
    span_deg=round(float(SPAN), 1), proxy_r95_m=round(r95),
    peaks=demo_peaks,
    skyline=dict(az_step_deg=az_step, elevation_deg=[float(v) for v in sky], range_m=[int(v) for v in sky_range]),
)

# ------------------------------------------------------------ write
def b64(a):
    return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode("ascii")


ELEV_OFF, ELEV_SCALE = 1000.0, 0.1
ev = np.round((Z - ELEV_OFF) / ELEV_SCALE)
assert ev.min() >= 0 and ev.max() < 65535
vis_dtype = "<u2" if NP <= 16 else "<u4"
bits = np.zeros(NC, np.uint32)
for k in range(NP):
    bits |= (VIS[k].astype(np.uint32) << k)
out = dict(
    schema="ridgeline-solver-grid/v1",
    area=meta["id"], name=meta["name"], region=meta["region"],
    note="Synthetic scenario. Same DEM for the hidden demo spot and the model, so real-world DEM and sensor error "
         "are not included.",
    bounds=meta["bounds"], crs="EPSG:4326", rows=H, cols=W, cell_m=[round(DX, 3), round(DY, 3)],
    m_per_deg=[round(MPLON, 4), round(MPLAT, 4)],
    frame="x = (lon - west) * m_per_deg[0] metres east, y = (lat - south) * m_per_deg[1] metres north; "
          "cell (row, col) centre at x = (col + 0.5) * cell_m[0], y = (rows - row - 0.5) * cell_m[1]; row 0 is north",
    eye_height_m=EYE_M, earth_radius_m=R_E, refraction_k=K_REF,
    elevation={"dtype": "uint16", "endian": "little", "encoding": "base64", "offset": ELEV_OFF, "scale": ELEV_SCALE,
               "meaning": "ground height m = offset + value * scale, 6 x 6 mean of the 5 m grid", "data": b64(ev.astype("<u2"))},
    water={"dtype": "uint8", "encoding": "base64", "meaning": "1 = water (majority of the 5 m water mask)",
           "data": b64(WATER.astype(np.uint8))},
    slope={"dtype": "uint8", "encoding": "base64", "scale": 0.5, "meaning": "slope deg = value * scale, 30 m grid",
           "data": b64(np.clip(np.round(SLOPE / 0.5), 0, 255).astype(np.uint8))},
    visibility={"dtype": "uint16" if NP <= 16 else "uint32", "endian": "little", "encoding": "base64",
                "meaning": "bit k set = peaks[k] summit in line of sight from the cell (eye 1.7 m, curvature, k 0.13)",
                "data": b64(bits.astype(vis_dtype))},
    peak_criteria=dict(source="find_true_peaks, topological_navigation/sprint2_localization.py",
                       min_altitude_m=2500, isolation_window_m=800, min_prominence_proxy_m=150,
                       smoothing_sigma_m=5, prominence_window_m=1000, dem="5 m texture grid of the tile",
                       extra="tops within 150 m of the tile edge dropped; named from terrain_meta peaks within 250 m"),
    peaks=[{k: p[k] for k in ("id", "name", "verified", "source", "lon", "lat", "elev_m", "x_m", "y_m", "row", "col",
                              "tpi_m", "isolation_m", "visible_share", "name_offset_m")} for p in peaks],
    demo=demo,
    line_of_sight=dict(eye_height_m=EYE_M, step_m=STEP, terrain="10 m grid, bilinear", skip_near_m=NEAR,
                       skip_summit_m=FAR, curvature="d^2 (1 - k) / (2 R)"),
    generated_by="terrain_viewer/scripts/team8_solver_grid.py",
    credits=meta.get("credits", []),
)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(out, f, separators=(",", ":"))
log("wrote", OUT, round(os.path.getsize(OUT) / 1024), "KB")
