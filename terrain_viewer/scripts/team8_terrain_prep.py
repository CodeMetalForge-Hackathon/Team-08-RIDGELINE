"""Team-8 terrain prep: turn one fetched area into the viewer's map files. Any place, any DEM resolution.

  python -I scripts/team8_terrain_prep.py .cache/<id> data/<id> [--cell 5]

Reads area.json, the DEM, landcover.tif and places.json written by team8_fetch_area.py, and writes on one EPSG:4326 grid
(cell 5 m for lidar, 10 m for 10 m and 30 m DEMs unless --cell says otherwise):
  terrain_rgb.png       Mapbox terrain-RGB at the texture cell, h = -10000 + (R*65536 + G*256 + B) * 0.1
  terrain_rgb_mesh.png  the same at twice the cell, for the 3D mesh
  topo.jpg              the map picture in the team's relief style (team8_relief.py, the look of Noah's route plots):
                        elevation tint, relief shading, water, contours, 1 km UTM grid
  slope.png             slope x2 (0.5 deg steps) on the mesh grid, smoothed over about 30 m. The route limit and the
                        too-steep hatching both read this file, so they always agree
  water_mask.png        255 = water, texture grid
  terrain_meta.json     bounds, grid, decoder, peaks, labels, UTM grid labels, declination, credits
"""
import argparse, json, math, os, sys, time
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject
from rasterio.transform import from_bounds
from scipy import ndimage
from PIL import Image
import contourpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # team8_relief.py sits next to this file; python -I leaves it off the path
import team8_relief as RELIEF

ap = argparse.ArgumentParser()
ap.add_argument("area_dir")
ap.add_argument("out_dir")
ap.add_argument("--cell", type=float, default=0.0)
A = ap.parse_args()
AREA = json.load(open(os.path.join(A.area_dir, "area.json"), encoding="utf-8"))
OUT = A.out_dir
os.makedirs(OUT, exist_ok=True)
T0 = time.time()


def log(*a):
    print(f"[{time.time() - T0:5.1f}s]", *a, flush=True)


def m_per_deg(lat):
    p = math.radians(lat)
    return (111132.954 - 559.822 * math.cos(2 * p) + 1.175 * math.cos(4 * p),
            111412.84 * math.cos(p) - 93.5 * math.cos(3 * p) + 0.118 * math.cos(5 * p))


native = float(AREA["dem"].get("native_res_m", 30))
CELL = A.cell or (5.0 if native <= 3 else 10.0)
west, south, east, north = AREA["bbox"]
MLAT, MLON = m_per_deg((south + north) / 2)
W = int(round((east - west) * MLON / CELL / 6.0)) * 6        # multiples of 6: the mesh (x2) and summit (x6) grids divide evenly
H = int(round((north - south) * MLAT / CELL / 6.0)) * 6
DLON, DLAT = (east - west) / W, (north - south) / H
DX, DY = DLON * MLON, DLAT * MLAT
TF = from_bounds(west, south, east, north, W, H)

# ------------------------------------------------------------ elevation and land cover on the grid
with rasterio.open(AREA["dem"]["path"]) as ds:
    src = ds.read(1).astype(np.float32)
    nd = ds.nodata
    bad = ~np.isfinite(src) | (src < -500) | (src > 9000)
    if nd is not None:
        bad |= src == nd
    src[bad] = np.nan
    Z = np.full((H, W), np.nan, np.float32)
    rs = Resampling.average if native < CELL / 1.5 else Resampling.bilinear
    reproject(src, Z, src_transform=ds.transform, src_crs=ds.crs, dst_transform=TF, dst_crs="EPSG:4326",
              resampling=rs, src_nodata=np.nan, dst_nodata=np.nan)
del src
holes = np.isnan(Z)
if holes.any():
    idx = ndimage.distance_transform_edt(holes, return_distances=False, return_indices=True)
    Z = Z[tuple(idx)]
log("grid", (H, W), "cell m", round(DX, 2), round(DY, 2), "native m", native, "holes", int(holes.sum()))

LC = np.zeros((H, W), np.uint8)
with rasterio.open(AREA["landcover"]["path"]) as ds:
    reproject(ds.read(1), LC, src_transform=ds.transform, src_crs=ds.crs, dst_transform=TF, dst_crs="EPSG:4326",
              resampling=Resampling.nearest, src_nodata=0, dst_nodata=0)

gy, gx = np.gradient(Z.astype(np.float64), DY, DX)            # rows run south
G = np.hypot(gx, gy)
SLOPE = np.degrees(np.arctan(G)).astype(np.float32)

# water: WorldCover permanent water, plus hydro-flattened lakes in lidar
water = LC == 80
if native <= 3:
    flat = (ndimage.maximum_filter(Z, 5) - ndimage.minimum_filter(Z, 5)) < 0.15
    lab, nl = ndimage.label(flat)
    big = np.zeros(nl + 1, bool)
    big[1:] = ndimage.sum(flat, lab, np.arange(1, nl + 1)) > 300
    water |= big[lab]
water = ndimage.binary_opening(water, iterations=1)
water = ndimage.binary_closing(water, iterations=2)
lab, nl = ndimage.label(water)
keep = np.zeros(nl + 1, bool)
keep[1:] = ndimage.sum(water, lab, np.arange(1, nl + 1)) * DX * DY >= 2000      # drop puddles under 2000 m2
WATER = keep[lab]
log("water share", round(float(WATER.mean()), 3))

# ------------------------------------------------------------ the map picture, in the team's relief style (team8_relief.py)
relief = float(np.percentile(Z[~WATER], 99.9) - np.percentile(Z[~WATER], 0.1)) if (~WATER).any() else float(np.ptp(Z))
CI, IDX = (50, 250) if relief >= 1200 else ((20, 100) if relief >= 400 else (10, 50))
E, N, ZONE, UTM = RELIEF.utm_grid((west, south, east, north), W, H)   # the 1 km UTM grid an MGRS reading refers to
TOPO, TINT_M = RELIEF.render(Z, WATER, (DX, DY), CI, IDX, (west, south, east, north), utm=(E, N))


def edge_ticks(vals, along, axis_len, to_lonlat):
    out = []
    for k in range(int(math.ceil(vals.min() / 1000)), int(math.floor(vals.max() / 1000)) + 1):
        i = np.searchsorted(vals, k * 1000.0) if vals[0] < vals[-1] else None
        if i is None or i <= 0 or i >= axis_len:
            continue
        f = (k * 1000.0 - vals[i - 1]) / (vals[i] - vals[i - 1])
        out.append(dict(v=k * 1000, **dict(zip(("lon", "lat"), to_lonlat(i - 1 + f)))))
    return out


grid_labels = dict(
    zone=ZONE, epsg=UTM,
    south=edge_ticks(E[-1], None, W, lambda x: (west + (x + 0.5) * DLON, south)),
    north=edge_ticks(E[0], None, W, lambda x: (west + (x + 0.5) * DLON, north)),
    west=edge_ticks(N[::-1, 0], None, H, lambda y: (west, south + (y + 0.5) * DLAT)),
    east=edge_ticks(N[::-1, -1], None, H, lambda y: (east, south + (y + 0.5) * DLAT)),
)
for side in ("south", "north", "west", "east"):
    for t in grid_labels[side]:
        t["lon"], t["lat"] = round(t["lon"], 6), round(t["lat"], 6)
del E, N
log("contours", CI, IDX, "m, UTM zone", ZONE, "grid lines", len(grid_labels["south"]), len(grid_labels["west"]))

# ------------------------------------------------------------ files on the grid
def terrain_rgb(z):
    v = np.round((z.astype(np.float64) + 10000.0) * 10.0).astype(np.int64)
    return np.stack([(v >> 16) & 255, (v >> 8) & 255, v & 255], -1).astype(np.uint8)


Z2 = Z.reshape(H // 2, 2, W // 2, 2).mean(axis=(1, 3))
sig = 15.0 / (2 * CELL)                                        # about 30 m footprint for the slope limit
g2y, g2x = np.gradient(ndimage.gaussian_filter(Z2.astype(np.float64), sig), DY * 2, DX * 2)
S2 = np.degrees(np.arctan(np.hypot(g2x, g2y)))
files = dict(terrain_rgb="terrain_rgb.png", terrain_rgb_mesh="terrain_rgb_mesh.png", topo="topo.jpg",
             slope="slope.png", water_mask="water_mask.png")
Image.fromarray(terrain_rgb(Z)).save(os.path.join(OUT, files["terrain_rgb"]), optimize=True)
Image.fromarray(terrain_rgb(Z2)).save(os.path.join(OUT, files["terrain_rgb_mesh"]), optimize=True)
RELIEF.save(TOPO, os.path.join(OUT, files["topo"]))
Image.fromarray(np.clip(np.round(S2 * 2), 0, 255).astype(np.uint8)).save(os.path.join(OUT, files["slope"]), optimize=True)
Image.fromarray((WATER * 255).astype(np.uint8)).save(os.path.join(OUT, files["water_mask"]), optimize=True)
log("images written")

# ------------------------------------------------------------ peaks: DEM summits, named where Wikidata agrees
def rc_lonlat(r, c):
    return west + (c + 0.5) * DLON, north - (r + 0.5) * DLAT


def lonlat_rc(lon, lat):
    return (north - lat) / DLAT - 0.5, (lon - west) / DLON - 0.5


places = json.load(open(AREA["places"]["path"], encoding="utf-8")) if os.path.exists(AREA["places"]["path"]) else []
H6, W6 = H // 6, W // 6
Z6 = Z.reshape(H6, 6, W6, 6).mean(axis=(1, 3))
cand = Z6 >= ndimage.maximum_filter(Z6, size=15)
cand[:3, :] = cand[-3:, :] = False
cand[:, :3] = cand[:, -3:] = False
min_rel = max(150.0, 0.17 * relief)
peaks = []
for r, c in zip(*np.nonzero(cand)):
    z = Z6[r, c]
    hr, hc = np.nonzero(Z6 > z + 1.0)
    iso = float(np.min(np.hypot((hr - r) * DY * 6, (hc - c) * DX * 6))) if hr.size else 1e9
    win = Z6[max(0, r - 50):r + 51, max(0, c - 50):c + 51]
    if iso >= 700 and z - np.percentile(win, 10) >= min_rel:
        r0, c0 = max(0, (r - 1) * 6), max(0, (c - 1) * 6)
        blk = Z[r0:(r + 2) * 6, c0:(c + 2) * 6]
        br, bc = np.unravel_index(np.argmax(blk), blk.shape)
        peaks.append(dict(r=int(r0 + br), c=int(c0 + bc), elev_m=round(float(blk.max()), 1)))
peaks.sort(key=lambda p: -p["elev_m"])
peaks = peaks[:12]
edge_px = 150 / DX
for pl in [p for p in places if p["kind"] == "peak"]:
    rr_, cc_ = lonlat_rc(pl["lon"], pl["lat"])
    if not (edge_px <= rr_ < H - edge_px and edge_px <= cc_ < W - edge_px):
        continue
    rad = int(250 / DX)
    r0, r1, c0, c1 = max(0, int(rr_) - rad), min(H, int(rr_) + rad + 1), max(0, int(cc_) - rad), min(W, int(cc_) + rad + 1)
    blk = Z[r0:r1, c0:c1]
    br, bc = np.unravel_index(np.argmax(blk), blk.shape)
    if min(br, bc, blk.shape[0] - 1 - br, blk.shape[1] - 1 - bc) * DX < 40:   # the top is on the search edge: not a summit here
        continue
    pr, pc, pz = r0 + br, c0 + bc, float(blk.max())
    off = math.hypot((pr - rr_) * DY, (pc - cc_) * DX)
    hit = [p for p in peaks if math.hypot((p["r"] - pr) * DY, (p["c"] - pc) * DX) < 200]
    p = hit[0] if hit else dict(r=int(pr), c=int(pc), elev_m=round(pz, 1))
    better = "name" not in p or pl["name"] == AREA["name"] or (p.get("name") != AREA["name"] and off < p.get("ref_offset_m", 1e9))
    if better:
        p.update(name=pl["name"], verified=True, source=pl["source"], ref_offset_m=round(off))
    if not hit:
        peaks.append(p)
for p in peaks:
    p["lon"], p["lat"] = [round(v, 6) for v in rc_lonlat(p["r"], p["c"])]
    p.setdefault("name", f"Pk {p['elev_m']:.0f}")
    p.setdefault("verified", False)
peaks.sort(key=lambda p: -p["elev_m"])
named = [p for p in peaks if p["verified"]]
LANDMARK = next((p for p in peaks if p["name"] == AREA["name"]), named[0] if named else peaks[0])
log("peaks", [(p["name"], p["elev_m"]) for p in peaks], "landmark", LANDMARK["name"])

# water and glacier names
labels = []
wl, nwl = ndimage.label(WATER)
for pl in [p for p in places if p["kind"] in ("lake", "glacier")]:
    rr_, cc_ = lonlat_rc(pl["lon"], pl["lat"])
    if not (0 <= rr_ < H and 0 <= cc_ < W):
        continue
    if pl["kind"] == "lake":
        rad = int(300 / DX)
        r0, c0 = max(0, int(rr_) - rad), max(0, int(cc_) - rad)
        win = wl[r0:int(rr_) + rad + 1, c0:int(cc_) + rad + 1]
        if not win.any():
            continue
        ii = np.argwhere(win > 0)
        k = ii[np.argmin(np.hypot(ii[:, 0] + r0 - rr_, ii[:, 1] + c0 - cc_))]
        comp = wl == win[k[0], k[1]]
        cm = ndimage.center_of_mass(comp)
        area_m2 = float(comp.sum() * DX * DY)
        rr_, cc_ = (cm if comp[int(cm[0]), int(cm[1])] else (k[0] + r0, k[1] + c0))
        labels.append(dict(kind="lake", name=pl["name"], area_m2=round(area_m2), elev_m=round(float(Z[int(rr_), int(cc_)]), 1), **dict(zip(("lon", "lat"), [round(v, 6) for v in rc_lonlat(rr_, cc_)]))))
    else:
        labels.append(dict(kind="glacier", name=pl["name"], lon=pl["lon"], lat=pl["lat"], elev_m=round(float(Z[int(rr_), int(cc_)]), 1)))
labels.sort(key=lambda l: -l.get("area_m2", 0))

# contour labels along index contours, upright, away from peaks, edges and each other
cl = []
gen = contourpy.contour_generator(z=Z.astype(np.float64))
pk_rc = np.array([(p["r"], p["c"]) for p in peaks]) if peaks else np.zeros((0, 2))
for lev in range(int(math.ceil(float(Z.min()) / IDX)) * IDX, int(float(Z.max())) + 1, IDX):
    for ln in gen.lines(lev):
        xy = np.asarray(ln)                                     # columns x, rows y
        if len(xy) < 12:
            continue
        seg = np.hypot(np.diff(xy[:, 0]) * DX, np.diff(xy[:, 1]) * DY)
        cum = np.concatenate([[0], np.cumsum(seg)])
        if cum[-1] < 1200:
            continue
        for target in np.arange(600, cum[-1] - 300, 1800):
            i = int(np.searchsorted(cum, target))
            i0, i1 = max(0, i - 4), min(len(xy) - 1, i + 4)
            x, y = xy[i]
            r_, c_ = int(round(y)), int(round(x))
            if not (0 <= r_ < H and 0 <= c_ < W) or WATER[r_, c_] or not (8 <= SLOPE[r_, c_] <= 42):
                continue
            if min(r_ * DY, (H - r_) * DY, c_ * DX, (W - c_) * DX) < 450:
                continue
            if len(pk_rc) and np.min(np.hypot((pk_rc[:, 0] - r_) * DY, (pk_rc[:, 1] - c_) * DX)) < 600:
                continue
            lon_, lat_ = rc_lonlat(y, x)
            if any(math.hypot((lon_ - q["lon"]) * MLON, (lat_ - q["lat"]) * MLAT) < 1100 for q in cl):
                continue
            ang = math.degrees(math.atan2(-(xy[i1, 1] - xy[i0, 1]) * DY, (xy[i1, 0] - xy[i0, 0]) * DX))
            ang = (ang + 90) % 180 - 90                           # keep the text upright
            cl.append(dict(lon=round(lon_, 6), lat=round(lat_, 6), elev_m=lev, angle=round(ang, 1)))
cl = cl[:48]
log("labels", len(labels), "contour labels", len(cl))

land = ~WATER
lc_share = {int(k): round(float(v), 3) for k, v in zip(*np.unique(LC[land], return_counts=True)) if v / land.sum() >= 0.002}
total = sum(lc_share.values()) or 1
lc_share = {k: round(v / land.sum(), 3) for k, v in zip(*np.unique(LC[land], return_counts=True)) if v / land.sum() >= 0.002}
dem_label = {"file": f"USGS 3DEP {native:g} m lidar", "3dep10": "USGS 3DEP 1/3 arc-second (about 10 m)",
             "cop30": "Copernicus DEM GLO-30 (about 30 m)"}[AREA["dem"]["source"]]
meta = dict(
    schema="team8-terrain/v2", id=AREA["id"], name=AREA["name"], region=AREA["region"],
    bounds=[round(west, 7), round(south, 7), round(east, 7), round(north, 7)],
    grid=dict(width=W, height=H, cell_m=[round(DX, 3), round(DY, 3)], crs="EPSG:4326", note="pixel edges match bounds; row 0 is north"),
    mesh=dict(width=W // 2, height=H // 2),
    elevation_decoder=dict(rScaler=6553.6, gScaler=25.6, bScaler=0.1, offset=-10000),
    elev_range_m=[round(float(Z.min()), 1), round(float(Z.max()), 1)],
    contours=dict(interval_m=CI, index_m=IDX, labels=cl),
    utm_grid=grid_labels,
    declination=AREA["declination"],
    slope_share_land=dict(lt15=round(float((SLOPE[land] < 15).mean()), 3), s15_25=round(float(((SLOPE[land] >= 15) & (SLOPE[land] < 25)).mean()), 3),
                          s25_35=round(float(((SLOPE[land] >= 25) & (SLOPE[land] < 35)).mean()), 3), ge35=round(float((SLOPE[land] >= 35).mean()), 3)),
    landcover_share={str(k): v for k, v in lc_share.items()},
    water_share=round(float(WATER.mean()), 3),
    area_km2=round(W * DX * H * DY / 1e6, 2),
    files=files,
    dem=dict(source=AREA["dem"]["source"], native_res_m=native, label=dem_label),
    landmark=LANDMARK["name"],
    peaks=[{k: p[k] for k in ("name", "lon", "lat", "elev_m", "verified") if k in p} | ({"source": p["source"]} if "source" in p else {}) for p in peaks],
    labels=labels,
    credits=[AREA["dem"]["credit"], AREA["landcover"]["credit"], AREA["places"]["credit"],
             "Magnetic declination from the World Magnetic Model 2025 (NOAA NCEI and BGS), public domain"],
    map_style=RELIEF.style_note(TINT_M),
)
with open(os.path.join(OUT, "terrain_meta.json"), "w", encoding="utf-8") as f:
    json.dump(meta, f, ensure_ascii=False, indent=1)
for k, v in files.items():
    log(k, round(os.path.getsize(os.path.join(OUT, v)) / 1e6, 2), "MB")
log("bounds", meta["bounds"], "declination", AREA["declination"]["deg"])
