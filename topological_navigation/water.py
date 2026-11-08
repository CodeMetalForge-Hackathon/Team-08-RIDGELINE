"""
water.py - known water from the USGS 3D Hydrography Program (3DHP) for YOUR grid.

    from water import load_water
    load_water(grid, crs.to_epsg(), cache_dir=HERE)   # internet needed the first time only

What it does
  1. Queries the 3DHP_all ArcGIS REST service for the grid's bounding box:
       layer 60 Waterbody (polygons): River(1) Canal(2) Lake(3) Ocean/Great Lake(4)
       layer 50 Flowline  (lines):    River(1) Canal(2) Drainageway(3)
     Connector flowlines (types 4-7) are virtual lines through lakes etc., so they are skipped.
  2. Rasterizes onto the routing grid as classes:
       1 OPEN      lakes, wide rivers, canals (polygons)  -> BLOCKED for routing
       2 STREAM    river/canal centerlines                -> slower to cross (cost x3)
       3 DRAINAGE  drainageways, may be dry               -> small penalty (cost x1.5)
  3. Caches the result next to your data so the app works offline afterwards.
  4. apply_water() sets grid.water, grid.blocked (open water) and grid.cost_mult.

Wetlands/marsh are NOT in the 3DHP waterbody layer (no swamp class there). For wetlands
use e.g. the National Wetlands Inventory later.

Coverage note: where USGS has not yet remapped an area from lidar, the service serves
legacy NHD data instead. load_water prints how much of what it found is legacy NHD.
"""
import json
import os
import time
import urllib.parse
import urllib.request

import numpy as np

WATER_VERSION = "v3-raw-cache+seam-fix"
BASE = "https://hydro.nationalmap.gov/arcgis/rest/services/3DHP_all/MapServer"
WATERBODY, FLOWLINE = 60, 50
PAGE = 2000                                   # service allows 2500 per request

NONE, OPEN, STREAM, DRAINAGE = 0, 1, 2, 3
CLASS_NAMES = {OPEN: "open water", STREAM: "stream/river line", DRAINAGE: "drainageway"}


# --------------------------------------------------------------------------- fetch
def _get_json(layer, params, retries=3, timeout=90):
    url = f"{BASE}/{layer}/query?" + urllib.parse.urlencode(params)
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "lost-hiker-app/0.1"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            if "error" in data:
                raise RuntimeError(data["error"])
            return data
        except Exception as e:                 # network hiccup, service busy, ...
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"3DHP query failed after {retries} tries: {last}")


def _query_all(layer, where, bounds, epsg, out_fields="OBJECTID,featuretype,featuretypelabel,workunitid"):
    xmin, ymin, xmax, ymax = bounds
    env = json.dumps({"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax,
                      "spatialReference": {"wkid": epsg}})
    feats, offset = [], 0
    while True:
        data = _get_json(layer, dict(
            f="json", where=where, geometry=env, geometryType="esriGeometryEnvelope",
            inSR=epsg, outSR=epsg, spatialRel="esriSpatialRelIntersects",
            outFields=out_fields, returnGeometry="true",
            orderByFields="OBJECTID", resultOffset=offset, resultRecordCount=PAGE))
        got = data.get("features", [])
        feats += got
        if not data.get("exceededTransferLimit") or not got:
            break
        offset += len(got)
    return feats


# --------------------------------------------------------------------------- rasterize
def _px(grid, pts):
    """Map coords (x, y) -> image coords where cell centers sit on integers."""
    x0, y0 = grid.origin_xy
    return [((p[0] - x0) / grid.cell - 0.5, (y0 - p[1]) / grid.cell - 0.5) for p in pts]


def _fill_polygons(grid, features):
    """
    Rasterize Esri polygons (even-odd, so islands become holes).
    Every ring OUTLINE is also painted as water. Without that, two rings of one feature that meet
    along a seam cancel each other's edge pixels and leave a 1-cell stripe of "land" straight
    across a lake. Painting outlines also keeps island shorelines blocked (conservative).
    """
    from PIL import Image, ImageDraw
    R, C = grid.dem.shape
    out = np.zeros((R, C), bool)
    for f in features:
        rings = (f.get("geometry") or {}).get("rings") or []
        if not rings:
            continue
        pr = [_px(grid, r) for r in rings]
        xs = [p[0] for r in pr for p in r]
        ys = [p[1] for r in pr for p in r]
        c0, c1 = max(int(np.floor(min(xs))) - 1, 0), min(int(np.ceil(max(xs))) + 2, C)
        r0, r1 = max(int(np.floor(min(ys))) - 1, 0), min(int(np.ceil(max(ys))) + 2, R)
        if c1 <= c0 or r1 <= r0:
            continue                           # entirely outside the grid
        h, w = r1 - r0, c1 - c0
        acc = np.zeros((h, w), bool)
        outline = Image.new("1", (w, h), 0)
        od = ImageDraw.Draw(outline)
        for ring in pr:
            if len(ring) < 3:
                continue
            pts = [(x - c0, y - r0) for x, y in ring]
            img = Image.new("1", (w, h), 0)
            ImageDraw.Draw(img).polygon(pts, fill=1)
            acc ^= np.asarray(img, bool)       # even-odd: holes (islands) drop out
            od.line(pts + [pts[0]], fill=1, width=1)
        out[r0:r1, c0:c1] |= acc | np.asarray(outline, bool)
    return out


def _draw_lines(grid, features, width_px=2):
    # 2 px wide: a 1 px diagonal line has gaps that 8-connected routes can slip through unnoticed
    from PIL import Image, ImageDraw
    R, C = grid.dem.shape
    img = Image.new("1", (C, R), 0)
    d = ImageDraw.Draw(img)
    for f in features:
        for path in (f.get("geometry") or {}).get("paths") or []:
            pts = _px(grid, path)
            if len(pts) >= 2:
                d.line(pts, fill=1, width=width_px)
    return np.asarray(img, bool)


def _seal(mask):
    """Close 1-cell gaps/slivers in open water (seams between polygons, rounding) so routes cannot
    slip through them. Padded so the map edge is not eroded."""
    from scipy.ndimage import binary_closing
    p = np.pad(mask, 2, mode="edge")
    return binary_closing(p, structure=np.ones((3, 3), bool))[2:-2, 2:-2] | mask


def rasterize(grid, waterbodies, flowlines):
    """Esri-JSON features -> uint8 class raster (open water wins over streams over drainage)."""
    R, C = grid.dem.shape
    is_stream = lambda f: f["attributes"].get("featuretype") in (1, 2)
    is_drain = lambda f: f["attributes"].get("featuretype") == 3
    open_ = _seal(_fill_polygons(grid, waterbodies))
    stream = _draw_lines(grid, [f for f in flowlines if is_stream(f)])
    drain = _draw_lines(grid, [f for f in flowlines if is_drain(f)])
    cls = np.zeros((R, C), np.uint8)
    cls[drain] = DRAINAGE
    cls[stream] = STREAM
    cls[open_] = OPEN
    return cls


def fetch_raw(grid, epsg):
    """Download the raw 3DHP features for the grid's bounding box."""
    R, C = grid.dem.shape
    x0, y0 = grid.origin_xy
    bounds = (x0, y0 - R * grid.cell, x0 + C * grid.cell, y0)
    print(f"Querying USGS 3DHP for bounds {tuple(round(b) for b in bounds)} (EPSG:{epsg}) ...")
    wb = _query_all(WATERBODY, "featuretype IN (1,2,3,4)", bounds, epsg)
    fl = _query_all(FLOWLINE, "featuretype IN (1,2,3)", bounds, epsg)
    return dict(waterbodies=wb, flowlines=fl, fetched=time.strftime("%Y-%m-%d"))


def _info(raw):
    def legacy(fs):
        return sum(1 for f in fs if str(f["attributes"].get("workunitid", "")).upper() == "NHD")
    wb, fl = raw["waterbodies"], raw["flowlines"]
    return dict(n_waterbodies=len(wb), n_flowlines=len(fl), legacy_nhd_waterbodies=legacy(wb),
                legacy_nhd_flowlines=legacy(fl), fetched=raw.get("fetched", "?"))


# --------------------------------------------------------------------------- apply + cache
def apply_water(grid, classes, block_open=True, stream_cost=3.0, drain_cost=1.5):
    """Attach water to the grid: display classes, block open water, slow down stream cells."""
    grid.water = classes
    if block_open:
        grid.blocked = grid.blocked | (classes == OPEN)
    cm = np.ones(classes.shape, float)
    cm[classes == STREAM] = stream_cost
    cm[classes == DRAINAGE] = drain_cost
    grid.cost_mult = cm
    return grid


def summarize(grid, classes, info=None):
    cell_ha = grid.cell ** 2 / 1e4
    print("Water on your grid:")
    for k, name in CLASS_NAMES.items():
        n = int((classes == k).sum())
        print(f"  {name:20s} {n:8,d} cells ({n * cell_ha:,.1f} ha)")
    if info:
        print(f"  features from service: {info['n_waterbodies']} waterbodies, {info['n_flowlines']} flowlines "
              f"(downloaded {info.get('fetched', '?')}; load_water(..., refresh=True) re-downloads)")
        lw, lf = info["legacy_nhd_waterbodies"], info["legacy_nhd_flowlines"]
        if lw or lf:
            print(f"  NOTE: {lw} waterbodies / {lf} flowlines are legacy NHD data (area not yet remapped "
                  f"from lidar by USGS): less precise.")
        if info["n_waterbodies"] == 0 and info["n_flowlines"] == 0:
            print("  NOTE: nothing returned. Either there is no mapped water here or the query failed silently.")


def load_water(grid, epsg, cache_dir=".", refresh=False, apply=True, **apply_kw):
    """
    3DHP water for `grid`. First call needs internet and saves the RAW features next to your data;
    later calls (and any change to how we rasterize) re-use that file, so no re-download.
    epsg: the raster's EPSG code, e.g. crs.to_epsg() (26912 for your raster).
    """
    import gzip
    print(f"[water.py {WATER_VERSION}] loaded from {os.path.abspath(__file__)}")
    if not epsg:
        raise ValueError("Need the raster's EPSG code (crs.to_epsg() returned None).")
    R, C = grid.dem.shape
    x0, y0 = grid.origin_xy
    path = os.path.join(cache_dir, f"water3dhp_raw_{epsg}_{int(round(x0))}_{int(round(y0))}_"
                                   f"{int(round(C * grid.cell))}x{int(round(R * grid.cell))}.json.gz")
    if os.path.exists(path) and not refresh:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            raw = json.load(fh)
        print(f"Loaded cached 3DHP features: {path}")
    else:
        raw = fetch_raw(grid, epsg)
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            json.dump(raw, fh)
        print(f"Saved 3DHP feature cache: {path}")
    classes = rasterize(grid, raw["waterbodies"], raw["flowlines"])
    summarize(grid, classes, _info(raw))
    if apply:
        apply_water(grid, classes, **apply_kw)
        check_water(grid)
    return classes


def check_water(grid, verbose=True):
    """
    Sanity-check the water on a grid. Returns a dict of problem counts (all should be 0):
      open_not_blocked    open-water cells that routing could still walk on
      stripes             1-cell-wide gaps inside open water (leaks a route could follow)
      islets              tiny walkable specks inside open water (isolated islands, <= 4 cells)
    """
    from scipy.ndimage import label
    if grid.water is None:
        return {}
    op = grid.water == OPEN
    o = np.pad(op, 1)
    gap_v = ~op & o[:-2, 1:-1] & o[2:, 1:-1]          # land cell with water directly above and below
    gap_h = ~op & o[1:-1, :-2] & o[1:-1, 2:]
    lab, n = label(~op)
    sizes = np.bincount(lab.ravel(), minlength=n + 1)[1:] if n else np.array([])
    res = dict(open_not_blocked=int((op & ~grid.blocked).sum()),
               stripes=int((gap_v | gap_h).sum()),
               islets=int(((sizes > 0) & (sizes <= 4)).sum()))
    if verbose:
        print("Water check:", res, "(all 0 = good)")
    return res