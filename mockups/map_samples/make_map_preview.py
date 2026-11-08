"""Map preview for the Code Metal Forge land-nav idea (Team-8).

Reads the USGS 3DEP 1 m DEM mosaic that Rohan shared (OpenTopography, Mount Moran area),
and renders the layers we would show the judges:

  00_hero.png              final fix (posterior + back-bearing lines) and the safe route
  01_basemap.png           hillshade + elevation tint + contours + peaks + lake
  02_localization_steps.png  start, barometer band, first bearing, two chosen peaks
  03_route_healthy_vs_injured.png  slope limit 30 deg vs 18 deg
  04_skyline.png           what the hidden true point sees (poster material, from the DEM)
  05_where_it_works.png    how many peaks are visible from each spot

It also runs a Monte Carlo check and writes stats.json.
All numbers are a simulation on the same DEM, so real-world error (pointing, DEM vs reality,
peaks outside the data) is not included yet.

Usage: python -I make_map_preview.py <dem.tif> <out_dir> <scratch_dir>
"""
import sys, os, json, math, time
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import transform as rtransform
from scipy import ndimage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra
from PIL import Image, ImageDraw, ImageFont

DEM, OUT, SCR = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(OUT, exist_ok=True)
os.makedirs(SCR, exist_ok=True)
T0 = time.time()


def log(*a):
    print(f"[{time.time() - T0:6.1f}s]", *a, flush=True)


# ---------------------------------------------------------------- grids
F = 6  # 5 m pixels per 30 m cell
with rasterio.open(DEM) as ds:
    B, CRS = ds.bounds, ds.crs
    W30 = int(round((B.right - B.left) / 30.0))
    H30 = int(round((B.top - B.bottom) / 30.0))
    W5, H5 = W30 * F, H30 * F
    Z5 = ds.read(1, out_shape=(H5, W5), resampling=Resampling.average).astype(np.float32)
    NATIVE = dict(width=ds.width, height=ds.height, res_m=list(ds.res), crs=ds.crs.to_string(),
                  file_mb=round(os.path.getsize(DEM) / 1e6, 1))
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
S30max = S5.reshape(H30, F, W30, F).max(axis=(1, 3))  # steepest 5 m patch inside each 30 m cell

# lakes are hydro-flattened in 3DEP, so "perfectly flat and big" = water
rng5 = ndimage.maximum_filter(Z5, 5) - ndimage.minimum_filter(Z5, 5)
flat = rng5 < 0.15
lab, nlab = ndimage.label(flat)
sizes = ndimage.sum(flat, lab, np.arange(1, nlab + 1))
big = np.zeros(nlab + 1, bool)
big[1:] = sizes > 300
lake5 = ndimage.binary_closing(big[lab], iterations=2)
lake30 = lake5.reshape(H30, F, W30, F).mean(axis=(1, 3)) > 0.5
VALID = ~lake30
log("lake share", round(float(lake5.mean()), 3))


# ---------------------------------------------------------------- base map
def hillshade(Z, rx, ry, alt=42.0):
    gy, gx = np.gradient(Z, ry, rx)
    nx, ny = -gx, gy  # rows go south, so dz/dnorth = -gy
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
log("base done")

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
log("peaks", [(p["name"], round(p["z"]), p["iso_m"]) for p in peaks])

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
log("visibility done", [p["visible_share"] for p in peaks])

# ---------------------------------------------------------------- localization model (grid Bayes filter)
def vm(d, s):
    return np.exp((np.cos(np.radians(d)) - 1.0) / math.radians(s) ** 2)


ELEVm = np.where(VIS, ELEV, -np.inf)
KT = np.argmax(ELEVm, axis=0)              # "tallest-looking" visible peak = largest elevation angle
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
    anyk = np.max(np.where(VIS, vm(obs - BEAR, sc), 0.0), axis=0)  # robustness: user picked another peak
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
        score = pv * (1 - abs(zc))  # visible AND its bearing still varies over the candidates
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
            P = norm(P * (1 - 0.9 * VIS[k]))  # "I can't see that one" is information too
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


# example run (the story for the judges)
rr_, cc_ = np.mgrid[0:H30, 0:W30]
EDGE_M = np.minimum(np.minimum(rr_, H30 - 1 - rr_) * RY, np.minimum(cc_, W30 - 1 - cc_) * RX)
MORAN = [i for i, p in enumerate(peaks) if p["name"] == "Mt Moran"]
see_moran = VIS[MORAN[0]] if MORAN else np.ones((H30, W30), bool)

# Monte Carlo (same random places and noise across conditions)
pool_mc = np.flatnonzero(VALID & (NVIS >= 3) & (S30 < 35))
pts = np.random.default_rng(42).choice(pool_mc, size=min(200, pool_mc.size), replace=False)
CONDS = [("good phone, compass 3 deg, barometer 10 m", 3, 10, 0.0),
         ("typical phone, compass 6 deg, barometer 15 m", 6, 15, 0.0),
         ("bad, compass 10 deg, barometer 40 m", 10, 40, 0.0),
         ("declination not corrected, +10.5 deg bias", 6, 15, 10.5)]
MC = []
for name, sc, sb, bias in CONDS:
    rs = [trial(int(t), np.random.default_rng(int(t)), sc, sb, bias) for t in pts]
    rs = [r for r in rs if r]
    errs = np.array([r["err"] for r in rs])
    MC.append(dict(condition=name, n=len(rs), median_err_m=round(float(np.median(errs))),
                   p90_err_m=round(float(np.percentile(errs, 90))),
                   within_100m=round(float(np.mean(errs <= 100)), 3),
                   truth_inside_95pct_region=round(float(np.mean([r["in95"] for r in rs])), 3),
                   median_95pct_area_km2=round(float(np.median([r["area95_km2"] for r in rs])), 3)))
    log("MC", MC[-1])


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
            v = 0.6 * 6.0 * np.exp(-3.5 * np.abs(g + 0.05))  # km/h, Tobler 1993, off-path x0.6
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


# example run for the story: a typical case (error near the Monte Carlo median, truth inside the 95% region).
# Preference order: the 3 bearings go to 3 different peaks (the model guessed the first peak right), the
# tallest-looking peak is not the highest one, Mt Moran in view with 4+ peaks, one "can't see it" answer.
# The Monte Carlo above is the honest overall number.
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
RT = {"healthy": a, "injured": route(SRC, DST, LIM_INJ)}
log("example score", best_score)
log("example tried", tried, "err m", round(EX["err"]), "area95", round(EX["area95_km2"], 3), "obs", EX["obs"], "misses", EX["misses"])
for k_, v_ in RT.items():
    log("route", k_, None if v_ is None else (round(v_["length_m"]), round(v_["time_s"] / 60), round(v_["climb_m"])))


# ---------------------------------------------------------------- drawing helpers
FONT = r"C:/Windows/Fonts/NotoSansJP-VF.ttf"
_fc = {}


def font(sz, bold=False):
    key = (sz, bold)
    if key not in _fc:
        try:
            f = ImageFont.truetype(FONT, sz)
            try:
                f.set_variation_by_axes([700 if bold else 500])
            except Exception:
                pass
        except Exception:
            f = ImageFont.truetype(r"C:/Windows/Fonts/arialbd.ttf" if bold else r"C:/Windows/Fonts/arial.ttf", sz)
        _fc[key] = f
    return _fc[key]


def txt(d, xy, s, sz=26, fill=(240, 240, 240), bold=False, anchor="la"):
    d.text(xy, s, font=font(sz, bold), fill=fill, stroke_width=max(2, sz // 9), stroke_fill=(8, 10, 12), anchor=anchor)


def star(d, x, y, R=16, fill=(255, 255, 255)):
    pts = []
    for i in range(10):
        a = math.pi / 2 + i * math.pi / 5
        rr = R if i % 2 == 0 else R * 0.45
        pts.append((x + rr * math.cos(a), y - rr * math.sin(a)))
    d.polygon(pts, fill=fill, outline=(0, 0, 0))


def tri(d, x, y, s=11, fill=(250, 250, 250)):
    d.polygon([(x, y - s), (x - s * 0.9, y + s * 0.6), (x + s * 0.9, y + s * 0.6)], fill=fill, outline=(0, 0, 0))


def cross(d, x, y, s=14, fill=(40, 225, 255), w=4):
    for col, ww in (((0, 0, 0), w + 3), (fill, w)):
        d.line([(x - s, y), (x + s, y)], fill=col, width=ww)
        d.line([(x, y - s), (x, y + s)], fill=col, width=ww)


def up(a, shape, order=1):
    return ndimage.zoom(a.astype(np.float32), (shape[0] / a.shape[0], shape[1] / a.shape[1]), order=order)


def wash(rgb, mask_or_q, color, amax, gamma=1.0):
    q = up(mask_or_q, rgb.shape[:2], 1)
    a = (np.clip(q, 0, 1) ** gamma * amax)[..., None]
    return (rgb.astype(np.float32) * (1 - a) + np.array(color, np.float32) * a).astype(np.uint8)


def outline30(mask30, shape, color, rgb):
    m = up(mask30.astype(np.float32), shape, 0) > 0.5
    e = m & ~ndimage.binary_erosion(m, iterations=2)
    rgb = rgb.copy()
    rgb[e] = color
    return rgb


def px30(i, w, h):
    r, c = divmod(int(i), W30)
    return (c + 0.5) * w / W30, (r + 0.5) * h / H30


def pxpeak(p, w, h):
    return (p["c5"] + 0.5) * w / W5, (p["r5"] + 0.5) * h / H5


def deco(d, w, h, s=1.0, labels=True, top=0):
    for p in peaks:
        x, y = pxpeak(p, w, h)
        if y < top:
            continue
        tri(d, x, y, 10 * s)
        if labels:
            lab_ = f"Mt Moran {p['z']:.0f} m" if p["name"] == "Mt Moran" else f"{p['z']:.0f}"
            txt(d, (x + 12 * s, y - 4 * s), lab_, int(19 * s))
    pxkm = 1000.0 / (RX5 * W5 / w)
    x0, y0 = 28 * s, h - 34 * s
    d.rectangle([x0, y0, x0 + pxkm, y0 + 8 * s], fill=(240, 240, 240), outline=(0, 0, 0))
    txt(d, (x0, y0 - 30 * s), "1 km", int(20 * s))
    nx, ny = w - 46 * s, 54 * s
    d.polygon([(nx, ny - 30 * s), (nx - 12 * s, ny + 8 * s), (nx, ny), (nx + 12 * s, ny + 8 * s)], fill=(240, 240, 240), outline=(0, 0, 0))
    txt(d, (nx, ny + 12 * s), "N", int(20 * s), anchor="ma")


def back_bearing(d, k, obs, w, h, s=1.0, color=(255, 214, 102)):
    x, y = pxpeak(peaks[k], w, h)
    L = float(DIST[k].flat[EX["est"]]) * 1.12
    th = math.radians(obs + 180.0)
    x2 = x + math.sin(th) * L / (RX5 * W5 / w)
    y2 = y - math.cos(th) * L / (RY5 * H5 / h)
    d.line([(x, y), (x2, y2)], fill=(0, 0, 0), width=int(5 * s))
    d.line([(x, y), (x2, y2)], fill=color, width=int(3 * s))


def fmt_t(sec):
    m = int(round(sec / 60))
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


CYAN = (40, 225, 255)
LINES = EX["obs"] if EX["obs"][0][0] == EX["k1_true"] else EX["obs"][1:]


def header(rgb, h):
    rgb = rgb.copy()
    rgb[:h] = (rgb[:h].astype(np.float32) * 0.45).astype(np.uint8)
    return rgb


RED = (235, 64, 52)


def save(img, name):
    path = os.path.join(OUT, name)
    img.save(path, optimize=True)
    log("saved", path)


# ---------------------------------------------------------------- 01 basemap
im = Image.fromarray(header(BASE, 108))
d = ImageDraw.Draw(im)
deco(d, W5, H5, 1.3, top=130)
txt(d, (30, 24), "Mount Moran area, Grand Teton NP  |  USGS 3DEP 1 m DEM (shown at 5 m)", 30, bold=True)
txt(d, (30, 66), "Hillshade + elevation tint + 100 m contours + lake (hydro-flattened)", 24, fill=(200, 210, 215))
save(im, "01_basemap.png")

# ---------------------------------------------------------------- 02 localization steps (2x2)
hw, hh = W5 // 2, H5 // 2
BASE_H = np.asarray(Image.fromarray(BASE).resize((hw, hh), Image.LANCZOS))
obs_txt = ", ".join(f"{peaks[k]['name']} {o:.0f}°" for k, o in EX["obs"][1:])
titles = [
    ("1  GPS is jammed", "You could be anywhere in the downloaded 10 x 8 km area"),
    ("2  Barometer", f"{EX['alt']:.0f} m (±25 m)  →  only this contour band fits"),
    ("3  'Point at the tallest-looking peak'", f"bearing {EX['obs'][0][1]:.0f}°  →  a few curves are left"),
    ("4  Assistant picks 2 more peaks", f"{obs_txt}  →  95% region {EX['area95_km2']:.2f} km², error {EX['err']:.0f} m"),
]
panels = []
for i, P in enumerate(EX["steps"]):
    q = P / P.max()
    if i == 0:
        rgb = wash(BASE_H, VALID.astype(np.float32), CYAN, 0.18)
    else:
        rgb = wash(BASE_H, q, CYAN, 0.85, 0.5)
    if i == 3:
        rgb = outline30(EX["m95"], (hh, hw), (255, 255, 255), rgb)
    pim = Image.fromarray(header(rgb, 92))
    pd = ImageDraw.Draw(pim)
    deco(pd, hw, hh, 0.8, labels=(i >= 2), top=104)
    if i == 3:
        for k, o in LINES:
            back_bearing(pd, k, o, hw, hh, 0.8)
        ex_, ey_ = px30(EX["est"], hw, hh)
        cross(pd, ex_, ey_, 12)
        tx_, ty_ = px30(TR, hw, hh)
        star(pd, tx_, ty_, 13)
    txt(pd, (22, 16), titles[i][0], 30, bold=True)
    txt(pd, (22, 56), titles[i][1], 21, fill=(205, 232, 240))
    pd.rectangle([0, 0, hw - 1, hh - 1], outline=(10, 12, 14), width=3)
    panels.append(pim)
grid = Image.new("RGB", (hw * 2, hh * 2 + 44), (14, 17, 20))  # footer band keeps the legend off the maps
for i, pim in enumerate(panels):
    grid.paste(pim, ((i % 2) * hw, (i // 2) * hh))
gd = ImageDraw.Draw(grid)
txt(gd, (22, hh * 2 + 22), "cyan = where you can be     ★ = hidden true spot (revealed at the end)     + = estimate     yellow = back-bearing lines",
    20, fill=(220, 220, 220), anchor="lm")
save(grid, "02_localization_steps.png")


# ---------------------------------------------------------------- 03 healthy vs injured route
def route_panel(key, lim, title):
    nogo = ((S30 > lim) | (S30max > CLIFF)) & VALID
    rgb = wash(BASE_H, nogo.astype(np.float32), RED, 0.42)
    rgb = wash(rgb, EX["steps"][3] / EX["steps"][3].max(), CYAN, 0.8, 0.5)
    pim = Image.fromarray(header(rgb, 120))
    pd = ImageDraw.Draw(pim)
    deco(pd, hw, hh, 0.8, labels=False, top=132)
    rt = RT[key]
    if rt:
        pts_ = [px30(i, hw, hh) for i in rt["path"]]
        pd.line(pts_, fill=(0, 0, 0), width=9, joint="curve")
        pd.line(pts_, fill=(255, 255, 255) if key == "healthy" else (255, 225, 90), width=5, joint="curve")
        sub = f"{rt['length_m'] / 1000:.1f} km, climb {rt['climb_m']:.0f} m, about {fmt_t(rt['time_s'] * (1 if key == 'healthy' else 2))}"
    else:
        sub = "No route under this limit. Stay, signal, wait."
    sx, sy = px30(SRC, hw, hh)
    cross(pd, sx, sy, 12)
    dx_, dy_ = px30(DST, hw, hh)
    pd.ellipse([dx_ - 11, dy_ - 11, dx_ + 11, dy_ + 11], fill=(255, 255, 255), outline=(0, 0, 0), width=3)
    txt(pd, (dx_ + 14, dy_ - 30), "Rendezvous", 20)
    txt(pd, (22, 16), title, 30, bold=True)
    txt(pd, (22, 56), sub, 22, fill=(230, 236, 240))
    txt(pd, (22, 90), f"red = steeper than {lim}° (30 m average) or a cliff (5 m slope over {CLIFF}°)", 18, fill=(240, 190, 185))
    pd.rectangle([0, 0, hw - 1, hh - 1], outline=(10, 12, 14), width=3)
    return pim


rp = Image.new("RGB", (hw * 2, hh))
rp.paste(route_panel("healthy", LIM_OK, f"Healthy, slopes up to {LIM_OK}°"), (0, 0))
rp.paste(route_panel("injured", LIM_INJ, f"Ankle injured, slopes up to {LIM_INJ}°"), (hw, 0))
save(rp, "03_route_healthy_vs_injured.png")

# ---------------------------------------------------------------- 00 hero (one screen for the table)
Pf = EX["steps"][3]
rgb = wash(BASE, (((S30 > LIM_OK) | (S30max > CLIFF)) & VALID).astype(np.float32), RED, 0.35)
rgb = wash(rgb, Pf / Pf.max(), CYAN, 0.85, 0.5)
rgb = outline30(EX["m95"], (H5, W5), (255, 255, 255), rgb)
im = Image.fromarray(header(rgb, 118))
d = ImageDraw.Draw(im)
deco(d, W5, H5, 1.3, top=130)
for k, o in LINES:
    back_bearing(d, k, o, W5, H5, 1.3)
rt = RT["healthy"]
if rt:
    pts_ = [px30(i, W5, H5) for i in rt["path"]]
    d.line(pts_, fill=(0, 0, 0), width=13, joint="curve")
    d.line(pts_, fill=(255, 255, 255), width=7, joint="curve")
sx, sy = px30(SRC, W5, H5)
cross(d, sx, sy, 18, w=5)
tx_, ty_ = px30(TR, W5, H5)
star(d, tx_, ty_, 17)
dx_, dy_ = px30(DST, W5, H5)
d.ellipse([dx_ - 15, dy_ - 15, dx_ + 15, dy_ + 15], fill=(255, 255, 255), outline=(0, 0, 0), width=4)
txt(d, (dx_ + 20, dy_ - 40), "Rendezvous", 28)
txt(d, (30, 24), "No GPS. Fixed from the terrain with 3 peaks and the barometer.", 38, bold=True)
txt(d, (30, 76), f"95% region {EX['area95_km2']:.2f} km²  |  error {EX['err']:.0f} m (simulated)  |  "
               + (f"safe route {rt['length_m'] / 1000:.1f} km, about {fmt_t(rt['time_s'])}" if rt else "no safe route"), 26,
    fill=(205, 232, 240))
save(im, "00_hero.png")


# ---------------------------------------------------------------- 04 skyline from the hidden true point
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


azs, dists, ang, edge = skyline(TR)
PXD = 10
top_deg, bot_deg = 32, -12
Wsk, Hsk = int(360 * PXD), int((top_deg - bot_deg) * PXD) + 120
sk = np.zeros((Hsk, Wsk, 3), np.float32)
sky_t, sky_b = np.array((14, 20, 28), np.float32), np.array((40, 52, 64), np.float32)
base_y = int(top_deg * PXD) + 40
fr = np.clip(np.arange(Hsk) / base_y, 0, 1)[:, None, None]
sk[:] = sky_t * (1 - fr) + sky_b * fr
cols_x = (azs * PXD).astype(int)
layers = [(14000, (150, 156, 160)), (6000, (112, 118, 120)), (3000, (78, 84, 86)), (1200, (48, 54, 56))]
for dlim, col in layers:
    hz = ang[:, dists <= dlim].max(1)
    ytop = (base_y - hz * PXD).astype(int)
    for x, yt in zip(cols_x, ytop):
        sk[max(0, yt):, x:x + 2] = col
img = Image.fromarray(np.clip(sk, 0, 255).astype(np.uint8))
d = ImageDraw.Draw(img)
for a_, lab_ in ((0, "N"), (90, "E"), (180, "S"), (270, "W")):
    txt(d, (a_ * PXD + 4, Hsk - 44), lab_, 26, bold=True)
for a_ in range(0, 360, 10):
    d.line([(a_ * PXD, Hsk - 12), (a_ * PXD, Hsk)], fill=(200, 200, 200), width=2)
near = edge < 5000
for x, nr_ in zip(cols_x, near):
    if nr_:
        d.line([(x, Hsk - 6), (x + 2, Hsk - 6)], fill=RED, width=6)
labs = []
for k, p in enumerate(peaks):
    if VIS[k].flat[TR]:
        b_, e_, dist_ = float(BEAR[k].flat[TR]), float(ELEV[k].flat[TR]), float(DIST[k].flat[TR])
        x, y = b_ * PXD, base_y - e_ * PXD
        tri(d, x, y - 8, 9, fill=(255, 214, 102))
        lab_ = (p["name"] if p["name"] == "Mt Moran" else f"{p['z']:.0f} m")
        labs.append((x, y, f"{lab_}  {b_:.0f}°  {dist_ / 1000:.1f} km"))
placed = []  # stagger labels upward when they would overlap a label already drawn
for x, y, s_ in sorted(labs):
    w_ = 10.5 * len(s_)
    anc = "lb" if x < 160 else ("rb" if x > Wsk - 160 else "mb")
    x0 = x if anc == "lb" else (x - w_ if anc == "rb" else x - w_ / 2)
    ty = y - 26
    while any(x0 < b1 + 14 and x0 + w_ > b0 - 14 and abs(ty - t1) < 24 for b0, b1, t1 in placed):
        ty -= 24
    placed.append((x0, x0 + w_, ty))
    if ty < y - 26:
        d.line([(x, y - 20), (x, ty + 2)], fill=(255, 214, 102), width=1)
    txt(d, (x, ty), s_, 20, anchor=anc)
txt(d, (16, 12), "Skyline from the hidden true spot, rendered from the DEM (vertical angles true to scale)", 26, bold=True)
txt(d, (16, 48), "red tick at the bottom = data ends within 5 km in that direction, so the real horizon there is missing", 20,
    fill=(240, 190, 185))
save(img, "04_skyline.png")

# ---------------------------------------------------------------- 05 where the fix works
cls = np.where(NVIS >= 3, 1.0, 0.0)
rgb = wash(BASE, ((NVIS <= 1) & VALID).astype(np.float32), (10, 10, 12), 0.55)
rgb = wash(rgb, ((NVIS == 2) & VALID).astype(np.float32), (120, 200, 190), 0.30)
rgb = wash(rgb, ((NVIS >= 3) & VALID).astype(np.float32), (60, 220, 200), 0.42)
im = Image.fromarray(header(rgb, 118))
d = ImageDraw.Draw(im)
deco(d, W5, H5, 1.3, top=130)
cov3 = float((NVIS[VALID] >= 3).mean())
cov2 = float((NVIS[VALID] == 2).mean())
txt(d, (30, 24), "Where a 3-peak fix works (inside this data only)", 38, bold=True)
txt(d, (30, 76), f"teal = 3+ of the {K} peaks visible ({cov3 * 100:.0f}% of land)   light teal = 2 ({cov2 * 100:.0f}%)   dark = 0 or 1, climb first",
    24, fill=(205, 232, 240))
save(im, "05_where_it_works.png")

# ---------------------------------------------------------------- data size on device (measured)
SIZES = {}
for res, arr in (("5m", Z5), ("10m", Z5.reshape(H5 // 2, 2, W5 // 2, 2).mean(axis=(1, 3))), ("30m", Z30)):
    fn = os.path.join(SCR, f"dem_{res}.tif")
    h_, w_ = arr.shape
    tfm = from_origin(B.left, B.top, (B.right - B.left) / w_, (B.top - B.bottom) / h_)
    with rasterio.open(fn, "w", driver="GTiff", height=h_, width=w_, count=1, dtype="float32", crs=CRS, transform=tfm,
                       compress="deflate", predictor=3, tiled=True) as dst:
        dst.write(arr.astype(np.float32), 1)
    SIZES[res] = round(os.path.getsize(fn) / 1e6, 2)
area_km2 = (B.right - B.left) * (B.top - B.bottom) / 1e6
log("sizes MB", SIZES, "area km2", round(area_km2, 1))


# ---------------------------------------------------------------- stats
def lonlat_cell(i):
    r, c = divmod(int(i), W30)
    x, y = B.left + (c + 0.5) * RX, B.top - (r + 0.5) * RY
    lo, la = rtransform(CRS, "EPSG:4326", [x], [y])
    return round(la[0], 5), round(lo[0], 5)


stats = dict(
    native=NATIVE, area_km2=round(area_km2, 1), bounds_utm=list(B), lake_share=round(float(lake5.mean()), 3),
    grid_30m=[H30, W30], peaks=peaks,
    coverage=dict(share_3plus=round(cov3, 3), share_2=round(cov2, 3)),
    example=dict(truth_latlon=lonlat_cell(TR), truth_elev_m=round(float(Z30.flat[TR])), est_latlon=lonlat_cell(EX["est"]),
                 err_m=round(EX["err"]), area95_km2=round(EX["area95_km2"], 3), baro_obs_m=round(EX["alt"]),
                 first_peak_true=peaks[EX["k1_true"]]["name"], first_peak_model_guess=peaks[EX["obs"][0][0]]["name"],
                 bearings=[dict(asked="tallest-looking" if i == 0 else "by name",
                                peak=peaks[EX["k1_true"] if i == 0 else k]["name"], obs_deg=round(o, 1))
                           for i, (k, o) in enumerate(EX["obs"])],
                 not_visible_asks=[peaks[k]["name"] for k in EX["misses"]]),
    monte_carlo=MC,
    routes={k: (None if v is None else dict(limit_deg=v["limit_deg"], length_km=round(v["length_m"] / 1000, 2),
                                             climb_m=round(v["climb_m"]), time_min=round(v["time_s"] / 60)))
            for k, v in RT.items()},
    rendezvous_latlon=lonlat_cell(DST),
    on_device_mb=dict(native_1m_uncompressed=NATIVE["file_mb"], **{f"{k}_deflate": v for k, v in SIZES.items()}),
    assumptions=["same DEM for truth and model (no DEM-vs-reality error)", "peaks outside the data are ignored",
                 "user can tell which peak is meant (predicted skyline shown)", "clear view, eye height 1.7 m",
                 "Tobler off-path speed; injured time doubled"],
)
with open(os.path.join(OUT, "stats.json"), "w", encoding="utf-8") as f:
    json.dump(stats, f, ensure_ascii=False, indent=1)
log("done")
