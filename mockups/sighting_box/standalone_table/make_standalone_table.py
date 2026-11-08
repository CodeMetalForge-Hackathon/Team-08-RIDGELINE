"""Build the stand-alone lookup table for the sighting box (Code Metal Forge 2026, Team-8).

When the box runs on its own battery (no PC, no phone) it cannot run the full grid Bayes filter.
So before the mission we precompute a table of candidate places. For each place we store what the
box would measure there (elevation angle of Mt Moran, the turn from Mt Moran to the 2nd and 3rd
peak, their elevation angles) and what to do (walking direction measured from Mt Moran, pace count
for the next leg). On the Arduino the box picks the nearest row.

No compass is needed. The first press on Mt Moran sets the gyro's zero, so every horizontal angle
the box uses is a difference from Mt Moran (the three-point resection idea from marine navigation).

Terrain rules (lake mask, slope limits, cliff rule, Tobler speed, lakeshore rendezvous) are copied
from make_map_preview.py so the numbers match the preview images and the phone screen.

Usage
  python -I make_standalone_table.py <dem_5m.tif> <stats.json> <out_dir>
         [--n 400] [--scene box_scene.json] [--phone-state phone_state.json] [--trials 3000]
"""
import argparse
import json
import math
import os
import time

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import transform as rtransform
from scipy import ndimage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra
from PIL import Image, ImageDraw, ImageFont

ap = argparse.ArgumentParser()
ap.add_argument("dem")
ap.add_argument("stats")
ap.add_argument("out")
ap.add_argument("--n", type=int, default=400, help="max rows (an Uno class board should stay near 400)")
ap.add_argument("--scene", default=None)
ap.add_argument("--phone-state", default=None)
ap.add_argument("--trials", type=int, default=3000)
A = ap.parse_args()
os.makedirs(A.out, exist_ok=True)
T0 = time.time()


def log(*a):
    print(f"[{time.time() - T0:6.1f}s]", *a, flush=True)


PEAK_NAMES = ["Mt Moran", "Pk 3666", "Pk 3388"]  # aim order: zero on Mt Moran, then the 2nd, then the 3rd
LEG_M = 500.0             # walk this far, then aim again
PACES_PER_M = 0.65        # left-foot pace count, about 65 per 100 m (varies by person and terrain)
SIG_ELEV, SIG_REL = 1.5, 2.5   # deg, typical box noise (pitch, gyro turn incl. aiming)
CLIFF, LIM_OK, LIM_INJ = 45, 30, 18
R_E, KREF, EYE = 6371000.0, 0.13, 1.7
CURV = (1 - KREF) / (2 * R_E)
F = 6

# ---------------------------------------------------------------- terrain (same rules as make_map_preview.py)
with rasterio.open(A.dem) as ds:
    B, CRS = ds.bounds, ds.crs
    W30 = int(round((B.right - B.left) / 30.0))
    H30 = int(round((B.top - B.bottom) / 30.0))
    W5, H5 = W30 * F, H30 * F
    Z5 = ds.read(1, out_shape=(H5, W5), resampling=Resampling.average).astype(np.float32)
RX5 = (B.right - B.left) / W5
RY5 = (B.top - B.bottom) / H5
RX, RY = RX5 * F, RY5 * F
Z30 = Z5.reshape(H30, F, W30, F).mean(axis=(1, 3))


def slope_deg(Z, rx, ry):
    gy, gx = np.gradient(Z, ry, rx)
    return np.degrees(np.arctan(np.hypot(gx, gy))).astype(np.float32)


S5 = slope_deg(Z5, RX5, RY5)
S30 = slope_deg(Z30, RX, RY)
S30max = S5.reshape(H30, F, W30, F).max(axis=(1, 3))
rng5 = ndimage.maximum_filter(Z5, 5) - ndimage.minimum_filter(Z5, 5)
flat = rng5 < 0.15
lab, nlab = ndimage.label(flat)
sizes = ndimage.sum(flat, lab, np.arange(1, nlab + 1))
big = np.zeros(nlab + 1, bool)
big[1:] = sizes > 300
lake5 = ndimage.binary_closing(big[lab], iterations=2)
lake30 = lake5.reshape(H30, F, W30, F).mean(axis=(1, 3)) > 0.5
VALID = ~lake30
RR, CC = np.mgrid[0:H30, 0:W30]
EDGE_M = np.minimum(np.minimum(RR, H30 - 1 - RR) * RY, np.minimum(CC, W30 - 1 - CC) * RX)
log("grid", Z30.shape, "lake share", round(float(lake5.mean()), 3))


def latlon_to_rc(lat, lon):
    xs, ys = rtransform("EPSG:4326", CRS, [lon], [lat])
    return (B.top - ys[0]) / RY - 0.5, (xs[0] - B.left) / RX - 0.5


def rc_to_latlon(r, c):
    x = B.left + (c + 0.5) * RX
    y = B.top - (r + 0.5) * RY
    lo, la = rtransform(CRS, "EPSG:4326", [x], [y])
    return la[0], lo[0]


def wrap(d):
    return (np.asarray(d) + 180.0) % 360.0 - 180.0


# ---------------------------------------------------------------- the three peaks, what the box would measure
stats = json.load(open(A.stats, encoding="utf-8"))
byname = {p["name"]: p for p in stats["peaks"]}
PK = []
for nm in PEAK_NAMES:
    p = byname[nm]
    pr, pc = latlon_to_rc(p["lat"], p["lon"])
    PK.append(dict(name=nm, r=pr, c=pc, z=float(p["z"]), lat=p["lat"], lon=p["lon"]))

K = len(PK)
rows_g, cols_g = np.mgrid[0:H30, 0:W30].astype(np.float32)
TS = np.linspace(0.02, 0.97, 120).astype(np.float32)[:, None, None]
VIS = np.zeros((K, H30, W30), bool)
BEAR = np.zeros((K, H30, W30), np.float32)
ELEV = np.zeros((K, H30, W30), np.float32)
for k, p in enumerate(PK):
    pr, pc, zp = p["r"], p["c"], p["z"]
    for r0 in range(0, H30, 24):
        r1 = min(H30, r0 + 24)
        R = rows_g[r0:r1]
        C = cols_g[r0:r1]
        dE = (pc - C) * RX
        dN = (R - pr) * RY
        D = np.hypot(dE, dN)
        zo = Z30[r0:r1] + EYE
        zpe = zp - CURV * D * D
        BEAR[k, r0:r1] = np.degrees(np.arctan2(dE, dN)) % 360
        ELEV[k, r0:r1] = np.degrees(np.arctan2(zpe - zo, D))
        sr = np.clip(np.rint(R + TS * (pr - R)), 0, H30 - 1).astype(np.int32)
        sc = np.clip(np.rint(C + TS * (pc - C)), 0, W30 - 1).astype(np.int32)
        zs = Z30[sr, sc] - CURV * (TS * D) ** 2
        zl = zo + TS * (zpe - zo)
        VIS[k, r0:r1] = ~np.any(zs > zl + 2.0, axis=0) & (D > 90)

# features in the order the box stores them: e0, r1, r2, e1, e2
FEAT = np.stack([ELEV[0], wrap(BEAR[1] - BEAR[0]), wrap(BEAR[2] - BEAR[0]), ELEV[1], ELEV[2]], -1)
IS_REL = np.array([False, True, True, False, False])
SIG = np.where(IS_REL, SIG_REL, SIG_ELEV)
ELIG = VALID & (S30 <= LIM_OK) & (S30max <= CLIFF) & VIS.all(0) & (EDGE_M >= 300)
E_IDX = np.flatnonzero(ELIG)
log("all three peaks visible on", round(float(ELIG.sum() * RX * RY / 1e6), 2), "km2,", E_IDX.size, "cells")

# ---------------------------------------------------------------- routes to the lakeshore (multi-source, reversed)
labl, nl = ndimage.label(lake30)
bigl = labl == (1 + int(np.argmax(ndimage.sum(lake30, labl, np.arange(1, nl + 1)))))
shore = VALID & ndimage.binary_dilation(bigl, iterations=1) & (S30 < 12)
SHORE = np.flatnonzero(shore)


def edge_time(a, b):
    ra, ca = divmod(int(a), W30)
    rb, cb = divmod(int(b), W30)
    L = math.hypot((rb - ra) * RY, (cb - ca) * RX)
    g = (Z30[rb, cb] - Z30[ra, ca]) / L
    v = 0.6 * 6.0 * math.exp(-3.5 * abs(g + 0.05))
    return L / (v / 3.6)


def to_shore(limit):
    ok = (VALID & (S30 <= limit) & (S30max <= CLIFF)) | shore
    idx = np.arange(H30 * W30).reshape(H30, W30)
    R_, C_, Wt = [], [], []
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            ra, rb = max(0, -dr), H30 - max(0, dr)
            ca, cb = max(0, -dc), W30 - max(0, dc)
            a = idx[ra:rb, ca:cb]
            b = idx[ra + dr:rb + dr, ca + dc:cb + dc]
            m = ok[ra:rb, ca:cb] & ok[ra + dr:rb + dr, ca + dc:cb + dc]
            L = math.hypot(dr * RY, dc * RX)
            g = (Z30[ra + dr:rb + dr, ca + dc:cb + dc] - Z30[ra:rb, ca:cb]) / L
            v = 0.6 * 6.0 * np.exp(-3.5 * np.abs(g + 0.05))
            R_.append(a[m])
            C_.append(b[m])
            Wt.append((L / (v / 3.6))[m])
    G = coo_matrix((np.concatenate(Wt), (np.concatenate(R_), np.concatenate(C_))), shape=(H30 * W30,) * 2).tocsr()
    d, pred, _ = dijkstra(G.T.tocsr(), directed=True, indices=SHORE, return_predecessors=True, min_only=True)
    return d, pred


def trace(i, d, pred):
    """Path from cell i to the shore. None when there is no route."""
    cur = int(i)
    path = [cur]
    if not np.isfinite(d[cur]):  # start cell steeper than the limit: allow one step out of it
        r, c = divmod(cur, W30)
        best = None
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                rr, cc = r + dr, c + dc
                if (dr or dc) and 0 <= rr < H30 and 0 <= cc < W30:
                    j = rr * W30 + cc
                    if np.isfinite(d[j]):
                        t = d[j] + edge_time(cur, j)
                        if best is None or t < best[0]:
                            best = (t, j)
        if best is None:
            return None
        cur = best[1]
        path.append(cur)
    while pred[cur] >= 0 and len(path) < 20000:
        cur = int(pred[cur])
        path.append(cur)
    P = np.array(path)
    r, c = np.divmod(P, W30)
    seg = np.hypot(np.diff(r) * RY, np.diff(c) * RX)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    j = min(int(np.searchsorted(cum, LEG_M)), len(P) - 1)
    leg = int(P[j])
    lr, lc = divmod(leg, W30)
    r0, c0 = divmod(int(i), W30)
    walk_abs = math.degrees(math.atan2((lc - c0) * RX, (r0 - lr) * RY)) % 360
    return dict(total_m=float(cum[-1]), leg_m=float(cum[j]), walk_abs=walk_abs, path=P)


d_ok, p_ok = to_shore(LIM_OK)
d_inj, p_inj = to_shore(LIM_INJ)
LEG = {}
for i in E_IDX:
    LEG[int(i)] = trace(i, d_ok, p_ok)
log("routes traced for", len(LEG), "cells, no route for", sum(v is None for v in LEG.values()))

# ---------------------------------------------------------------- candidate rows on a lattice
er, ec = np.divmod(E_IDX, W30)
for s in range(2, 60):
    m = (er % s == s // 2) & (ec % s == s // 2)
    if m.sum() <= A.n:
        break
CAND = E_IDX[m]
log("lattice step", s, "cells =", round(s * 30), "m, rows", CAND.size)

rows = []
for i in CAND:
    i = int(i)
    r, c = divmod(i, W30)
    f = FEAT[r, c]
    lg = LEG[i]
    flags = 0
    if lg is None:
        flags |= 1
        walk_rel, paces, total_hm = 0.0, 0, 0
    else:
        walk_rel = float(wrap(lg["walk_abs"] - BEAR[0, r, c]))
        paces = int(round(lg["leg_m"] * PACES_PER_M))
        total_hm = int(min(255, round(lg["total_m"] / 100.0)))
    if trace(i, d_inj, p_inj) is None:
        flags |= 2
    la, lo = rc_to_latlon(r, c)
    rows.append(dict(cell=i, lat=round(la, 6), lon=round(lo, 6), elev_m=round(float(Z30[r, c]), 1),
                     e0=float(f[0]), r1=float(f[1]), r2=float(f[2]), e1=float(f[3]), e2=float(f[4]),
                     walk_rel=walk_rel, paces=paces, total_hm=total_hm, flags=flags))
CF = np.array([[q["e0"], q["r1"], q["r2"], q["e1"], q["e2"]] for q in rows])
CWALK = np.array([q["walk_rel"] for q in rows])
CSTAY = np.array([bool(q["flags"] & 1) for q in rows])
CRC = np.array([divmod(q["cell"], W30) for q in rows])


def lookup(obs):
    dd = obs[:, None, :] - CF[None, :, :]
    dd[..., IS_REL] = wrap(dd[..., IS_REL])
    cost = ((dd / SIG) ** 2).sum(-1)
    k = np.argmin(cost, axis=1)
    return k, cost[np.arange(len(k)), k]


# ---------------------------------------------------------------- simulation: does the table point the right way?
rng = np.random.default_rng(11)
T = rng.choice(E_IDX, size=min(A.trials, E_IDX.size), replace=False)
TR, TC = np.divmod(T, W30)
TF = FEAT[TR, TC]
T_WALK = np.array([np.nan if LEG[int(t)] is None else LEG[int(t)]["walk_abs"] for t in T])
T_STAY = np.isnan(T_WALK)
VAL = {}
for name, se, sr in [("ideal (no noise, only the table's spacing)", 0.0, 0.0),
                     ("typical (pitch 1.5 deg, turn 2.5 deg)", 1.5, 2.5),
                     ("rough (pitch 3 deg, turn 5 deg)", 3.0, 5.0)]:
    noise = rng.normal(size=TF.shape) * np.where(IS_REL, sr, se)
    k, best = lookup(TF + noise)
    pos = np.hypot((CRC[k, 0] - TR) * RY, (CRC[k, 1] - TC) * RX)
    both = ~T_STAY & ~CSTAY[k]
    walk_used = BEAR[0, TR, TC] + CWALK[k]  # the person turns from the Mt Moran they really see
    derr = np.abs(wrap(walk_used - T_WALK))[both]
    VAL[name] = dict(
        trials=int(T.size),
        position_error_m=dict(median=round(float(np.median(pos))), p90=round(float(np.percentile(pos, 90))),
                              p99=round(float(np.percentile(pos, 99)))),
        walk_direction_error_deg=dict(median=round(float(np.median(derr)), 1),
                                      within_30=round(float((derr <= 30).mean()), 3),
                                      within_45=round(float((derr <= 45).mean()), 3),
                                      over_90=round(float((derr > 90).mean()), 3)),
        stay_agreement=round(float((T_STAY == CSTAY[k]).mean()), 3),
        cost_p99=round(float(np.percentile(best, 99)), 2))
    log(name, json.dumps(VAL[name]))
COST_MAX = VAL["rough (pitch 3 deg, turn 5 deg)"]["cost_p99"]

# ---------------------------------------------------------------- the demo spot (box_scene.json) and the phone's route
DEMO = None
if A.scene:
    sc = json.load(open(A.scene, encoding="utf-8"))
    vp = sc["viewpoint"]
    rf, cf = latlon_to_rc(vp["lat"], vp["lon"])
    r, c = int(round(rf)), int(round(cf))
    scene_pk = {p["name"]: p for p in sc["peaks"]}
    want = np.array([scene_pk["Mt Moran"]["elev_deg"],
                     wrap(scene_pk["Pk 3666"]["bearing_true"] - scene_pk["Mt Moran"]["bearing_true"]),
                     wrap(scene_pk["Pk 3388"]["bearing_true"] - scene_pk["Mt Moran"]["bearing_true"]),
                     scene_pk["Pk 3666"]["elev_deg"], scene_pk["Pk 3388"]["elev_deg"]], float)
    k, best = lookup(want[None, :])
    q = rows[int(k[0])]
    lg = trace(r * W30 + c, d_ok, p_ok)
    DEMO = dict(cell_features=[round(float(x), 2) for x in FEAT[r, c]], scene_features=[round(float(x), 2) for x in want],
                all_three_visible=bool(VIS[:, r, c].all()), eligible=bool(ELIG[r, c]),
                row=int(k[0]), row_cost=round(float(best[0]), 2),
                row_distance_m=round(float(math.hypot((q["cell"] // W30 - r) * RY, (q["cell"] % W30 - c) * RX))),
                box_says=dict(walk_from_moran_deg=round(q["walk_rel"], 1), paces=q["paces"], total_km=q["total_hm"] / 10,
                              stay=bool(q["flags"] & 1), stay_injured=bool(q["flags"] & 2)),
                box_walk_abs=round(float((scene_pk["Mt Moran"]["bearing_true"] + q["walk_rel"]) % 360), 1),
                own_route_walk_abs=None if lg is None else round(lg["walk_abs"], 1),
                own_route_total_km=None if lg is None else round(lg["total_m"] / 1000, 2))
    if A.phone_state:
        ps = json.load(open(A.phone_state, encoding="utf-8"))
        pts = ps["routes"]["healthy"]["points"]
        xs, ys = rtransform("EPSG:4326", CRS, [p["lon"] for p in pts], [p["lat"] for p in pts])
        xs, ys = np.array(xs), np.array(ys)
        cum = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(xs), np.diff(ys)))])
        j = min(int(np.searchsorted(cum, LEG_M)), len(xs) - 1)
        DEMO["phone_route_walk_abs"] = round(math.degrees(math.atan2(xs[j] - xs[0], ys[j] - ys[0])) % 360, 1)
        DEMO["phone_route_total_km"] = round(float(cum[-1]) / 1000, 2)
        DEMO["box_vs_phone_deg"] = round(float(abs(wrap(DEMO["box_walk_abs"] - DEMO["phone_route_walk_abs"]))), 1)
    log("demo", json.dumps(DEMO))

# ---------------------------------------------------------------- outputs
def i16(x):
    return int(max(-32768, min(32767, round(x * 10))))


H = []
H.append("// standalone_table.h  generated by make_standalone_table.py, do not edit by hand")
H.append("// Stand-alone mode of the sighting box. Code Metal Forge 2026, Team-8.")
H.append("// Aim order: Mt Moran (press = zero of the turn), then Pk 3666, then Pk 3388.")
H.append("// Angles are in 0.1 degree. Turns are clockwise positive (turning right, seen from above).")
H.append("// Elevation angles are up positive. walk = walking direction measured from Mt Moran, clockwise.")
H.append("// After walking `paces` left-foot paces, aim the three peaks again.")
H.append("// Terrain: USGS 3DEP via OpenTopography. Model output for a demo, not a safety tool.")
H.append("#pragma once")
H.append("#include <Arduino.h>")
H.append("")
H.append(f"#define ST_N {len(rows)}")
for j, nm in enumerate(PEAK_NAMES):
    H.append(f'#define ST_PEAK{j} "{nm}"')
H.append("#define ST_FLAG_STAY 1      // no safe route under 30 deg: Stay. Signal. Wait.")
H.append("#define ST_FLAG_STAY_INJ 2  // no route under 18 deg (injured)")
H.append(f"#define ST_W_ELEV {int(round((SIG_REL * 10) ** 2))}   // weights = 1 / sigma^2, scaled to integers")
H.append(f"#define ST_W_REL {int(round((SIG_ELEV * 10) ** 2))}")
H.append(f"#define ST_COST_MAX {int(round(COST_MAX * (SIG_ELEV * 10) ** 2 * (SIG_REL * 10) ** 2))}UL  // above this, ask to aim again")
H.append("")
H.append("typedef struct {")
H.append("  int16_t e0, r1, r2, e1, e2;  // what the box measures at this place (0.1 deg)")
H.append("  int16_t walk;                // walking direction from Mt Moran, clockwise (0.1 deg)")
H.append("  uint16_t paces;              // left-foot paces for this leg, then aim again")
H.append("  uint8_t total_hm;            // whole remaining route in 100 m")
H.append("  uint8_t flags;")
H.append("} StRow;")
H.append("")
H.append("const StRow ST_TABLE[ST_N] PROGMEM = {")
for q in rows:
    H.append(f"  {{{i16(q['e0'])}, {i16(q['r1'])}, {i16(q['r2'])}, {i16(q['e1'])}, {i16(q['e2'])}, "
             f"{i16(q['walk_rel'])}, {q['paces']}, {q['total_hm']}, {q['flags']}}},")
H.append("};")
H.append("")
H.append("static inline int32_t st_wrap(int32_t d) { while (d > 1800) d -= 3600; while (d < -1800) d += 3600; return d; }")
H.append("static inline uint32_t st_term(int32_t d, uint32_t w) { if (d < 0) d = -d; if (d > 1000) d = 1000; return w * (uint32_t)(d * d); }")
H.append("")
H.append("// e0, e1, e2: pitch at press 1, 2, 3. r1, r2: yaw at press 2 and 3 minus yaw at press 1. All in 0.1 deg.")
H.append("// Copies the nearest row into *out and returns its cost (compare with ST_COST_MAX).")
H.append("static inline uint32_t st_find(int16_t e0, int16_t r1, int16_t r2, int16_t e1, int16_t e2, StRow* out) {")
H.append("  uint32_t best = 0xFFFFFFFFUL;")
H.append("  StRow row;")
H.append("  for (uint16_t i = 0; i < ST_N; i++) {")
H.append("    memcpy_P(&row, &ST_TABLE[i], sizeof(StRow));")
H.append("    uint32_t c = st_term((int32_t)e0 - row.e0, ST_W_ELEV) + st_term(st_wrap((int32_t)r1 - row.r1), ST_W_REL)")
H.append("               + st_term(st_wrap((int32_t)r2 - row.r2), ST_W_REL) + st_term((int32_t)e1 - row.e1, ST_W_ELEV)")
H.append("               + st_term((int32_t)e2 - row.e2, ST_W_ELEV);")
H.append("    if (c < best) { best = c; *out = row; }")
H.append("  }")
H.append("  return best;")
H.append("}")
H.append("")
with open(os.path.join(A.out, "standalone_table.h"), "w", encoding="utf-8", newline="\n") as fh:
    fh.write("\n".join(H))

with open(os.path.join(A.out, "standalone_table.csv"), "w", encoding="utf-8", newline="\n") as fh:
    keys = ["lat", "lon", "elev_m", "e0", "r1", "r2", "e1", "e2", "walk_rel", "paces", "total_hm", "flags"]
    fh.write(",".join(keys) + "\n")
    for q in rows:
        fh.write(",".join(str(round(q[k], 2)) if isinstance(q[k], float) else str(q[k]) for k in keys) + "\n")

bytes_per_row = 5 * 2 + 2 + 2 + 1 + 1
SUMMARY = dict(peaks=PEAK_NAMES, rows=len(rows), lattice_m=round(s * 30), bytes=len(rows) * bytes_per_row,
               area_all_three_visible_km2=round(float(ELIG.sum() * RX * RY / 1e6), 2),
               share_of_land=round(float(ELIG.sum() / VALID.sum()), 3),
               leg_m=LEG_M, paces_per_100m=PACES_PER_M * 100, sigma_deg=dict(elev=SIG_ELEV, turn=SIG_REL),
               cost_max=COST_MAX, simulation=VAL, demo=DEMO,
               rules="30 m cells, lake mask from hydro-flattened 3DEP, slope limit 30 deg (injured 18), "
                     "5 m slope over 45 deg is a cliff, Tobler off-path speed x0.6, goal = nearest reachable lakeshore")
with open(os.path.join(A.out, "validation.json"), "w", encoding="utf-8", newline="\n") as fh:
    json.dump(SUMMARY, fh, ensure_ascii=False, indent=2)

# ---------------------------------------------------------------- picture: where the table works and which way it sends you
UP = 3
ls = np.radians(315)
gy, gx = np.gradient(Z30, RY, RX)
slope = np.arctan(np.hypot(gx, gy))
aspect = np.arctan2(-gx, gy)
hs = np.sin(np.radians(42)) * np.cos(slope) + np.cos(np.radians(42)) * np.sin(slope) * np.cos(ls - aspect)
g = np.clip(40 + 150 * np.clip(hs, 0, 1), 0, 255).astype(np.uint8)
rgb = np.stack([g, g, g], -1)
rgb[lake30] = (22, 42, 64)
tint = ELIG
rgb[tint] = (0.55 * rgb[tint] + 0.45 * np.array([40, 225, 255])).astype(np.uint8)
img = Image.fromarray(rgb).resize((W30 * UP, H30 * UP), Image.LANCZOS)
d = ImageDraw.Draw(img)
try:
    fnt = ImageFont.truetype(r"C:/Windows/Fonts/NotoSansJP-VF.ttf", 22)
    fsm = ImageFont.truetype(r"C:/Windows/Fonts/NotoSansJP-VF.ttf", 17)
except OSError:
    fnt = fsm = ImageFont.load_default()
for q in rows:
    r, c = divmod(q["cell"], W30)
    x, y = (c + 0.5) * UP, (r + 0.5) * UP
    if q["flags"] & 1:
        d.ellipse([x - 3, y - 3, x + 3, y + 3], fill=(235, 64, 52))
        continue
    a = math.radians(float(BEAR[0, r, c]) + q["walk_rel"])
    d.line([x, y, x + 11 * math.sin(a), y - 11 * math.cos(a)], fill=(255, 214, 102), width=2)
    d.ellipse([x - 2, y - 2, x + 2, y + 2], fill=(255, 255, 255))
for p in PK:
    x, y = (p["c"] + 0.5) * UP, (p["r"] + 0.5) * UP
    d.polygon([(x, y - 11), (x - 10, y + 7), (x + 10, y + 7)], fill=(250, 250, 250))
    d.text((x + 12, y - 12), p["name"], font=fsm, fill=(255, 255, 255))
if DEMO is not None:
    x, y = (cf + 0.5) * UP, (rf + 0.5) * UP
    d.line([x - 10, y - 10, x + 10, y + 10], fill=(40, 225, 255), width=4)
    d.line([x - 10, y + 10, x + 10, y - 10], fill=(40, 225, 255), width=4)
d.rectangle([0, 0, W30 * UP, 64], fill=(14, 17, 20))
d.text((14, 6), f"Stand-alone table: {len(rows)} places where Mt Moran, Pk 3666 and Pk 3388 are all visible (cyan)",
       font=fnt, fill=(240, 240, 240))
d.text((14, 36), "yellow tick = first walking direction, red dot = Stay. Signal. Wait., cyan X = demo spot, blue = lake",
       font=fsm, fill=(200, 210, 215))
img.save(os.path.join(A.out, "standalone_table_map.png"))
log("done", A.out)
