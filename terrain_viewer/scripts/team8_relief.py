"""Team-8 relief map: paint topo.jpg in the team's terrain-map style, the one Noah's route plots use.

  python -I scripts/team8_relief.py data/<id> [data/<id> ...]

team8_terrain_prep.py calls render() for a new area. Run this file to repaint areas that are already prepared:
it reads terrain_rgb.png, water_mask.png and terrain_meta.json, writes topo.jpg and records the tint range in
terrain_meta.json (map_style), so the viewer's key can print it. Rebuild bundle.js afterwards (team8_bundle.py).

The look follows topological_navigation/plotting.py on the Sprint2-NavAlgorithm branch, so the viewer, the phone,
the big screen and the route plots read as one map:
  elevation tint  #3f7d3a, #8fba62, #cfba78, #9a7550, #5b5550 from the 2nd to the 98th percentile (no blue on land)
  relief shading  light from 315 deg, 40 deg up, vertical exaggeration 2, soft-light blend (matplotlib LightSource),
                  worked out on a grid of about 10 m, like Noah's plots, and smoothed up to the texture
  water           #3b8ed0 at 95 %
  contours        black at 35 %, every fifth (the index line) wider at 55 %, interval from terrain_meta.json
  1 km UTM grid   thin white lines, the grid an MGRS reading refers to
"""
import json, math, os, sys
import numpy as np
from PIL import Image
from scipy import ndimage
from rasterio.warp import transform as rtransform

STOPS = ((0.00, "#3f7d3a"), (0.25, "#8fba62"), (0.50, "#cfba78"), (0.75, "#9a7550"), (1.00, "#5b5550"))
WATER, WATER_ALPHA = "#3b8ed0", 0.95
AZIMUTH, ALTITUDE, VERT_EXAG, SHADE_CELL_M = 315.0, 40.0, 2.0, 10.0
CONTOUR_ALPHA = (0.35, 0.55)                                   # minor, index
GRID_RGB, GRID_ALPHA = (255, 255, 255), 0.6


def rgb01(h):
    return np.array([int(h[i:i + 2], 16) for i in (1, 3, 5)], np.float64) / 255.0


def tint(z, vmin, vmax):
    """matplotlib LinearSegmentedColormap.from_list over STOPS, held at the end colours outside vmin..vmax."""
    t = np.clip((z - vmin) / max(vmax - vmin, 1e-6), 0.0, 1.0)
    xs, cs = [s for s, _ in STOPS], np.stack([rgb01(c) for _, c in STOPS])
    return np.stack([np.interp(t, xs, cs[:, k]) for k in range(3)], -1)


def hillshade(z, dx, dy):
    """matplotlib LightSource(AZIMUTH, ALTITUDE).hillshade(z, VERT_EXAG, dx, dy), with its min-max stretch."""
    e_dy, e_dx = np.gradient(VERT_EXAG * z, -dy, dx)          # row 0 is north
    n = np.stack([-e_dx, -e_dy, np.ones_like(z)], -1)
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    az, alt = math.radians(90.0 - AZIMUTH), math.radians(ALTITUDE)
    i = n @ np.array([math.cos(az) * math.cos(alt), math.sin(az) * math.cos(alt), math.sin(alt)])
    lo, hi = float(i.min()), float(i.max())
    if hi - lo > 1e-6:
        i = (i - lo) / (hi - lo)
    return np.clip(i, 0.0, 1.0)


def lines(val, step, width_px, gpx):
    """Anti-aliased lines where val crosses a multiple of step, width_px wide; gpx is how much val changes per pixel."""
    d = np.abs(np.mod(val / step + 0.5, 1.0) - 0.5) * step / np.maximum(gpx, 1e-6)
    return np.clip(0.5 * width_px + 0.5 - d, 0.0, 1.0)


def utm_grid(bounds, W, H):
    """UTM easting and northing of every texture pixel (row 0 north), and the zone and EPSG code."""
    west, south, east, north = bounds
    lat_c, lon_c = (south + north) / 2, (west + east) / 2
    zone = int((lon_c + 180) // 6) + 1
    epsg = f"EPSG:{(32600 if lat_c >= 0 else 32700) + zone}"
    dlon, dlat = (east - west) / W, (north - south) / H
    sr, sc = np.arange(0, H + 8, 8).clip(max=H - 1), np.arange(0, W + 8, 8).clip(max=W - 1)
    LL = np.meshgrid(west + (sc + 0.5) * dlon, north - (sr + 0.5) * dlat)
    EE, NN = rtransform("EPSG:4326", epsg, LL[0].ravel().tolist(), LL[1].ravel().tolist())
    EE, NN = np.array(EE).reshape(LL[0].shape), np.array(NN).reshape(LL[0].shape)
    rr, cc = np.mgrid[0:H, 0:W]
    E = ndimage.map_coordinates(EE, [rr / 8.0, cc / 8.0], order=1, mode="nearest")
    N = ndimage.map_coordinates(NN, [rr / 8.0, cc / 8.0], order=1, mode="nearest")
    return E, N, zone, epsg


def render(Z, water, cell, interval, index, bounds, utm=None):
    """The map picture as an RGB uint8 array, and the tint range in metres.

    Z: heights in metres on the texture grid, row 0 north. water: bool mask on the same grid.
    cell: (dx, dy) in metres. interval, index: contour steps in metres. utm: (E, N) from utm_grid, if already made."""
    H, W = Z.shape
    dx, dy = float(cell[0]), float(cell[1])
    Z = np.asarray(Z, np.float64)
    k = max(1, int(round(SHADE_CELL_M / max(dx, dy))))
    while H % k or W % k:
        k -= 1
    Zs = Z.reshape(H // k, k, W // k, k).mean(axis=(1, 3)) if k > 1 else Z
    vmin, vmax = (float(v) for v in np.percentile(Zs, [2, 98]))
    shade = hillshade(Zs, dx * k, dy * k)
    if k > 1:
        shade = np.asarray(Image.fromarray(shade.astype(np.float32)).resize((W, H), Image.BILINEAR), np.float64)
    base = tint(Z, vmin, vmax)
    s = shade[..., None]
    rgb = (2 * s * base + (1 - 2 * s) * base ** 2) * 255.0     # soft light, as LightSource.blend_soft_light
    del shade, s, base

    if water.any():
        w = ndimage.gaussian_filter(water.astype(np.float32), 0.8)[..., None] * WATER_ALPHA
        rgb = rgb * (1 - w) + rgb01(WATER) * 255.0 * w

    scale = max(W, H * 1.35) / 1150.0                          # texture pixels per screen pixel at the whole-tile view
    zc = ndimage.gaussian_filter(Z, 1.0)                       # also smooths out the 0.1 m steps of decoded heights
    gy, gx = np.gradient(zc, dy, dx)
    g = np.hypot(gx, gy)
    gpx = g * (dx + dy) / 2                                    # metres of climb per texture pixel
    minor = lines(zc, interval, max(0.9, 0.7 * scale), gpx) * np.where(g > math.tan(math.radians(55)), 0.25, 1.0)
    major = lines(zc, index, max(1.4, 1.4 * scale), gpx)
    minor[water] = 0
    major[water] = 0
    for a, alpha in ((minor, CONTOUR_ALPHA[0]), (major, CONTOUR_ALPHA[1])):
        rgb *= (1 - a * alpha)[..., None]                      # black
    del zc, gx, gy, g, gpx, minor, major

    E, N = utm if utm is not None else utm_grid(bounds, W, H)[:2]
    for v in (E, N):
        a = (lines(v, 1000.0, max(0.8, 0.7 * scale), np.hypot(*np.gradient(v))) * GRID_ALPHA)[..., None]
        rgb = rgb * (1 - a) + np.array(GRID_RGB, np.float64) * a
    return np.clip(rgb + 0.5, 0, 255).astype(np.uint8), [round(vmin), round(vmax)]


def style_note(tint_m):
    return dict(name="team relief", source="Noah's plotting.py, Sprint2-NavAlgorithm", tint_m=tint_m,
                tint=[c for _, c in STOPS], water=WATER)


def save(img, path):
    Image.fromarray(img).save(path, quality=90, optimize=True)


def repaint(data_dir):
    mp = os.path.join(data_dir, "terrain_meta.json")
    meta = json.load(open(mp, encoding="utf-8"))
    dec = meta["elevation_decoder"]
    t = np.asarray(Image.open(os.path.join(data_dir, meta["files"]["terrain_rgb"])).convert("RGB"), np.float64)
    Z = t[..., 0] * dec["rScaler"] + t[..., 1] * dec["gScaler"] + t[..., 2] * dec["bScaler"] + dec["offset"]
    water = np.asarray(Image.open(os.path.join(data_dir, meta["files"]["water_mask"])).convert("L")) > 127
    img, tint_m = render(Z, water, meta["grid"]["cell_m"], meta["contours"]["interval_m"], meta["contours"]["index_m"],
                         meta["bounds"])
    save(img, os.path.join(data_dir, meta["files"]["topo"]))
    meta["map_style"] = style_note(tint_m)
    with open(mp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    print(data_dir, img.shape[1], "x", img.shape[0], "tint", tint_m, "m,",
          round(os.path.getsize(os.path.join(data_dir, meta["files"]["topo"])) / 1e6, 2), "MB")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for d in sys.argv[1:]:
        repaint(d.rstrip("/\\"))
