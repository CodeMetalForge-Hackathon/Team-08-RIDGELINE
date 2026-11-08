"""Team-8 mock run: grid Bayes localization (barometer, bearing to Mount Moran, elevation angle) and A* routes.

Reads the files written by team8_terrain_prep.py and writes:
  prob_1_baro.png, prob_2_bearing.png, prob_3_elev.png  (10 m grid, same bounds as the terrain)
      R = 255 * sqrt(P / Pmax), G = 255 inside the 95% region, B = 255 inside the 50% region
  sim.json   hidden truth, measurements, stage stats, estimate, routes (healthy 30 deg, injured 18 deg),
             phone legs, skyline from the estimate, peaks seen from the estimate

Everything is synthetic. The hidden point is a made-up demo spot, not a real person or position.
Usage: python -I team8_simulate.py <data_dir>
"""
import sys, os, json, math, time, heapq
import numpy as np
from scipy import ndimage
from PIL import Image
import contourpy
from rasterio.warp import transform as rtransform

DATA = sys.argv[1]
T0 = time.time()


def log(*a):
    print(f"[{time.time() - T0:5.1f}s]", *a, flush=True)


meta = json.load(open(os.path.join(DATA, "terrain_meta.json"), encoding="utf-8"))
W5, H5 = meta["grid"]["width"], meta["grid"]["height"]
west, south, east, north = meta["bounds"]
rgb = np.asarray(Image.open(os.path.join(DATA, meta["files"]["terrain_rgb"])).convert("RGB")).astype(np.int64)
Z5 = (-10000.0 + (rgb[..., 0] * 65536 + rgb[..., 1] * 256 + rgb[..., 2]) * 0.1).astype(np.float32)
LAKE5 = np.asarray(Image.open(os.path.join(DATA, meta["files"]["water_mask"]))) > 127
DX5, DY5 = meta["grid"]["cell_m"]
DECL = float(meta["declination"]["deg"])
N_MC = int(os.environ.get("TEAM8_MC", "200"))

# ------------------------------------------------------------ working grid (twice the texture cell: 10 m for lidar, 20 m otherwise)
F = 2
H, W = H5 // F, W5 // F
DX, DY = DX5 * F, DY5 * F
DLON, DLAT = (east - west) / W, (north - south) / H
Z = Z5.reshape(H, F, W, F).mean(axis=(1, 3))
LAKE = LAKE5.reshape(H, F, W, F).any(axis=(1, 3))
LAND = ~LAKE
gy5, gx5 = np.gradient(Z5.astype(np.float64), DY5, DX5)
S5 = np.degrees(np.arctan(np.hypot(gx5, gy5)))
CLIFF = S5.reshape(H, F, W, F).max(axis=(1, 3)) > 45.0          # any texture-cell patch steeper than 45 deg
ZS = ndimage.gaussian_filter(Z.astype(np.float64), 15.0 / DX)      # about 30 m footprint
SCAP = np.asarray(Image.open(os.path.join(DATA, meta["files"]["slope"]))).astype(np.float64) / 2.0   # same file the map hatches
ROWS, COLS = np.mgrid[0:H, 0:W]
XE = (COLS + 0.5) * DX                                              # metres east of the west edge
YN = (H - ROWS - 0.5) * DY                                          # metres north of the south edge
DIAG = math.hypot(W * DX, H * DY)


def rc_lonlat(r, c):
    return west + (c + 0.5) * DLON, north - (r + 0.5) * DLAT


def lonlat_rc(lon, lat):
    return (north - lat) / DLAT - 0.5, (lon - west) / DLON - 0.5


def z5_at(lons, lats):
    rr = (north - np.asarray(lats)) / (DLAT / F) - 0.5
    cc = (np.asarray(lons) - west) / (DLON / F) - 0.5
    return ndimage.map_coordinates(Z5, [rr, cc], order=1, mode="nearest")


R_E, KREF, EYE = 6371000.0, 0.13, 1.7
CURV = (1 - KREF) / (2 * R_E)
moran = next(p for p in meta["peaks"] if p["name"] == meta["landmark"])   # the landmark (Mount Moran in the first tile)
LM_NAME = moran["name"]
mr, mc = lonlat_rc(moran["lon"], moran["lat"])
XM, YM, ZM = (mc + 0.5) * DX, (H - mr - 0.5) * DY, moran["elev_m"]
DE, DN = XM - XE, YM - YN
DIST = np.hypot(DE, DN)
BEAR = (np.degrees(np.arctan2(DE, DN)) + 360) % 360
ELEV = np.degrees(np.arctan2(ZM - CURV * DIST ** 2 - (Z + EYE), DIST))

# visibility of the Moran summit from every cell (ray sampling)
VIS = np.zeros((H, W), bool)
TS = np.linspace(0.02, 0.985, 180)[:, None, None]
for r0 in range(0, H, 40):
    r1 = min(H, r0 + 40)
    R_, C_ = ROWS[r0:r1].astype(np.float64), COLS[r0:r1].astype(np.float64)
    zo = Z[r0:r1] + EYE
    D = DIST[r0:r1]
    zpe = ZM - CURV * D ** 2
    sr = np.clip(np.rint(R_ + TS * (mr - R_)), 0, H - 1).astype(np.int32)
    sc = np.clip(np.rint(C_ + TS * (mc - C_)), 0, W - 1).astype(np.int32)
    zs = Z[sr, sc] - CURV * (TS * D) ** 2
    VIS[r0:r1] = ~np.any(zs > zo + TS * (zpe - zo) + 2.0, axis=0) & (D > 120)
log(LM_NAME, "visible from", round(float(VIS[LAND].mean()), 3), "of land")


def angd(a, b):
    return (a - b + 180) % 360 - 180


def norm(P):
    P = np.where(LAND, P, 0.0)
    return P / P.sum()


def hpd(P, mass):
    f = P.ravel()
    sub = np.flatnonzero(f > f.max() * 1e-9)                  # the far tail holds no mass worth sorting
    o = sub[np.argsort(f[sub])[::-1]]
    n = int(np.searchsorted(np.cumsum(f[o]), mass)) + 1
    m = np.zeros(f.size, bool)
    m[o[:n]] = True
    return m.reshape(P.shape)


SIG_B, SIG_A, SIG_E = 30.0, 5.0, 1.0
NOISE = dict(baro_m=12.0, bearing_deg=2.0, elev_deg=-0.35)   # fixed, stated sensor errors for the demo


def localize(tr, tc, noise=NOISE):
    alt = float(Z[tr, tc] + noise["baro_m"])
    brg = float(BEAR[tr, tc] + noise["bearing_deg"])
    ela = float(ELEV[tr, tc] + noise["elev_deg"])
    P0 = norm(np.ones((H, W)))
    P1 = norm(P0 * np.exp(-0.5 * ((Z - alt) / SIG_B) ** 2))
    P2 = norm(P1 * np.exp(-0.5 * (angd(BEAR, brg) / SIG_A) ** 2) * np.where(VIS, 1.0, 0.02))
    P3 = norm(P2 * np.exp(-0.5 * ((ELEV - ela) / SIG_E) ** 2))
    k = int(np.argmax(ndimage.gaussian_filter(P3, 1.5)))
    er, ec = divmod(k, W)
    err = math.hypot((er - tr) * DY, (ec - tc) * DX)
    return dict(P=[P0, P1, P2, P3], alt=alt, brg=brg, ela=ela, est=(er, ec), err=err)


# hidden true spot: the same demo cell as the first preview figure (UTM 12N, about 2.6 km from Moran)
def good(tr, tc):
    if not (0 <= tr < H and 0 <= tc < W) or not LAND[tr, tc] or not VIS[tr, tc] or SCAP[tr, tc] > 25:
        return None
    L = localize(tr, tc)
    in95 = hpd(L["P"][3], 0.95)[tr, tc]
    return L if (in95 and L["err"] <= 150) else None


# ------------------------------------------------------------ routes (A*, Tobler off-trail speed, slope limit)
lake_big = ndimage.label(LAKE)[0]
if lake_big.max():
    sizes = ndimage.sum(LAKE, lake_big, np.arange(1, lake_big.max() + 1))
    main_lake = lake_big == (1 + int(np.argmax(sizes)))
else:
    main_lake = LAKE
flat_ok = ndimage.maximum_filter(SCAP, 7) < 6
EDGE_M = np.minimum(np.minimum(ROWS, H - 1 - ROWS) * DY, np.minimum(COLS, W - 1 - COLS) * DX)
LZ_LABEL = "Rendezvous, flat lakeshore"
lz_pool = np.flatnonzero(LAND & flat_ok & ~CLIFF & (EDGE_M > 100)
                         & ndimage.binary_dilation(main_lake, iterations=int(150 / DX)) & ~ndimage.binary_dilation(LAKE, iterations=2)) \
    if main_lake.sum() * DX * DY > 0.2e6 else np.zeros(0, int)
if lz_pool.size < 20:                                          # no big lake: the flattest low ground instead
    LZ_LABEL = "Rendezvous, flat valley floor"
    for lim in (6, 9, 12):
        lz_pool = np.flatnonzero(LAND & (ndimage.maximum_filter(SCAP, 7) < lim) & ~CLIFF & (EDGE_M > 100)
                                 & (Z < np.percentile(Z[LAND], 30)))
        if lz_pool.size >= 20:
            break


def pick_lz(er, ec):
    lz_d = np.hypot(XE.ravel()[lz_pool] - XE[er, ec], YN.ravel()[lz_pool] - YN[er, ec])
    return int(lz_pool[int(np.argmin(lz_d))])


VK = 0.6 * 6.0 / 3.6     # Tobler peak speed off-trail, m/s (at grade -0.05)


def astar(er, ec, LZ, cap):
    ok = (LAND & (SCAP <= cap) & ~CLIFF).ravel().tolist()
    start, goal = er * W + ec, LZ
    ok[start] = ok[goal] = True
    zl = ZS.ravel().tolist()
    gr_, gc_ = divmod(LZ, W)
    N = W * H
    g = [1e30] * N
    prev = [-1] * N
    closed = bytearray(N)
    steps = [(dr * W + dc, dr, dc, math.hypot(dr * DY, dc * DX)) for dr in (-1, 0, 1) for dc in (-1, 0, 1) if dr or dc]
    exp_, hyp = math.exp, math.hypot
    g[start] = 0.0
    heap = [(hyp((er - gr_) * DY, (ec - gc_) * DX) / VK, 0.0, start)]
    n_exp = 0
    while heap:
        _, gs, i = heapq.heappop(heap)
        if closed[i]:
            continue
        if i == goal:
            break
        closed[i] = 1
        n_exp += 1
        r, c = divmod(i, W)
        zi = zl[i]
        for off, dr, dc, L in steps:
            rr, cc = r + dr, c + dc
            if rr < 0 or rr >= H or cc < 0 or cc >= W:
                continue
            j = i + off
            if closed[j] or not ok[j]:
                continue
            ng = gs + L / (VK * exp_(-3.5 * abs((zl[j] - zi) / L + 0.05)))
            if ng < g[j]:
                g[j] = ng
                prev[j] = i
                heapq.heappush(heap, (ng + hyp((rr - gr_) * DY, (cc - gc_) * DX) / VK, ng, j))
    if g[goal] >= 1e30:
        return None, n_exp
    path = [goal]
    while path[-1] != start:
        path.append(prev[path[-1]])
    return path[::-1], n_exp


def tobler_s(L, dz):
    return L / (VK * math.exp(-3.5 * abs(dz / max(L, 1e-6) + 0.05)))


def build_route(cells, cap, speed=1.0):
    rc = np.array([divmod(i, W) for i in cells], np.float64)
    x, y = (rc[:, 1] + 0.5) * DX, (H - rc[:, 0] - 0.5) * DY
    if len(x) > 7:
        k = np.ones(5) / 5
        xs_, ys_ = np.convolve(x, k, "same"), np.convolve(y, k, "same")
        xs_[:2], ys_[:2], xs_[-2:], ys_[-2:] = x[:2], y[:2], x[-2:], y[-2:]
        x, y = xs_, ys_
    lons = west + x / (W * DX) * (east - west)
    lats = south + y / (H * DY) * (north - south)
    zz = z5_at(lons, lats)
    t, ts, length, climb, desc = 0.0, [0.0], 0.0, 0.0, 0.0
    for a in range(1, len(x)):
        L = math.hypot(x[a] - x[a - 1], y[a] - y[a - 1])
        dz = float(zz[a] - zz[a - 1])
        t += tobler_s(L, dz) / speed
        ts.append(round(t, 1))
        length += L
        climb += max(0.0, dz)
        desc += max(0.0, -dz)
    smax = float(max(SCAP[int(r_), int(c_)] for r_, c_ in rc))
    return dict(cap_deg=cap, speed_factor=speed, path=[[round(float(a), 6), round(float(b), 6), round(float(c), 1)] for a, b, c in zip(lons, lats, zz)],
                timestamps=ts, length_m=round(length), time_s=round(t), climb_m=round(climb), descent_m=round(desc),
                max_slope_deg=round(smax, 1), xy=list(zip(x.tolist(), y.tolist())))


def path_gap(a, b):
    A = np.array(a["xy"][::4])
    return max(float(np.min(np.hypot(A[:, 0] - x, A[:, 1] - y))) for x, y in b["xy"][::4])


def plan(tr, tc):
    L = good(tr, tc)
    if not L:
        return None
    er, ec = L["est"]
    LZ = pick_lz(er, ec)
    hc, _ = astar(er, ec, LZ, 30)
    if not hc:
        return None
    hr = build_route(hc, 30)
    if not (0.19 * DIAG <= hr["length_m"] <= 0.62 * DIAG):
        return None
    ic, _ = astar(er, ec, LZ, 18)
    ir = build_route(ic, 18, speed=0.5) if ic else None   # injured: 18 deg limit, half speed
    story = 2 if ir is None else (3 if (ir["length_m"] >= 1.2 * hr["length_m"] or path_gap(hr, ir) > 300) else 1)
    return dict(L=L, LZ=LZ, routes=dict(healthy=hr, injured=ir), tr=tr, tc=tc,
                score=(story, -abs(28 - hr["max_slope_deg"]), -L["err"]))


# hidden true spot: try the first preview figure's cell (UTM 12N), then search for a spot that tells the story
# (fix inside the 95% region and under 150 m, a 2.5 to 8 km walk out, and the 18 deg limit changes or kills the route)
cand = []
if meta.get("id") == "moran":    # keep the first preview figure's spot for the Moran story
    tx, ty = rtransform("EPSG:26912", "EPSG:4326", [515785.9], [4855038.4])
    cand = [tuple(int(round(v)) for v in lonlat_rc(tx[0], ty[0]))]
edge = EDGE_M
zl = Z[LAND]
pool = np.flatnonzero(LAND & VIS & (SCAP < 25) & (DIST > 0.15 * DIAG) & (DIST < 0.35 * DIAG) & (edge > 0.06 * DIAG)
                      & (Z > np.percentile(zl, 35)) & (Z < np.percentile(zl, 85)))
cand += [divmod(int(k), W) for k in np.random.default_rng(3).permutation(pool)[:90]]
best, tried = None, 0
for tr_, tc_ in cand:
    tried += 1
    pl = plan(tr_, tc_)
    if pl and (best is None or pl["score"] > best["score"]):
        best = pl
    if best and best["score"][0] == 3 and best["score"][1] > -6 and best["L"]["err"] <= 120:
        break
LOC, tr, tc, LZ, routes = best["L"], best["tr"], best["tc"], best["LZ"], best["routes"]
er, ec = LOC["est"]
lzr, lzc = divmod(LZ, W)
log("tried", tried, "score", best["score"], "truth", (tr, tc), "z", round(float(Z[tr, tc])), "dist to Moran",
    round(float(DIST[tr, tc])), "err", round(LOC["err"]))
for k_, v_ in routes.items():
    log("route", k_, None if not v_ else (v_["length_m"], round(v_["time_s"] / 60), v_["max_slope_deg"]))


# ------------------------------------------------------------ probability PNGs and stage stats
stages = []
keys = [("baro", "Barometer band"), ("bearing", f"Bearing to {LM_NAME}"), ("elev", "Elevation angle to the summit")]
for i, (key, title) in enumerate(keys, start=1):
    P = LOC["P"][i]
    m95, m50 = hpd(P, 0.95), hpd(P, 0.50)
    q = np.sqrt(P / P.max())
    img = np.stack([np.round(q * 255), m95 * 255, m50 * 255], -1).astype(np.uint8)
    fn = f"prob_{i}_{key}.png"
    Image.fromarray(img).save(os.path.join(DATA, fn), optimize=True)
    pk = int(np.argmax(P))
    rings = {}
    for nm, m_ in (("hpd95", m95), ("hpd50", m50)):            # region outlines as lon/lat rings, drawn as smooth lines
        lev = float(P[m_].min()) * 0.999
        out_r = []
        for ln in sorted(contourpy.contour_generator(z=P).lines(lev), key=len, reverse=True)[:30]:
            xy = np.asarray(ln)
            if len(xy) < 6:
                continue
            xy = xy[::max(1, len(xy) // 400)]
            out_r.append([[round(west + (x + 0.5) * DLON, 6), round(north - (y + 0.5) * DLAT, 6)] for x, y in xy])
        rings[nm] = out_r
    stages.append(dict(step=i, key=key, title=title, png=fn, hpd95_km2=round(float(m95.sum() * DX * DY / 1e6), 3),
                       hpd50_km2=round(float(m50.sum() * DX * DY / 1e6), 3), truth_in_hpd95=bool(m95[tr, tc]),
                       peak_lonlat=[round(v, 6) for v in rc_lonlat(*divmod(pk, W))], outline=rings))
    log("stage", i, key, stages[-1]["hpd95_km2"], "km2", "truth in 95%", stages[-1]["truth_in_hpd95"])

# ------------------------------------------------------------ the same model on many random spots (not only the demo spot)
rng = np.random.default_rng(7)
mc_pool = np.flatnonzero(LAND & VIS & (EDGE_M > 300) & (DIST > 300))
picks = rng.choice(mc_pool, size=min(N_MC, mc_pool.size), replace=False)
errs, cover, areas, errs_nd, cover_nd = [], [], [], [], []
for k in picks:
    r_, c_ = divmod(int(k), W)
    nz = dict(baro_m=float(rng.normal(0, SIG_B)), bearing_deg=float(rng.normal(0, SIG_A)), elev_deg=float(rng.normal(0, SIG_E)))
    for decl, e_, cv in ((0.0, errs, cover), (DECL, errs_nd, cover_nd)):
        L_ = localize(r_, c_, dict(nz, bearing_deg=nz["bearing_deg"] + decl))
        m_ = hpd(L_["P"][3], 0.95)
        e_.append(L_["err"])
        cv.append(bool(m_[r_, c_]))
        if decl == 0.0:
            areas.append(float(m_.sum() * DX * DY / 1e6))
MC = dict(n=len(errs), sigma=dict(baro_m=SIG_B, bearing_deg=SIG_A, elev_deg=SIG_E),
          landmark_visible_share=round(float(VIS[LAND].mean()), 3),
          median_err_m=round(float(np.median(errs))), p90_err_m=round(float(np.percentile(errs, 90))),
          truth_in_95_share=round(float(np.mean(cover)), 3), median_area95_km2=round(float(np.median(areas)), 3),
          no_declination=dict(bias_deg=round(DECL, 2), median_err_m=round(float(np.median(errs_nd))),
                              truth_in_95_share=round(float(np.mean(cover_nd)), 3)),
          note="Random land spots where the landmark is visible, random sensor noise at the stated sigma, same DEM for truth and model")
log("monte carlo", MC)

# ------------------------------------------------------------ phone legs (Douglas-Peucker on the healthy route)
DIRS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def dp(pts, eps):
    if len(pts) < 3:
        return [0, len(pts) - 1]
    a, b = np.array(pts[0]), np.array(pts[-1])
    ab = b - a
    n = np.hypot(*ab) or 1e-9
    d = [abs(ab[0] * (p[1] - a[1]) - ab[1] * (p[0] - a[0])) / n for p in pts[1:-1]]
    k = int(np.argmax(d)) + 1
    if d[k - 1] <= eps:
        return [0, len(pts) - 1]
    left, right = dp(pts[:k + 1], eps), dp(pts[k:], eps)
    return left[:-1] + [i + k for i in right]


legs = []
hr = routes.get("healthy")
if hr:
    idx = dp(hr["xy"], 45.0)
    for a, b in zip(idx[:-1], idx[1:]):
        (x0, y0), (x1, y1) = hr["xy"][a], hr["xy"][b]
        brg = (math.degrees(math.atan2(x1 - x0, y1 - y0)) + 360) % 360
        dz = hr["path"][b][2] - hr["path"][a][2]
        dist = sum(math.hypot(hr["xy"][k + 1][0] - hr["xy"][k][0], hr["xy"][k + 1][1] - hr["xy"][k][1]) for k in range(a, b))
        word = "climb" if dz > 15 else ("descend" if dz < -15 else "stay level")
        legs.append(dict(i0=a, i1=b, bearing_deg=round(brg, 1), dist_m=round(dist), dz_m=round(dz),
                         t0_s=hr["timestamps"][a], t1_s=hr["timestamps"][b], to=hr["path"][b][:2],
                         say=f"Head {DIRS[int((brg + 22.5) // 45) % 8]} ({brg:.0f}°) for {dist:.0f} m, {word}"
                             + (f" {abs(dz):.0f} m" if word != "stay level" else "")))
    log("legs", len(legs))
for r_ in routes.values():
    if r_:
        r_.pop("xy")

# ------------------------------------------------------------ skyline and peaks seen from the estimate
AZ_STEP = 0.25
azs = np.arange(0, 360, AZ_STEP)
dists = np.arange(15.0, 15000.0, 15.0)
z_eye = float(Z[er, ec] + EYE)
th = np.radians(azs)[:, None]
rr = er - np.cos(th) * dists[None] / DY
cc = ec + np.sin(th) * dists[None] / DX
inside = (rr >= 0) & (rr <= H - 1) & (cc >= 0) & (cc <= W - 1)
zs = ndimage.map_coordinates(Z, [np.clip(rr, 0, H - 1).ravel(), np.clip(cc, 0, W - 1).ravel()], order=1).reshape(rr.shape)
ang = np.where(inside, np.degrees(np.arctan2(zs - CURV * dists[None] ** 2 - z_eye, dists[None])), -90.0)
horizon = ang.max(1)
edge_m = np.where(inside.all(1), dists[-1], dists[np.argmin(inside, axis=1)])
near_ridge = np.where(inside, ang, -90.0)[:, dists <= 1500].max(1)
seen = []
for p in meta["peaks"]:
    pr_, pc_ = lonlat_rc(p["lon"], p["lat"])
    dE, dN = (pc_ - ec) * DX, (er - pr_) * DY
    D = math.hypot(dE, dN)
    b_ = (math.degrees(math.atan2(dE, dN)) + 360) % 360
    e_ = math.degrees(math.atan2(p["elev_m"] - CURV * D * D - z_eye, D))
    k = int(round(b_ / AZ_STEP)) % len(azs)
    block = ang[k, dists < D * 0.97].max() if (dists < D * 0.97).any() else -90
    seen.append(dict(name=p["name"], verified=p["verified"], bearing_deg=round(b_, 2), elev_deg=round(e_, 2), dist_m=round(D),
                     visible=bool(e_ >= block - 0.05), lon=p["lon"], lat=p["lat"], elev_m=p["elev_m"]))

# ------------------------------------------------------------ line of position from Moran (the measured bearing, reversed)
back = math.radians(LOC["brg"] + 180)
lop = []
for s in np.arange(0, 9000, 30.0):
    x_, y_ = XM + math.sin(back) * s, YM + math.cos(back) * s
    if not (0 <= x_ <= W * DX and 0 <= y_ <= H * DY):
        break
    lop.append([west + x_ / (W * DX) * (east - west), south + y_ / (H * DY) * (north - south)])
lopz = z5_at([p[0] for p in lop], [p[1] for p in lop])
lop = [[round(a, 6), round(b, 6), round(float(z), 1)] for (a, b), z in zip(lop, lopz)]

elon, elat = rc_lonlat(er, ec)
tlon, tlat = rc_lonlat(tr, tc)
lzlon, lzlat = rc_lonlat(lzr, lzc)
sim = dict(
    schema="team8-sim/v1",
    note="Synthetic demo. The hidden point is made up. Same DEM for truth and model, so real-world error is not included.",
    scenario=f"Downed pilot near {LM_NAME}, GPS jammed, mild head injury. Pre-loaded terrain, barometer, and pointing at {LM_NAME}.",
    grid=dict(width=W, height=H, cell_m=[round(DX, 3), round(DY, 3)], bounds=meta["bounds"]),
    landmark=dict(name=LM_NAME, lon=moran["lon"], lat=moran["lat"], elev_m=moran["elev_m"]),
    truth=dict(lon=round(tlon, 6), lat=round(tlat, 6), elev_m=round(float(Z[tr, tc]), 1),
               dist_to_moran_m=round(float(DIST[tr, tc]))),
    measurements=dict(baro_alt_m=round(LOC["alt"], 1), baro_sigma_m=SIG_B, bearing_deg=round(LOC["brg"], 2),
                      bearing_sigma_deg=SIG_A, elev_angle_deg=round(LOC["ela"], 2), elev_sigma_deg=SIG_E, noise_added=NOISE),
    prior_km2=round(float(LAND.sum() * DX * DY / 1e6), 2),
    stages=stages,
    estimate=dict(lon=round(elon, 6), lat=round(elat, 6), elev_m=round(float(Z[er, ec]), 1), error_m=round(LOC["err"]),
                  method="argmax of the smoothed final posterior"),
    rendezvous=dict(lon=round(lzlon, 6), lat=round(lzlat, 6), elev_m=round(float(Z[lzr, lzc]), 1), label=LZ_LABEL),
    routes=routes,
    legs=legs,
    line_of_position=lop,
    skyline=dict(from_lonlat=[round(elon, 6), round(elat, 6)], eye_m=round(z_eye, 1), az_step_deg=AZ_STEP,
                 horizon_deg=[round(float(v), 2) for v in horizon], near_ridge_deg=[round(float(v), 2) for v in near_ridge],
                 data_edge_m=[int(v) for v in edge_m]),
    peaks_from_estimate=seen,
    mc=MC,
    declination=meta["declination"],
    model=dict(grid=f"{DX:.0f} m", barometer="Gaussian, sigma 30 m", bearing="Gaussian on angle, sigma 5 deg, x0.02 where Moran is hidden",
               elevation_angle="Gaussian, sigma 1 deg, curvature and refraction k=0.13", route="A*, Tobler off-trail (x0.6), "
               "slope limit on a 30 m smoothed DEM, cliffs (any 5 m patch over 45 deg) and water blocked"),
)
with open(os.path.join(DATA, "sim.json"), "w", encoding="utf-8") as f:
    json.dump(sim, f, ensure_ascii=False, separators=(",", ":"))
log("estimate err m", sim["estimate"]["error_m"], "visible peaks", [s["name"] for s in seen if s["visible"]])
log("sim.json", round(os.path.getsize(os.path.join(DATA, "sim.json")) / 1e3), "kB")
