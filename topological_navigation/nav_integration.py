"""
nav_integration.py - glue between the teammate's DEM/candidate data and hiker_router.

Run `inspect_raster(path)` FIRST: it prints the CRS, cell size and size of the
raster, which decide every choice below.

NOTE: load_grid / latlon_to_rc need rasterio + pyproj and were NOT run against
your real GeoTIFF (they were not installable where this was written). The numpy
pieces (pool_posterior, true<->magnetic helpers) were tested on synthetic data.
"""
from __future__ import annotations

import numpy as np

from hiker_router import (CostConfig, Grid, build_edge_costs, cost_to_go,
                          descend, expected_cost, first_leg_agreement,
                          circular_stats, next_leg, path_stats)


def inspect_raster(path: str) -> None:
    import rasterio
    with rasterio.open(path) as ds:
        print("CRS:        ", ds.crs)             # want a projected CRS in metres (UTM)
        print("cell size:  ", ds.res)             # (x, y); expect (1.0, 1.0)
        print("shape:      ", ds.shape, f"-> {ds.shape[0] * ds.shape[1] / 1e6:.0f} M cells")
        print("bounds:     ", ds.bounds)
        print("nodata:     ", ds.nodata)
        print("rotation?   ", ds.transform.b != 0 or ds.transform.d != 0)  # must be False


def load_grid(path: str, cell_m: float = 10.0):
    """
    Read the DEM at routing resolution (area-averaged), not 1 m.
    Returns (Grid, crs). NoData cells become blocked, with filled elevation so
    gradients stay finite.
    """
    import rasterio
    from rasterio.enums import Resampling

    with rasterio.open(path) as ds:
        f = max(1, round(cell_m / ds.res[0]))
        out_shape = (ds.height // f, ds.width // f)
        dem = ds.read(1, out_shape=out_shape, resampling=Resampling.average,
                      masked=True)
        left, top = ds.bounds.left, ds.bounds.top
        width_m = ds.bounds.right - ds.bounds.left
        crs = ds.crs

    bad = np.ma.getmaskarray(dem)
    dem = dem.filled(np.nan).astype(float)
    dem[bad] = np.nanmean(dem)                      # keep gradients finite
    cell = width_m / out_shape[1]                   # actual cell size after resampling
    return Grid(dem=dem, cell=cell, origin_xy=(left, top), blocked=bad), crs


def latlon_to_rc(grid: Grid, crs, lat: float, lon: float) -> tuple[int, int]:
    from pyproj import Transformer
    x, y = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(lon, lat)
    return grid.xy_to_rc(x, y)


def pool_posterior(weights_full: np.ndarray, grid_shape: tuple[int, int]) -> np.ndarray:
    """
    Resample the localizer's full-resolution candidate mask/weights (e.g. the
    1 m boolean mask) to routing resolution by SUMMING (never averaging) so
    probability mass is conserved. Returns a posterior that sums to 1.
    """
    H, W = weights_full.shape
    R, C = grid_shape
    rr, cc = np.nonzero(weights_full)
    w = weights_full[rr, cc].astype(float)
    r = np.minimum(rr * R // H, R - 1)
    c = np.minimum(cc * C // W, C - 1)
    post = np.bincount(r * C + c, weights=w, minlength=R * C).reshape(R, C)
    return post / post.sum()


def true_to_magnetic(true_deg: float, declination_deg: float) -> float:
    """declination: degrees EAST of true north is positive."""
    return (true_deg - declination_deg) % 360


def magnetic_to_true(mag_deg: float, declination_deg: float) -> float:
    return (mag_deg + declination_deg) % 360


def guidance(grid: Grid, goal_rc, posterior, cfg: CostConfig, declination_deg: float):
    """One navigation update from a posterior over positions."""
    ec = build_edge_costs(grid, cfg)
    T = cost_to_go(grid, ec, goal_rc)             # cache this per (goal, cfg)

    best = np.unravel_index(np.argmax(posterior), posterior.shape)
    path = descend(T, ec, best, goal_rc)
    if path is None:
        return {"status": "no_feasible_route"}

    hd = first_leg_agreement(T, ec, goal_rc, posterior)
    mean_h, spread = circular_stats(hd) if len(hd) else (np.nan, np.nan)
    bearing, dist, wp = next_leg(path, grid)
    return {
        "status": "ok" if spread < 20 else "ambiguous_ask_for_another_fix",
        "heading_magnetic": true_to_magnetic(bearing, declination_deg),
        "leg_m": dist,
        "eta_min": expected_cost(T, posterior) / 60,
        "heading_spread_deg": spread,
        "path_stats": path_stats(path, grid),
    }


if __name__ == "__main__":
    # Synthetic test of the numpy pieces: fake a 1 m raster's candidate band,
    # pool it to a 10 m routing grid, and route from it.
    R = C = 150
    yy, xx = np.mgrid[0:R, 0:C]
    dem10 = 300 * np.exp(-(((yy - 75) / 30) ** 2 + ((xx - 75) / 30) ** 2)) + 2000
    grid = Grid(dem=dem10, cell=10.0)

    full = np.zeros((R * 10, C * 10), bool)       # pretend 1 m candidate mask
    full[740:760, 100:140] = True                 # a patch west of the hill
    post = pool_posterior(full, (R, C))
    print("pooled mass:", post.sum().round(3), "nonzero cells:", int((post > 0).sum()))

    out = guidance(grid, goal_rc=(75, 140), posterior=post,
                   cfg=CostConfig.injured(), declination_deg=10.0)
    print({k: (round(v, 1) if isinstance(v, float) else v) for k, v in out.items()
           if k != "path_stats"})
