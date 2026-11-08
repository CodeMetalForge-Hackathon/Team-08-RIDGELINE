"""Team-8 terrain prep for the deck.gl viewer.

Reads the USGS 3DEP 1 m mosaic (EPSG:26912) and writes, on one EPSG:4326 grid of about 5 m:
  terrain_rgb_5m.png    Mapbox terrain-RGB  h = -10000 + (R*65536 + G*256 + B) * 0.1
  terrain_rgb_10m.png   same at about 10 m (lighter mesh, for the repo and slow laptops)
  terrain_color_5m.png  hillshade x slope safety (green <15, yellow <25, orange <35, red >=35 deg)
  terrain_color_5m.jpg  the same picture as JPEG (small, used by the offline bundle)
  lake_mask_5m.png      255 = hydro-flattened water
  terrain_meta.json     bounds [west, south, east, north], decoder, peaks, shares, credits

Usage: python -I team8_terrain_prep.py <output_USGS1m.tif> <out_data_dir>
"""
import sys, os, json, math, time
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject, transform as rtransform
from rasterio.transform import from_bounds
from scipy import ndimage
from PIL import Image

DEM, OUT = sys.argv[1], sys.argv[2]
os.makedirs(OUT, exist_ok=True)
T0 = time.time()
CELL = 5.0


def log(*a):
    print(f"[{time.time() - T0:5.1f}s]", *a, flush=True)


def m_per_deg(lat):
    p = math.radians(lat)
    mlat = 111132.954 - 559.822 * math.cos(2 * p) + 1.175 * math.cos(4 * p)
    mlon = 111412.84 * math.cos(p) - 93.5 * math.cos(3 * p) + 0.118 * math.cos(5 * p)
    return mlat, mlon


# ------------------------------------------------------------ reproject to a lon/lat grid
with rasterio.open(DEM) as ds:
    src = ds.read(1).astype(np.float32)
    nd, st, scrs, b = ds.nodata, ds.transform, ds.crs, ds.bounds
bad = ~np.isfinite(src) | (src < -100) | (src > 9000)
if nd is not None:
    bad |= src == nd
src[bad] = np.nan
log("read", src.shape, "nodata share", round(float(bad.mean()), 4))

xs = [b.left, b.right, b.left, b.right]
ys = [b.top, b.top, b.bottom, b.bottom]
lo, la = rtransform(scrs, "EPSG:4326", xs, ys)
lat_c = (max(la[2], la[3]) + min(la[0], la[1])) / 2
MLAT, MLON = m_per_deg(lat_c)
pad_lon, pad_lat = 15.0 / MLON, 15.0 / MLAT          # stay 15 m inside the UTM footprint
west, east = max(lo[0], lo[2]) + pad_lon, min(lo[1], lo[3]) - pad_lon
south, north = max(la[2], la[3]) + pad_lat, min(la[0], la[1]) - pad_lat
W = int(round((east - west) * MLON / CELL / 6.0)) * 6   # multiples of 6 so 10 m and 30 m grids divide evenly
H = int(round((north - south) * MLAT / CELL / 6.0)) * 6
DLON, DLAT = (east - west) / W, (north - south) / H
DX, DY = DLON * MLON, DLAT * MLAT
dst_tf = from_bounds(west, south, east, north, W, H)
Z = np.full((H, W), np.nan, np.float32)
reproject(src, Z, src_transform=st, src_crs=scrs, dst_transform=dst_tf, dst_crs="EPSG:4326",
          resampling=Resampling.average, src_nodata=np.nan, dst_nodata=np.nan)
del src
holes = np.isnan(Z)
if holes.any():
    idx = ndimage.distance_transform_edt(holes, return_distances=False, return_indices=True)
    Z = Z[tuple(idx)]
log("grid", (H, W), "cell m", round(DX, 3), round(DY, 3), "holes filled", int(holes.sum()))

# ------------------------------------------------------------ derived layers
gy, gx = np.gradient(Z.astype(np.float64), DY, DX)       # rows go south
SLOPE = np.degrees(np.arctan(np.hypot(gx, gy))).astype(np.float32)


def hillshade(alt=42.0):
    nx, ny = -gx, gy
    nrm = np.sqrt(nx * nx + ny * ny + 1.0)
    out = np.zeros(Z.shape, np.float64)
    a = math.radians(alt)
    for az, w in ((315, 0.45), (270, 0.2), (360, 0.2), (225, 0.15)):
        t = math.radians(az)
        lx, ly, lz = math.sin(t) * math.cos(a), math.cos(t) * math.cos(a), math.sin(a)
        out += w * np.clip((nx * lx + ny * ly + lz) / nrm, 0, 1)
    return out


HS = hillshade()
rng5 = ndimage.maximum_filter(Z, 5) - ndimage.minimum_filter(Z, 5)
flat = rng5 < 0.15
lab, nlab = ndimage.label(flat)
sizes = ndimage.sum(flat, lab, np.arange(1, nlab + 1))
big = np.zeros(nlab + 1, bool)
big[1:] = sizes > 300
LAKE = ndimage.binary_closing(big[lab], iterations=2)
log("lake share", round(float(LAKE.mean()), 3))

SAFE = np.array([(46, 157, 79), (227, 196, 64), (239, 138, 42), (215, 38, 61)], np.float64)
cls = np.digitize(SLOPE, [15, 25, 35])
tinted = SAFE[cls] * (0.22 + 0.95 * HS)[..., None]
gray = (255 * 0.92 * HS)[..., None]
rgb = 0.74 * tinted + 0.26 * gray


def contour_edges(step):
    c = np.floor(Z / step)
    e = np.zeros(Z.shape, bool)
    e[:, 1:] |= c[:, 1:] != c[:, :-1]
    e[1:, :] |= c[1:, :] != c[:-1, :]
    return e


for m, a in ((contour_edges(100) & ~LAKE, 0.16), (ndimage.binary_dilation(contour_edges(500)) & ~LAKE, 0.26)):
    rgb[m] = rgb[m] * (1 - a) + 255 * a
rgb[LAKE] = (22, 44, 70)
shore = LAKE & ~ndimage.binary_erosion(LAKE, iterations=2)
rgb[shore] = (90, 130, 160)
COLOR = np.clip(rgb, 0, 255).astype(np.uint8)


def terrain_rgb(z):
    v = np.round((z.astype(np.float64) + 10000.0) * 10.0).astype(np.int64)
    return np.stack([(v >> 16) & 255, (v >> 8) & 255, v & 255], -1).astype(np.uint8)


Z10 = Z.reshape(H // 2, 2, W // 2, 2).mean(axis=(1, 3))
files = {
    "terrain_rgb_5m": "terrain_rgb_5m.png", "terrain_rgb_10m": "terrain_rgb_10m.png",
    "color_5m_png": "terrain_color_5m.png", "color_5m_jpg": "terrain_color_5m.jpg", "lake_mask_5m": "lake_mask_5m.png",
}
Image.fromarray(terrain_rgb(Z)).save(os.path.join(OUT, files["terrain_rgb_5m"]), optimize=True)
Image.fromarray(terrain_rgb(Z10)).save(os.path.join(OUT, files["terrain_rgb_10m"]), optimize=True)
Image.fromarray(COLOR).save(os.path.join(OUT, files["color_5m_png"]), optimize=True)
Image.fromarray(COLOR).save(os.path.join(OUT, files["color_5m_jpg"]), quality=88, optimize=True)
Image.fromarray((LAKE * 255).astype(np.uint8)).save(os.path.join(OUT, files["lake_mask_5m"]), optimize=True)
log("images written")

# ------------------------------------------------------------ peaks (DEM summits, named only when they match a source)
PEAK_REFS = [  # name, lat, lon, elevation m, source (Wikipedia coordinate boxes, read 2026-11-07)
    ("Mount Moran", 43.83528, -110.77639, 3840, "https://en.wikipedia.org/wiki/Mount_Moran"),
    ("Thor Peak", 43.83083, -110.79500, 3668, "https://en.wikipedia.org/wiki/Thor_Peak_(Wyoming)"),
    ("Traverse Peak", 43.86806, -110.79472, 3370, "https://en.wikipedia.org/wiki/Traverse_Peak"),
    ("Raynolds Peak", 43.87083, -110.82556, 3327, "https://en.wikipedia.org/wiki/Raynolds_Peak"),
    ("Bivouac Peak", 43.86889, -110.78417, 3300, "https://en.wikipedia.org/wiki/Bivouac_Peak"),
]


def rc_to_lonlat(r, c):
    return west + (c + 0.5) * DLON, north - (r + 0.5) * DLAT


def lonlat_to_rc(lon, lat):
    return (north - lat) / DLAT - 0.5, (lon - west) / DLON - 0.5


H30, W30 = H // 6, W // 6
Z30 = Z.reshape(H30, 6, W30, 6).mean(axis=(1, 3))
cand = Z30 >= ndimage.maximum_filter(Z30, size=15)
cand[:8, :] = cand[-8:, :] = False
cand[:, :8] = cand[:, -8:] = False
peaks = []
for r, c in zip(*np.nonzero(cand)):
    z = Z30[r, c]
    hr, hc = np.nonzero(Z30 > z + 1.0)
    iso = float(np.min(np.hypot((hr - r) * DY * 6, (hc - c) * DX * 6))) if hr.size else 1e9
    win = Z30[max(0, r - 50):r + 51, max(0, c - 50):c + 51]
    if iso >= 700 and z - np.percentile(win, 10) >= 300:
        r0, c0 = max(0, (r - 1) * 6), max(0, (c - 1) * 6)
        blk = Z[r0:(r + 2) * 6, c0:(c + 2) * 6]
        br, bc = np.unravel_index(np.argmax(blk), blk.shape)
        peaks.append(dict(r=int(r0 + br), c=int(c0 + bc), elev_m=round(float(blk.max()), 1)))
peaks.sort(key=lambda p: -p["elev_m"])
peaks = peaks[:12]
for name, rlat, rlon, relev, url in PEAK_REFS:
    rr, cc = lonlat_to_rc(rlon, rlat)
    rad = int(200 / DX)
    r0, r1 = max(0, int(rr) - rad), min(H, int(rr) + rad + 1)
    c0, c1 = max(0, int(cc) - rad), min(W, int(cc) + rad + 1)
    blk = Z[r0:r1, c0:c1]
    br, bc = np.unravel_index(np.argmax(blk), blk.shape)
    pr, pc, pz = r0 + br, c0 + bc, float(blk.max())
    off = math.hypot((pr - rr) * DY, (pc - cc) * DX)
    ok = abs(pz - relev) <= 25 and off <= 200
    log("match", name, "dem", round(pz), "ref", relev, "offset m", round(off), "ok", ok)
    if not ok:
        continue
    hit = [p for p in peaks if math.hypot((p["r"] - pr) * DY, (p["c"] - pc) * DX) < 150]
    p = hit[0] if hit else dict(r=int(pr), c=int(pc), elev_m=round(pz, 1))
    if not hit:
        peaks.append(p)
    p.update(name=name, verified=True, source=url, ref_elev_m=relev, ref_offset_m=round(off))
for p in peaks:
    p["lon"], p["lat"] = [round(v, 6) for v in rc_to_lonlat(p["r"], p["c"])]
    p.setdefault("name", f"Pk {p['elev_m']:.0f}")
    p.setdefault("verified", False)
peaks.sort(key=lambda p: -p["elev_m"])

land = ~LAKE
meta = dict(
    schema="team8-terrain/v1",
    bounds=[round(west, 7), round(south, 7), round(east, 7), round(north, 7)],
    grid=dict(width=W, height=H, cell_m=[round(DX, 3), round(DY, 3)], crs="EPSG:4326",
              note="pixel edges match bounds; row 0 is north"),
    elevation_decoder=dict(rScaler=6553.6, gScaler=25.6, bScaler=0.1, offset=-10000),
    elev_range_m=[round(float(Z.min()), 1), round(float(Z.max()), 1)],
    slope_share_land=dict(lt15=round(float((SLOPE[land] < 15).mean()), 3),
                          s15_25=round(float(((SLOPE[land] >= 15) & (SLOPE[land] < 25)).mean()), 3),
                          s25_35=round(float(((SLOPE[land] >= 25) & (SLOPE[land] < 35)).mean()), 3),
                          ge35=round(float((SLOPE[land] >= 35).mean()), 3)),
    lake_share=round(float(LAKE.mean()), 3),
    area_km2=round(W * DX * H * DY / 1e6, 2),
    files=files,
    peaks=[{k: p[k] for k in ("name", "lon", "lat", "elev_m", "verified") if k in p} |
           ({"source": p["source"]} if "source" in p else {}) for p in peaks],
    credits="USGS 3DEP 1 m lidar DEM via OpenTopography (WY_GrandTetonElkRefuge_2014, WY_GrandTetonNP_D22, "
            "WY_North_Converse_2020_D20), averaged to about 5 m on an EPSG:4326 grid. Public domain (USGS).",
)
with open(os.path.join(OUT, "terrain_meta.json"), "w", encoding="utf-8") as f:
    json.dump(meta, f, ensure_ascii=False, indent=1)
for k, v in files.items():
    log(k, round(os.path.getsize(os.path.join(OUT, v)) / 1e6, 2), "MB")
log("peaks", [(p["name"], p["elev_m"]) for p in peaks])
log("bounds", meta["bounds"], "slope share", meta["slope_share_land"])
