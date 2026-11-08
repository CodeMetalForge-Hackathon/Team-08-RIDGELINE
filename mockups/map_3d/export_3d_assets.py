"""Export the assets for the Team-8 3D terrain-fix viewer (index.html, deck.gl 9 TerrainLayer).

The model part is copied from _ai/workspace/2026-11-07_codemetal_map_preview/make_map_preview.py with the
same parameters and seeds, so the example run is the same one as in the sample images (error 42 m,
95% region 0.12 km2). That script is not changed and not imported (it does all its work on import).

Writes into <out_dir>/data/
  terrain_rgb.png        Mapbox terrain-RGB on an EPSG:4326 grid, 1025 x 1025 px (about 10 m E-W, 8 m N-S)
                         h = -10000 + (R*65536 + G*256 + B) * 0.1
  base.png               hillshade x elevation tint, lake, contours (same look as 01_basemap.png), about 5 m
  prob_0.png .. prob_3.png   the 4 steps of the position probability, cyan with alpha
  nogo_30.png, nogo_18.png   red no-go area for the slope limit (30 m mean slope over the limit, or a 5 m cliff)
  coverage.png           where a 3-peak fix works (teal 3+, light teal 2, dark 0 or 1)
and <out_dir>/state.json  bounds, decoder, peaks, observations, stages, 95% outlines, routes, truth, rendezvous

The 1 m source stays where it is. Only these small derived files are written.

Usage: python -I export_3d_assets.py <dem.tif> <out_dir> [--stats <sample stats.json>]
"""
import sys, os, json, math, time, argparse
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin, from_bounds
from rasterio.warp import transform as rtransform, reproject
from scipy import ndimage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra
from PIL import Image
import cv2

ap = argparse.ArgumentParser()
ap.add_argument("dem")
ap.add_argument("out")
ap.add_argument("--stats", default=None, help="stats.json of the sample run, to copy the Monte Carlo table")
ARGS = ap.parse_args()
DEM, OUT = ARGS.dem, ARGS.out
DATA = os.path.join(OUT, "data")
os.makedirs(DATA, exist_ok=True)
T0 = time.time()


def log(*a):
    print(f"[{time.time() - T0:6.1f}s]", *a, flush=True)


# =============================================================== model (copied from make_map_preview.py)
# ---------------------------------------------------------------- grids
F = 6  # 5 m pixels per 30 m cell
with rasterio.open(DEM) as ds:
    B, CRS = ds.bounds, ds.crs
    W30 = int(round((B.right - B.left) / 30.0))
    H30 = int(round((B.top - B.bottom) / 30.0))
    W5, H5 = W30 * F, H30 * F
    Z5 = ds.read(1, out_shape=(H5, W5), resampling=Resampling.average).astype(np.float32)
RX5 = (B.right - B.left) / W5
RY5 = (B.top - B.bottom) / H5
RX, RY = RX5 * F, RY5 * F
Z30 = Z5.reshape(H30, F, W30, F).mean(axis=(1, 3))
log("grid", Z5.shape, Z30.shape, round(RX5, 3), round(RY5, 3))


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


# ---------------------------------------------------------------- base map
def hillshade(Z, rx, ry, alt=42.0):
    gy, gx = np.gradient(Z, ry, rx)
    nx, ny = -gx, gy
    nrm = np.sqrt(nx * nx + ny * ny + 1.0)
    out = np.zeros_like(Z)
    a = math.radians(alt)
    for az, w in ((315, 0.45), (270, 0.2), (360, 0.2), (225, 0.15)):
        t = math.radians(az)
        lx, ly, lz = math.sin(t) * math.cos(a), math.cos(t) * math.cos(a), math.sin(a)
        out += w * np.clip((nx * lx + ny * ly + lz) / nrm, 0, 1)
    return out


HS = hillshade(Z5, RX5, RY5)
STOPS = [(2050, (36, 48, 46)), (2300, (54, 68, 60)), (2700, (88, 94, 86)),
         (3100, (130, 128, 120)), (3500, (178, 176, 170)), (3860, (226, 228, 230))]


def tint(Z):
    e = [s[0] for s in STOPS]
    return np.stack([np.interp(Z, e, [s[1][i] for s in STOPS]) for i in range(3)], -1)


def contour_edges(Z, step):
    c = np.floor(Z / step)
    e = np.zeros(Z.shape, bool)
    e[:, 1:] |= c[:, 1:] != c[:, :-1]
    e[1:, :] |= c[1:, :] != c[:-1, :]
    return e


def make_base():
    rgb = tint(Z5) * (0.28 + 0.9 * HS)[..., None]
    rgb[lake5] = (22, 42, 64)
    shore = lake5 & ~ndimage.binary_erosion(lake5, iterations=2)
    rgb[shore] = (80, 118, 146)
    e100 = contour_edges(Z5, 100) & ~lake5
    e500 = ndimage.binary_dilation(contour_edges(Z5, 500)) & ~lake5
    for m, a in ((e100, 0.13), (e500, 0.22)):
        rgb[m] = rgb[m] * (1 - a) + 255 * a
    return np.clip(rgb, 0, 255).astype(np.uint8)


BASE = make_base()

# ---------------------------------------------------------------- peaks
mf = ndimage.maximum_filter(Z30, size=15)
cand = Z30 >= mf
mg = 8
cand[:mg, :] = False
cand[-mg:, :] = False
cand[:, :mg] = False
cand[:, -mg:] = False
peaks = []
for r, c in zip(*np.nonzero(cand)):
    z = Z30[r, c]
    hr, hc = np.nonzero(Z30 > z + 1.0)
    iso = float(np.min(np.hypot((hr - r) * RY, (hc - c) * RX))) if hr.size else 1e9
    win = Z30[max(0, r - 50):r + 51, max(0, c - 50):c + 51]
    relief = float(z - np.percentile(win, 10))
    if iso >= 700 and relief >= 300:
        r0, c0 = max(0, (r - 1) * F), max(0, (c - 1) * F)
        blk = Z5[r0:(r + 2) * F, c0:(c + 2) * F]
        br, bc = np.unravel_index(np.argmax(blk), blk.shape)
        peaks.append(dict(z=float(blk.max()), r5=int(r0 + br), c5=int(c0 + bc), iso_m=round(iso), relief_m=round(relief)))
peaks.sort(key=lambda p: -p["z"])
peaks = peaks[:12]
xs = [B.left + (p["c5"] + 0.5) * RX5 for p in peaks]
ys = [B.top - (p["r5"] + 0.5) * RY5 for p in peaks]
lons, lats = rtransform(CRS, "EPSG:4326", xs, ys)
for p, lo, la in zip(peaks, lons, lats):
    p.update(lon=round(lo, 5), lat=round(la, 5))
    moran = abs(la - 43.8350) < 0.004 and abs(lo + 110.7766) < 0.005 and p["z"] > 3800
    p["name"] = "Mt Moran" if moran else f"Pk {p['z']:.0f}"
K = len(peaks)
log("peaks", [(p["name"], round(p["z"])) for p in peaks])

# ---------------------------------------------------------------- visibility and geometry per peak
R_E, KREF, EYE = 6371000.0, 0.13, 1.7
CURV = (1 - KREF) / (2 * R_E)
rows_g, cols_g = np.mgrid[0:H30, 0:W30].astype(np.float32)
TS = np.linspace(0.02, 0.97, 120).astype(np.float32)[:, None, None]
VIS = np.zeros((K, H30, W30), bool)
BEAR = np.zeros((K, H30, W30), np.float32)
ELEV = np.zeros((K, H30, W30), np.float32)
DIST = np.zeros((K, H30, W30), np.float32)
for k, p in enumerate(peaks):
    pr = (p["r5"] + 0.5) / F - 0.5
    pc = (p["c5"] + 0.5) / F - 0.5
    zp = p["z"]
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
        DIST[k, r0:r1] = D
        sr = np.clip(np.rint(R + TS * (pr - R)), 0, H30 - 1).astype(np.int32)
        sc = np.clip(np.rint(C + TS * (pc - C)), 0, W30 - 1).astype(np.int32)
        zs = Z30[sr, sc] - CURV * (TS * D) ** 2
        zl = zo + TS * (zpe - zo)
        VIS[k, r0:r1] = ~np.any(zs > zl + 2.0, axis=0) & (D > 90)
    p["visible_share"] = round(float(VIS[k][VALID].mean()), 3)
NVIS = VIS.sum(0)
log("visibility done")


# ---------------------------------------------------------------- localization model (grid Bayes filter)
def vm(d, s):
    return np.exp((np.cos(np.radians(d)) - 1.0) / math.radians(s) ** 2)


ELEVm = np.where(VIS, ELEV, -np.inf)
KT = np.argmax(ELEVm, axis=0)
HAS = np.isfinite(ELEVm.max(axis=0))
PRED1 = np.take_along_axis(BEAR, KT[None], 0)[0]


def norm(P):
    P = np.where(VALID, P, 0.0)
    s = P.sum()
    return P / s if s > 0 else P


def like_baro(alt, sb):
    return np.exp(-0.5 * ((Z30 - alt) / sb) ** 2)


def like_tallest(obs, sc):
    main = vm(obs - PRED1, sc)
    anyk = np.max(np.where(VIS, vm(obs - BEAR, sc), 0.0), axis=0)
    return np.where(HAS, 0.8 * main + 0.2 * anyk, 1e-3)


def like_named(k, obs, sc):
    return np.where(VIS[k], vm(obs - BEAR[k], sc), 0.02)


def choose_next(P, used):
    best = None
    for k in range(K):
        if k in used:
            continue
        w = P * VIS[k]
        pv = float(w.sum())
        if pv < 0.3:
            continue
        zc = complex((w * np.exp(1j * np.radians(BEAR[k]))).sum()) / pv
        score = pv * (1 - abs(zc))
        if best is None or score > best[0]:
            best = (score, k)
    return None if best is None else best[1]


def hpd(P, mass):
    f = P.ravel()
    o = np.argsort(f)[::-1]
    cs = np.cumsum(f[o])
    n = int(np.searchsorted(cs, mass)) + 1
    m = np.zeros(f.size, bool)
    m[o[:n]] = True
    return m.reshape(P.shape)


def cell_xy(i):
    r, c = divmod(int(i), W30)
    return c * RX, r * RY


def trial(tr, rng, sc_true, sb_true, bias=0.0, sc_m=6.0, sb_m=25.0, record=False):
    if not HAS.flat[tr]:
        return None
    steps = []
    P = norm(np.ones((H30, W30)))
    if record:
        steps.append(P.copy())
    alt = float(Z30.flat[tr] + rng.normal(0, sb_true))
    P = norm(P * like_baro(alt, sb_m))
    if record:
        steps.append(P.copy())
    k1 = int(KT.flat[tr])
    o1 = float(BEAR[k1].flat[tr] + bias + rng.normal(0, sc_true))
    P = norm(P * like_tallest(o1, sc_m))
    if record:
        steps.append(P.copy())
    k1_hat = int(np.bincount(KT.ravel(), weights=P.ravel(), minlength=K).argmax())
    used = {k1_hat}
    obs, misses = [(k1_hat, o1)], []
    for _ in range(2):
        for _try in range(5):
            k = choose_next(P, used)
            if k is None:
                break
            used.add(k)
            if VIS[k].flat[tr]:
                o = float(BEAR[k].flat[tr] + bias + rng.normal(0, sc_true))
                P = norm(P * like_named(k, o, sc_m))
                obs.append((k, o))
                break
            P = norm(P * (1 - 0.9 * VIS[k]))
            misses.append(k)
    if record:
        steps.append(P.copy())
    est = int(np.argmax(ndimage.gaussian_filter(P, 0.7)))
    ex, ey = cell_xy(est)
    tx, ty = cell_xy(tr)
    m95 = hpd(P, 0.95)
    res = dict(err=math.hypot(ex - tx, ey - ty), in95=bool(m95.flat[tr]),
               area95_km2=float(m95.sum() * RX * RY / 1e6), n_bearings=len(obs), misses=len(misses))
    if record:
        res.update(steps=steps, est=est, obs=obs, misses=misses, m95=m95, alt=alt, k1_true=k1)
    return res


rr_, cc_ = np.mgrid[0:H30, 0:W30]
EDGE_M = np.minimum(np.minimum(rr_, H30 - 1 - rr_) * RY, np.minimum(cc_, W30 - 1 - cc_) * RX)
MORAN = [i for i, p in enumerate(peaks) if p["name"] == "Mt Moran"]
see_moran = VIS[MORAN[0]] if MORAN else np.ones((H30, W30), bool)

# ---------------------------------------------------------------- routing (Tobler off-path speed, slope limit)
CLIFF = 45


def route(src, dst, limit):
    ok = VALID & (S30 <= limit) & (S30max <= CLIFF)
    ok.flat[src] = True
    ok.flat[dst] = True
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
    d, pred = dijkstra(G, directed=True, indices=src, return_predecessors=True)
    if not np.isfinite(d[dst]):
        return None
    path = [dst]
    while path[-1] != src:
        path.append(int(pred[path[-1]]))
    path.reverse()
    seg = list(zip(path[:-1], path[1:]))
    length = sum(math.hypot(*np.subtract(cell_xy(a), cell_xy(b))) for a, b in seg)
    climb = sum(max(0.0, float(Z30.flat[b] - Z30.flat[a])) for a, b in seg)
    return dict(path=path, time_s=float(d[dst]), length_m=length, climb_m=climb, limit_deg=limit)


labl, nl = ndimage.label(lake30)
bigl = labl == (1 + int(np.argmax(ndimage.sum(lake30, labl, np.arange(1, nl + 1))))) if nl else lake30
shore = VALID & ndimage.binary_dilation(bigl, iterations=1) & (S30 < 12)
sr_, sc_ = np.nonzero(shore)
LIM_OK, LIM_INJ = 30, 18


def rendezvous(src):
    er, ec = divmod(int(src), W30)
    j = int(np.argmin(np.hypot((sr_ - er) * RY, (sc_ - ec) * RX)))
    return int(sr_[j] * W30 + sc_[j])


# example run, same selection rule and seeds as the sample
base_ex = VALID & (Z30 > 2450) & (Z30 < 2950) & (S30 < 30) & (EDGE_M >= 1500)
prim = base_ex & (NVIS >= 4) & see_moran
cands = list(np.random.default_rng(7).permutation(np.flatnonzero(prim))) + \
    list(np.random.default_rng(8).permutation(np.flatnonzero(base_ex & (NVIS >= 3) & ~prim)))[:250]
best, best_score, tried = None, None, 0
for tr in cands:
    tried += 1
    ex = trial(int(tr), np.random.default_rng(1), 5.0, 12.0, record=True)
    if not ex or ex["n_bearings"] < 3 or not ex["in95"] or not (40 <= ex["err"] <= 150):
        continue
    src = ex["est"]
    dst = rendezvous(src)
    a = route(src, dst, LIM_OK)
    if not a or a["length_m"] > 9000:
        continue
    score = (ex["obs"][0][0] == ex["k1_true"], ex["k1_true"] != 0, bool(prim.flat[tr]), len(ex["misses"]) > 0)
    if best_score is None or score > best_score:
        best, best_score = (int(tr), ex, src, dst, a), score
    if all(score):
        break
TR, EX, SRC, DST, a = best
RT = {LIM_OK: a, LIM_INJ: route(SRC, DST, LIM_INJ)}
log("example tried", tried, "err m", round(EX["err"]), "area95", round(EX["area95_km2"], 3),
    "obs", [(peaks[k]["name"], round(o, 1)) for k, o in EX["obs"]], "misses", EX["misses"])


def skyline(tr, step=0.2, ds=15.0, dmax=14000.0):
    r, c = divmod(int(tr), W30)
    zo = Z30[r, c] + EYE
    azs = np.arange(0, 360, step)
    dists = np.arange(ds, dmax, ds)
    th = np.radians(azs)[:, None]
    rr = r - np.cos(th) * dists[None] / RY
    cc = c + np.sin(th) * dists[None] / RX
    inside = (rr >= 0) & (rr <= H30 - 1) & (cc >= 0) & (cc <= W30 - 1)
    zs = ndimage.map_coordinates(Z30, [np.clip(rr, 0, H30 - 1).ravel(), np.clip(cc, 0, W30 - 1).ravel()], order=1).reshape(rr.shape)
    ang = np.degrees(np.arctan2(zs - CURV * dists[None] ** 2 - zo, dists[None]))
    ang = np.where(inside, ang, -90.0)
    edge = np.where(inside.all(1), dmax, dists[np.argmin(inside, axis=1)])
    return azs, dists, ang, edge


# =============================================================== export (new in this script)
T5 = from_origin(B.left, B.top, RX5, RY5)
T30 = from_origin(B.left, B.top, RX, RY)


def ll(xs_, ys_):
    lo, la = rtransform(CRS, "EPSG:4326", list(map(float, xs_)), list(map(float, ys_)))
    return np.asarray(lo), np.asarray(la)


def cell_center_utm(i):
    r, c = divmod(int(i), W30)
    return B.left + (c + 0.5) * RX, B.top - (r + 0.5) * RY


# ---------------------------------------------------------------- the lon/lat box inside the UTM rectangle
n_ = 400
t_ = np.linspace(0.0, 1.0, n_)
lo_b, la_b = ll(B.left + t_ * (B.right - B.left), np.full(n_, B.bottom))
lo_r, la_r = ll(np.full(n_, B.right), B.bottom + t_ * (B.top - B.bottom))
lo_t, la_t = ll(B.left + t_ * (B.right - B.left), np.full(n_, B.top))
lo_l, la_l = ll(np.full(n_, B.left), B.bottom + t_ * (B.top - B.bottom))
MARGIN = 0.00008  # about 6 to 9 m, so every output pixel has data under it
WEST, EAST = float(lo_l.max()) + MARGIN, float(lo_r.min()) - MARGIN
SOUTH, NORTH = float(la_b.max()) + MARGIN, float(la_t.min()) - MARGIN
LAT0 = 0.5 * (SOUTH + NORTH)
M_LAT = 111132.954 - 559.822 * math.cos(2 * math.radians(LAT0)) + 1.175 * math.cos(4 * math.radians(LAT0))
M_LON = 111412.84 * math.cos(math.radians(LAT0)) - 93.5 * math.cos(3 * math.radians(LAT0))
SPAN_E, SPAN_N = (EAST - WEST) * M_LON, (NORTH - SOUTH) * M_LAT
BOUNDS = [round(WEST, 7), round(SOUTH, 7), round(EAST, 7), round(NORTH, 7)]
log("lonlat bounds", BOUNDS, "span m", round(SPAN_E), round(SPAN_N))


def warp(src, src_tf, shape, res=Resampling.bilinear):
    dst = np.full(shape, np.nan, np.float32)
    reproject(np.ascontiguousarray(src, dtype=np.float32), dst, src_transform=src_tf, src_crs=CRS,
              dst_transform=from_bounds(WEST, SOUTH, EAST, NORTH, shape[1], shape[0]), dst_crs="EPSG:4326",
              resampling=res, src_nodata=np.nan, dst_nodata=np.nan)
    bad = ~np.isfinite(dst)
    if bad.any():  # fill the odd edge pixel from its nearest neighbour
        idx = ndimage.distance_transform_edt(bad, return_distances=False, return_indices=True)
        dst = dst[tuple(idx)]
    return dst, int(bad.sum())


# ---------------------------------------------------------------- terrain-RGB (square 2^n + 1 so deck.gl uses Martini)
TN = 1025
ZT, nbad = warp(Z5, T5, (TN, TN), Resampling.average)
v = np.round((ZT.astype(np.float64) + 10000.0) * 10.0).astype(np.int64)
trgb = np.stack([(v >> 16) & 255, (v >> 8) & 255, v & 255], -1).astype(np.uint8)
Image.fromarray(trgb).save(os.path.join(DATA, "terrain_rgb.png"), optimize=True)  # uint8 H x W x 3, read as RGB
dec = -10000.0 + ((trgb[..., 0].astype(np.int64) * 65536 + trgb[..., 1].astype(np.int64) * 256 + trgb[..., 2]) * 0.1)
log("terrain_rgb", (TN, TN), "filled", nbad, "decode err max", round(float(np.abs(dec - ZT).max()), 3),
    "range", round(float(ZT.min()), 1), round(float(ZT.max()), 1))
TT = from_bounds(WEST, SOUTH, EAST, NORTH, TN, TN)


def z_at(lon, lat, peak=False):
    """Terrain height of the deck.gl mesh grid at a point (bilinear), or the local max for a camera."""
    col, row = ~TT @ (lon, lat)
    col, row = col - 0.5, row - 0.5
    if peak:
        r0, c0 = int(round(row)), int(round(col))
        return float(ZT[max(0, r0 - 1):r0 + 2, max(0, c0 - 1):c0 + 2].max())
    return float(ndimage.map_coordinates(ZT, [[row], [col]], order=1)[0])


# ---------------------------------------------------------------- base texture, about 5 m
OW, OH = int(round(SPAN_E / 10.0)), int(round(SPAN_N / 10.0))  # overlay grid, about 10 m
BW, BH = OW * 2, OH * 2
base_ll = np.stack([warp(BASE[..., i], T5, (BH, BW))[0] for i in range(3)], -1)
Image.fromarray(np.clip(np.rint(base_ll), 0, 255).astype(np.uint8)).save(os.path.join(DATA, "base.png"), optimize=True)
log("base", (BH, BW))


def rgba_png(name, layers_):
    """layers_ = [(float mask on the 30 m grid, (r, g, b), alpha max, gamma)], composited bottom to top."""
    out_rgb = np.zeros((OH, OW, 3), np.float32)
    out_a = np.zeros((OH, OW), np.float32)
    for m30, col, amax, gamma in layers_:
        q = np.clip(warp(m30, T30, (OH, OW))[0], 0, 1)
        a_ = (q ** gamma) * amax
        na = a_ + out_a * (1 - a_)
        with np.errstate(invalid="ignore", divide="ignore"):
            out_rgb = np.where(na[..., None] > 0,
                               (np.array(col, np.float32) * a_[..., None] + out_rgb * (out_a * (1 - a_))[..., None]) / na[..., None],
                               0)
        out_a = na
    img = np.concatenate([np.clip(np.rint(out_rgb), 0, 255), np.clip(np.rint(out_a * 255), 0, 255)[..., None]], -1).astype(np.uint8)
    Image.fromarray(img).save(os.path.join(DATA, name), optimize=True)  # uint8 H x W x 4, read as RGBA


CYAN, RED, YELLOW = (40, 225, 255), (235, 64, 52), (255, 214, 102)

# ---------------------------------------------------------------- probability steps
STEPS = EX["steps"]
for s, P in enumerate(STEPS):
    if s == 0:
        rgba_png("prob_0.png", [(VALID.astype(np.float32), CYAN, 0.20, 1.0)])
    else:
        rgba_png(f"prob_{s}.png", [((P / P.max()).astype(np.float32), CYAN, 0.85, 0.5)])
log("prob pngs")

# ---------------------------------------------------------------- no-go and coverage
for lim in (LIM_OK, LIM_INJ):
    nogo = (((S30 > lim) | (S30max > CLIFF)) & VALID).astype(np.float32)
    rgba_png(f"nogo_{lim}.png", [(nogo, RED, 0.46, 1.0)])
rgba_png("coverage.png", [(((NVIS <= 1) & VALID).astype(np.float32), (10, 10, 12), 0.62, 1.0),
                          (((NVIS == 2) & VALID).astype(np.float32), (120, 200, 190), 0.40, 1.0),
                          (((NVIS >= 3) & VALID).astype(np.float32), (60, 220, 200), 0.55, 1.0)])
log("nogo and coverage pngs")


# ---------------------------------------------------------------- 95% outlines
def outline_95(P, up=4):
    f = np.sort(P.ravel())[::-1]
    n = int(np.searchsorted(np.cumsum(f), 0.95))
    thr = float(f[min(n, f.size - 1)])
    Pu = ndimage.zoom(P.astype(np.float64), up, order=1)
    hu, wu = Pu.shape
    mask = (Pu >= thr).astype(np.uint8)
    cs, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    rings = []
    for c in cs:
        if len(c) < 4:
            continue
        c = cv2.approxPolyDP(c, 1.0, True).reshape(-1, 2).astype(np.float64)
        if len(c) < 3:
            continue
        col = c[:, 0] * (W30 - 1) / (wu - 1)
        row = c[:, 1] * (H30 - 1) / (hu - 1)
        lo, la = ll(B.left + (col + 0.5) * RX, B.top - (row + 0.5) * RY)
        ring = [[round(float(x), 6), round(float(y), 6)] for x, y in zip(lo, la)]
        ring.append(ring[0])
        rings.append(ring)
    return rings


# ---------------------------------------------------------------- per-step story
OBS = EX["obs"]
truth_x, truth_y = cell_center_utm(TR)


def peak_view(eye_cell):
    out = []
    for k, p in enumerate(peaks):
        out.append(dict(peak=k, bearing=round(float(BEAR[k].flat[eye_cell]), 1),
                        elev_deg=round(float(ELEV[k].flat[eye_cell]), 2),
                        dist_km=round(float(DIST[k].flat[eye_cell]) / 1000, 2),
                        visible=bool(VIS[k].flat[eye_cell])))
    return out


def circ_mean(degs):
    z = np.mean(np.exp(1j * np.radians(degs)))
    return float(np.degrees(np.angle(z)) % 360)


def eye_for(cell, bearing, step):
    x, y = cell_center_utm(cell)
    lo, la = ll([x], [y])
    lo, la = float(lo[0]), float(la[0])
    zg = z_at(lo, la, peak=True)
    pv = peak_view(cell)
    az, dists, ang, edge = skyline(cell, step=0.5)
    layers_ = {}
    for dlim in (1200, 3000, 6000, 14000):
        layers_[str(dlim)] = [round(float(v_), 2) for v_ in ang[:, dists <= dlim].max(1)]
    vis_elev = [q["elev_deg"] for q in pv if q["visible"]]
    pitch = float(np.clip(np.median(vis_elev) * 0.6 if vis_elev else 4.0, 2.0, 10.0))
    return dict(lon=round(lo, 6), lat=round(la, 6), ground_z=round(zg, 1), z=round(zg + EYE, 1),
                bearing=round(bearing % 360, 1), pitch=round(pitch, 1), peaks=pv,
                skyline=dict(step_deg=0.5, layers=layers_, data_edge_m=[round(float(e_)) for e_ in edge]))


def ray_to_edge(k, obs_deg):
    p = peaks[k]
    x0 = B.left + (p["c5"] + 0.5) * RX5
    y0 = B.top - (p["r5"] + 0.5) * RY5
    th = math.radians(obs_deg + 180.0)
    dx, dy = math.sin(th), math.cos(th)
    ts = []
    for d_, lo_, hi_, v0 in ((dx, B.left, B.right, x0), (dy, B.bottom, B.top, y0)):
        if abs(d_) > 1e-9:
            ts.append(max((lo_ - v0) / d_, (hi_ - v0) / d_))
    L = min(ts) - 15.0
    dd = np.linspace(0, L, max(2, int(L / 60)))
    lo, la = ll(x0 + dx * dd, y0 + dy * dd)
    xe, ye = cell_center_utm(EX["est"])
    return dict(peak=k, obs_deg=round(obs_deg, 1), length_m=round(L),
                est_dist_m=round(math.hypot(xe - x0, ye - y0)),
                path=[[round(float(a_), 6), round(float(b_), 6)] for a_, b_ in zip(lo, la)])


LINES = [ray_to_edge(k, o) for k, o in OBS]

stage_est = [None]
for s in (1, 2):
    stage_est.append(int(np.argmax(ndimage.gaussian_filter(STEPS[s], 0.7))))
stage_est.append(int(EX["est"]))

next_by_stage = {0: [], 1: [], 2: [k for k, _ in OBS[1:]], 3: []}
obs_txt = ", ".join(f"{peaks[k]['name']} {o:.0f}°" for k, o in OBS[1:])
TITLES = [
    ("GPS is jammed", "You could be anywhere in the 10 x 8 km area on the device"),
    ("Barometer", f"{EX['alt']:.0f} m, ±25 m. Only this contour band fits."),
    ("Point at the peak that looks tallest", f"Bearing {OBS[0][1]:.0f}°. A few curves are left."),
    ("The assistant asks for 2 more peaks", f"{obs_txt}. Fix found."),
]
INSTR = [
    "Hold still. Reading the barometer.",
    "Point the box at the peak that looks tallest to you.",
    f"Point at the glowing peak, {peaks[OBS[1][0]]['name']}, about {OBS[1][1]:.0f}°.",
    "Fix found. Follow the white route to the rendezvous.",
]

stages = []
for s, P in enumerate(STEPS):
    est = stage_est[s]
    st = dict(i=s, title=TITLES[s][0], subtitle=TITLES[s][1], instruction=INSTR[s],
              prob=f"data/prob_{s}.png", lines=list(range(len(OBS))) if s == 3 else ([0] if s == 2 else []),
              next=next_by_stage[s])
    if s == 0:
        st.update(area95_km2=None, outline=[], estimate=None, eye=None, err_m=None, truth_in95=None)
    else:
        m95 = hpd(P, 0.95)
        ex_, ey_ = cell_center_utm(est)
        lo, la = ll([ex_], [ey_])
        if s == 1:
            k_ = int(KT.flat[est]) if HAS.flat[est] else (MORAN[0] if MORAN else 0)
            bearing = float(BEAR[k_].flat[est])
        elif s == 2:
            bearing = float(BEAR[OBS[1][0]].flat[est])
        else:
            bearing = circ_mean([o for _, o in OBS])
        st.update(area95_km2=round(float(m95.sum() * RX * RY / 1e6), 3), outline=outline_95(P),
                  estimate=dict(lon=round(float(lo[0]), 6), lat=round(float(la[0]), 6), z=round(z_at(float(lo[0]), float(la[0])), 1)),
                  err_m=round(math.hypot(ex_ - truth_x, ey_ - truth_y)), truth_in95=bool(m95.flat[TR]),
                  eye=eye_for(est, bearing, s))
    stages.append(st)
    log("stage", s, st["title"], "area95", st["area95_km2"], "outline rings", len(st["outline"]), "err", st["err_m"])


def lonlatz_cells(path):
    pts = []
    for i in path:
        x, y = cell_center_utm(i)
        lo, la = ll([x], [y])
        pts.append([round(float(lo[0]), 6), round(float(la[0]), 6), round(z_at(float(lo[0]), float(la[0])), 1)])
    return pts


def fmt_t(sec):
    m = int(round(sec / 60))
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


routes = {}
for lim, rt in RT.items():
    if rt is None:
        routes[str(lim)] = None
        continue
    routes[str(lim)] = dict(limit_deg=lim, length_km=round(rt["length_m"] / 1000, 2), climb_m=round(rt["climb_m"]),
                            time_min=round(rt["time_s"] / 60), time_text=fmt_t(rt["time_s"]),
                            path=lonlatz_cells(rt["path"]))


def point(cell):
    x, y = cell_center_utm(cell)
    lo, la = ll([x], [y])
    return dict(lon=round(float(lo[0]), 6), lat=round(float(la[0]), 6), z=round(z_at(float(lo[0]), float(la[0])), 1))


mc = []
if ARGS.stats and os.path.exists(ARGS.stats):
    with open(ARGS.stats, encoding="utf-8") as f:
        mc = json.load(f).get("monte_carlo", [])

cov3 = float((NVIS[VALID] >= 3).mean())
cov2 = float((NVIS[VALID] == 2).mean())
state = dict(
    schema="team8-3dmap/v1",
    note="Simulation on the same DEM. Real-world error (pointing, DEM vs reality, peaks outside the data) is not included.",
    bounds=BOUNDS,
    span_m=[round(SPAN_E), round(SPAN_N)],
    terrain=dict(image="data/terrain_rgb.png", size=[TN, TN], cell_m=[round(SPAN_E / (TN - 1), 2), round(SPAN_N / (TN - 1), 2)],
                 decoder=dict(rScaler=6553.6, gScaler=25.6, bScaler=0.1, offset=-10000),
                 elev_range_m=[round(float(ZT.min()), 1), round(float(ZT.max()), 1)], source_crs=CRS.to_string()),
    base=dict(image="data/base.png", size=[BW, BH]),
    overlays=dict(size=[OW, OH], nogo={str(LIM_OK): f"data/nogo_{LIM_OK}.png", str(LIM_INJ): f"data/nogo_{LIM_INJ}.png"},
                  coverage="data/coverage.png"),
    model_grid=dict(cell_m=[round(RX, 2), round(RY, 2)], size=[W30, H30]),
    peaks=[dict(id=k, name=p["name"], lon=p["lon"], lat=p["lat"], z=round(p["z"], 1), mesh_z=round(z_at(p["lon"], p["lat"], peak=True), 1),
                visible_share=p["visible_share"]) for k, p in enumerate(peaks)],
    observations=[dict(n=i + 1, step=2 if i == 0 else 3, asked="tallest-looking" if i == 0 else "by name", peak=int(k),
                       obs_deg=round(o, 1)) for i, (k, o) in enumerate(OBS)],
    first_peak_true=int(EX["k1_true"]),
    not_visible_asks=[int(k) for k in EX["misses"]],
    bearing_lines=LINES,
    sensors=dict(baro_obs_m=round(EX["alt"]), baro_sigma_m=25, compass_sigma_deg=6, gps_sigma_m=8,
                 eye_height_m=EYE),
    truth=point(TR),
    rendezvous=point(DST),
    stages=stages,
    routes=routes,
    slope_rule=dict(cliff_5m_deg=CLIFF, healthy_deg=LIM_OK, injured_deg=LIM_INJ, injured_time_factor=2),
    coverage=dict(share_3plus=round(cov3, 3), share_2=round(cov2, 3), peaks=K),
    monte_carlo=mc,
    credits=[
        "Elevation: USGS 3D Elevation Program (3DEP) 1 m DEM, public domain, U.S. Geological Survey",
        "Data access: OpenTopography (opentopography.org), which mosaicked the 3DEP tiles",
        "Projects WY_GrandTetonElkRefuge_2014, WY_GrandTetonNP_D22, WY_North_Converse_2020_D20",
        "Rendering: deck.gl 9.3.11, MIT License, Copyright vis.gl contributors",
    ],
    assumptions=["same DEM for truth and model (no DEM-vs-reality error)", "peaks outside the data are ignored",
                 "user can tell which peak is meant (predicted view shown)", "clear view, eye height 1.7 m",
                 "Tobler off-path speed, injured time doubled"],
)
with open(os.path.join(OUT, "state.json"), "w", encoding="utf-8") as f:
    json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
for fn in sorted(os.listdir(DATA)):
    log("file", fn, round(os.path.getsize(os.path.join(DATA, fn)) / 1e6, 2), "MB")
log("state.json", round(os.path.getsize(os.path.join(OUT, "state.json")) / 1e6, 2), "MB")
log("done")
