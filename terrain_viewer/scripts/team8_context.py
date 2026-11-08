"""Team-8 context ring: lower-detail terrain all around a tile, so the 3D view has no cliff where the tile ends.

  python -I scripts/team8_fetch_area.py --id moran_context --name "Mount Moran context" --region "Teton Range, Wyoming" \
      --center 43.85289 -110.78270 --size-km 48 40 --dem 3dep10
  python -I scripts/team8_context.py .cache/moran_context data/moran data/moran_context
  python -I scripts/team8_bundle.py data/moran

The ring is only for the eye. The solver, the route planner, the phone and the tests read data/<id>/ alone, so the
fix, the routes and every measured number stay those of the tile.

Writes into the out folder (no terrain_meta.json there, so the area picker does not list it as a tile):
  context_mesh.png   terrain-RGB on a coarse grid (about 40 m between nodes). Its outermost node lines inside the tile run
                     along the tile mesh's edge vertices and take their heights from the tile, so the two surfaces meet;
                     the nodes further in are lowered out of sight under the tile
  context_topo.jpg   the map picture of the ring in Noah's relief tint with the tile's own tint range and shading stretch,
                     so the colours run on across the edge, a little less saturated, with water, without contours or grid
  context_meta.json  bounds, grid sizes, decoder, the fade (where the ring melts into the paper, applied in the browser)
                     and credits
"""
import argparse, json, math, os, sys, time
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import reproject
from scipy import ndimage
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # team8_relief.py sits next to this file
import team8_relief as RELIEF  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("area_dir", help="the wide fetch, e.g. .cache/moran_context")
ap.add_argument("tile_dir", help="the prepared tile it surrounds, e.g. data/moran")
ap.add_argument("out_dir", help="e.g. data/moran_context")
ap.add_argument("--mesh-cell", type=float, default=40.0, help="metres between mesh nodes")
ap.add_argument("--tex-cell", type=float, default=24.0, help="metres per texture pixel")
ap.add_argument("--saturation", type=float, default=0.72, help="1 keeps the tile's colours, 0 is grey")
ap.add_argument("--drop", type=float, default=80.0, help="metres the hidden part under the tile is lowered")
A = ap.parse_args()
T0 = time.time()


def log(*a):
    print(f"[{time.time() - T0:5.1f}s]", *a, flush=True)


def m_per_deg(lat):
    p = math.radians(lat)
    return (111132.954 - 559.822 * math.cos(2 * p) + 1.175 * math.cos(4 * p),
            111412.84 * math.cos(p) - 93.5 * math.cos(3 * p) + 0.118 * math.cos(5 * p))


AREA = json.load(open(os.path.join(A.area_dir, "area.json"), encoding="utf-8"))
TM = json.load(open(os.path.join(A.tile_dir, "terrain_meta.json"), encoding="utf-8"))
B, DEC = TM["bounds"], TM["elevation_decoder"]


def decode(name):
    t = np.asarray(Image.open(os.path.join(A.tile_dir, name)).convert("RGB"), np.float64)
    return t[..., 0] * DEC["rScaler"] + t[..., 1] * DEC["gScaler"] + t[..., 2] * DEC["bScaler"] + DEC["offset"]


def read(path, tf, shape, resampling, dtype=np.float32, nodata=np.nan):
    """Resample a fetched GeoTIFF onto an EPSG:4326 grid (row 0 north) given by its transform."""
    out = np.full(shape, nodata, dtype)
    with rasterio.open(path) as ds:
        src = ds.read(1)
        if dtype == np.float32:
            src = src.astype(np.float32)
            bad = ~np.isfinite(src) | (src < -500) | (src > 9000)
            if ds.nodata is not None:
                bad |= src == ds.nodata
            src[bad] = np.nan
        reproject(src, out, src_transform=ds.transform, src_crs=ds.crs, dst_transform=tf, dst_crs="EPSG:4326",
                  resampling=resampling, src_nodata=nodata, dst_nodata=nodata)
    if dtype == np.float32 and np.isnan(out).any():
        idx = ndimage.distance_transform_edt(np.isnan(out), return_distances=False, return_indices=True)
        out = out[tuple(idx)]
    return out


# ------------------------------------------------------------ the mesh grid, locked to the tile's edge vertices
ZM = decode(TM["files"]["terrain_rgb_mesh"])                 # what the tile mesh draws (vertices on pixel centres)
mh, mw = ZM.shape
dlon_m, dlat_m = (B[2] - B[0]) / mw, (B[3] - B[1]) / mh
eW, eE, eN, eS = B[0] + dlon_m / 2, B[2] - dlon_m / 2, B[3] - dlat_m / 2, B[1] + dlat_m / 2
MLAT, MLON = m_per_deg((B[1] + B[3]) / 2)
nx, ny = max(1, round((eE - eW) * MLON / A.mesh_cell)), max(1, round((eN - eS) * MLAT / A.mesh_cell))
cx, cy = (eE - eW) / nx, (eN - eS) / ny                       # degrees between nodes; the tile edges fall on nodes
w, s, e, n = AREA["bbox"]
iw, ie = math.floor((eW - w) / cx), math.floor((e - eE) / cx)
jn, js = math.floor((n - eN) / cy), math.floor((eS - s) / cy)
W, H = iw + nx + ie + 1, jn + ny + js + 1
lon0, lat0 = eW - iw * cx, eN + jn * cy                        # node (0, 0), the north-west corner
X0, Y1 = lon0 - cx / 2, lat0 + cy / 2                          # the loader puts node i at x0 + (x1 - x0)(i + 0.5)/W
X1, Y0 = X0 + W * cx, Y1 - H * cy
BOUNDS = [round(v, 9) for v in (X0, Y0, X1, Y1)]
log("mesh", (H, W), "node m", round(cx * MLON, 1), round(cy * MLAT, 1), "bounds", BOUNDS)

Z = read(AREA["dem"]["path"], from_origin(X0, Y1, cx, cy), (H, W), Resampling.average)
lons, lats = lon0 + np.arange(iw, iw + nx + 1) * cx, lat0 - np.arange(jn, jn + ny + 1) * cy
RR, CC = np.meshgrid((B[3] - lats) / dlat_m - 0.5, (lons - B[0]) / dlon_m - 0.5, indexing="ij")
Zt = ndimage.map_coordinates(ZM, [RR, CC], order=1, mode="nearest")
inner = Zt - A.drop                                            # hidden under the tile, out of the way of its surface
inner[0, :], inner[-1, :], inner[:, 0], inner[:, -1] = Zt[0, :], Zt[-1, :], Zt[:, 0], Zt[:, -1]   # the seam meets
seam = np.concatenate([Zt[0, :], Zt[-1, :], Zt[:, 0], Zt[:, -1]])
ring = np.concatenate([Z[jn - 1, iw:iw + nx + 1], Z[jn + ny + 1, iw:iw + nx + 1], Z[jn:jn + ny + 1, iw - 1], Z[jn:jn + ny + 1, iw + nx + 1]])
log("seam: tile edge vs the 10 m DEM one node outside, median abs diff m", round(float(np.median(np.abs(seam - ring))), 1))
Z[jn:jn + ny + 1, iw:iw + nx + 1] = inner
v = np.clip(np.round((Z.astype(np.float64) - DEC["offset"]) / DEC["bScaler"]), 0, 2 ** 24 - 1).astype(np.uint32)
rgb = np.stack([(v >> 16) & 255, (v >> 8) & 255, v & 255], -1).astype(np.uint8)
os.makedirs(A.out_dir, exist_ok=True)
Image.fromarray(rgb).save(os.path.join(A.out_dir, "context_mesh.png"), optimize=True)
del Z, rgb, v

# ------------------------------------------------------------ the map picture, painted like the tile
style = TM["map_style"]
vmin, vmax = style["tint_m"]
Zt5 = decode(TM["files"]["terrain_rgb"])                       # the tile's own shading stretch, as team8_relief.render made it
th, tw = Zt5.shape
tdx, tdy = TM["grid"]["cell_m"]
k = max(1, int(round(RELIEF.SHADE_CELL_M / max(tdx, tdy))))
while th % k or tw % k:
    k -= 1
Zs = Zt5.reshape(th // k, k, tw // k, k).mean(axis=(1, 3)) if k > 1 else Zt5


def shade_raw(z, dx, dy):
    """team8_relief.hillshade before its min-max stretch."""
    e_dy, e_dx = np.gradient(RELIEF.VERT_EXAG * z, -dy, dx)
    nrm = np.stack([-e_dx, -e_dy, np.ones_like(z)], -1)
    nrm /= np.linalg.norm(nrm, axis=-1, keepdims=True)
    az, alt = math.radians(90.0 - RELIEF.AZIMUTH), math.radians(RELIEF.ALTITUDE)
    return nrm @ np.array([math.cos(az) * math.cos(alt), math.sin(az) * math.cos(alt), math.sin(alt)], z.dtype)


ref = shade_raw(Zs, tdx * k, tdy * k)
lo, hi = float(ref.min()), float(ref.max())
del Zt5, Zs, ref

TX, TY = round((X1 - X0) * MLON / A.tex_cell), round((Y1 - Y0) * MLAT / A.tex_cell)
F = 2                                                          # shade and water at half the texture cell, then shrink
FW, FH = TX * F, TY * F
ftf = from_origin(X0, Y1, (X1 - X0) / FW, (Y1 - Y0) / FH)
fdx, fdy = (X1 - X0) / FW * MLON, (Y1 - Y0) / FH * MLAT
Zf = read(AREA["dem"]["path"], ftf, (FH, FW), Resampling.average)
LC = read(AREA["landcover"]["path"], ftf, (FH, FW), Resampling.mode, np.uint8, 0)
log("texture", (TY, TX), "painted at", (FH, FW), "cell m", round(fdx, 1), round(fdy, 1))

water = ndimage.binary_closing(ndimage.binary_opening(LC == 80, iterations=1), iterations=2)
lab, nl = ndimage.label(water)
keep = np.zeros(nl + 1, bool)
keep[1:] = ndimage.sum(water, lab, np.arange(1, nl + 1)) * fdx * fdy >= 2000
water = keep[lab]
del LC, lab

shade = np.clip((shade_raw(Zf.astype(np.float32), fdx, fdy) - lo) / max(hi - lo, 1e-6), 0, 1)[..., None]
base = RELIEF.tint(Zf, vmin, vmax).astype(np.float32)
img = (2 * shade * base + (1 - 2 * shade) * base ** 2) * 255.0
del shade, base, Zf
wa = (ndimage.gaussian_filter(water.astype(np.float32), 0.8) * RELIEF.WATER_ALPHA)[..., None]
img = img * (1 - wa) + RELIEF.rgb01(RELIEF.WATER).astype(np.float32) * 255.0 * wa
del wa
grey = (img @ np.array([0.299, 0.587, 0.114], np.float32))[..., None]
img = grey + (img - grey) * A.saturation                       # a little less colour than the tile, so the tile leads
img = img.reshape(TY, F, TX, F, 3).mean(axis=(1, 3))
Image.fromarray(np.clip(img + 0.5, 0, 255).astype(np.uint8)).save(os.path.join(A.out_dir, "context_topo.jpg"), quality=86, optimize=True)
del img, grey

hx, hy = (X1 - X0) / 2 * MLON, (Y1 - Y0) / 2 * MLAT
meta = dict(schema="team8-context/v1", id=os.path.basename(os.path.normpath(A.out_dir)), tile=TM["id"],
            name=f"Around {TM['name']}", region=AREA.get("region", ""), bounds=BOUNDS,
            mesh=dict(width=W, height=H, node_m=[round(cx * MLON, 2), round(cy * MLAT, 2)], drop_m=A.drop,
                      note="nodes on pixel centres; the tile's edge vertices lie on nodes; inside the tile lowered by drop_m"),
            texture=dict(width=TX, height=TY, cell_m=[round((X1 - X0) / TX * MLON, 2), round((Y1 - Y0) / TY * MLAT, 2)]),
            size_km=[round(2 * hx / 1000, 1), round(2 * hy / 1000, 1)],
            elevation_decoder=DEC,
            fade=dict(center=[round((B[0] + B[2]) / 2, 7), round((B[1] + B[3]) / 2, 7)], half_m=[round(hx), round(hy)],
                      power=4, start=0.5, end=0.94,
                      note="alpha = 1 - smoothstep(start, end, r), r = (|dx/hx|^p + |dy/hy|^p)^(1/p) from the tile centre"),
            map_style=dict(style, saturation=A.saturation, contours=None, grid=None,
                           shade_stretch=[round(lo, 5), round(hi, 5)], note="tint and shading stretch of the tile"),
            dem=dict(source=AREA["dem"]["source"], label="USGS 3DEP 1/3 arc-second (about 10 m)", credit=AREA["dem"]["credit"]),
            credits=[AREA["dem"]["credit"], AREA["landcover"]["credit"]],
            files=dict(mesh="context_mesh.png", topo="context_topo.jpg"))
with open(os.path.join(A.out_dir, "context_meta.json"), "w", encoding="utf-8", newline="\n") as f:
    json.dump(meta, f, ensure_ascii=False, indent=1)
sz = {k_: round(os.path.getsize(os.path.join(A.out_dir, v_)) / 1e6, 2) for k_, v_ in meta["files"].items()}
log("wrote", A.out_dir, sz, "MB, ring", meta["size_km"], "km")
