"""
benchmark.py - stress the router with feasible goals of varying difficulty.

    from benchmark import run_benchmark
    run_benchmark(grid, start, CostConfig.injured(), plot=True)

What it does
  1. One forward Dijkstra from `start` -> every cell the caps can reach.
  2. Picks one goal per difficulty bucket from that set (so each is feasible).
  3. Routes to each, and CHECKS the result:
       - path runs start -> goal along allowed edges only
       - path cost == cost-to-go value (descend() is truly following the field)
       - forward cost (start->goal) == backward cost (goal<-start): catches bugs
         in the directional/asymmetric edge costs
       - gradient caps respected along the whole path
       - loosening the caps never makes the route slower
"""
from dataclasses import replace

import numpy as np
from scipy.ndimage import convolve

from hiker_router import (CostConfig, Grid, build_edge_costs, cost_to_go,
                          descend, path_stats, tobler_speed)


def _pick_goals(grid, F, start, cfg, rng):
    reach = np.argwhere(np.isfinite(F))
    d = np.hypot(reach[:, 0] - start[0], reach[:, 1] - start[1]) * grid.cell
    f = F[reach[:, 0], reach[:, 1]]
    dz = grid.dem[reach[:, 0], reach[:, 1]] - grid.dem[start]
    flat_speed = float(tobler_speed(np.array(0.0))) * cfg.speed_factor
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = f / np.maximum(d / flat_speed, 1e-9)          # "x slower than flat ground"

    # reachable cells that border unreachable terrain = hugging the walls
    unreach = (~np.isfinite(F)).astype(float)
    nb = convolve(unreach, np.ones((3, 3)), mode="constant", cval=1.0)
    wall = nb[reach[:, 0], reach[:, 1]] >= 4

    dmax = float(d.max())
    none = np.zeros(len(d), bool)
    far_enough = d > 0.3 * dmax
    buckets = {
        "near":            (d > 0.15 * dmax) & (d < 0.35 * dmax),
        "mid":             (d > 0.45 * dmax) & (d < 0.65 * dmax),
        "far":             d > 0.85 * dmax,
        "big detour":      (far_enough & (ratio >= np.nanpercentile(ratio[far_enough], 90)))
                           if far_enough.any() else none,
        "biggest climb":   dz >= np.percentile(dz, 98) if dz.max() > 20 else none,
        "biggest descent": dz <= np.percentile(dz, 2) if dz.min() < -20 else none,
        "hugging walls":   wall & far_enough,
        "slowest (time)":  f >= f.max() * 0.999,
    }
    goals, used = {}, set()
    for name, m in buckets.items():
        idx = [i for i in np.flatnonzero(m)
               if tuple(int(x) for x in reach[i]) not in used]
        if idx:
            k = int(rng.choice(idx))
            g = tuple(int(x) for x in reach[k])
            used.add(g)
            goals[name] = (g, float(d[k]))
    return goals


def _path_cost(path, ec_map):
    tot = 0.0
    for a, b in zip(path[:-1], path[1:]):
        off = (b[0] - a[0], b[1] - a[1])
        if off not in ec_map:
            return None
        tot += ec_map[off][a]
    return tot


def run_benchmark(grid: Grid, start, cfg: CostConfig, plot=False, seed=0, basemap=False):
    rng = np.random.default_rng(seed)
    ec = build_edge_costs(grid, cfg)
    ec_map = dict(ec)
    F = cost_to_go(grid, ec, start, forward=True)
    print(f"reachable from start: {np.isfinite(F).sum():,} cells "
          f"({np.isfinite(F).mean():.1%} of grid)")
    _r = np.argwhere(np.isfinite(F))
    print(f"farthest reachable point: "
          f"{np.hypot(_r[:, 0] - start[0], _r[:, 1] - start[1]).max() * grid.cell:.0f} m from start")

    # looser caps, everything else identical -> should never be slower
    loose = replace(cfg, max_up=cfg.max_up * 1.5, max_down=cfg.max_down * 1.5,
                    max_cross=cfg.max_cross * 1.5)
    ec_loose = build_edge_costs(grid, loose)

    goals = _pick_goals(grid, F, start, cfg, rng)
    rows, paths = [], {}
    for name, (goal, straight) in goals.items():
        T = cost_to_go(grid, ec, goal)
        path = descend(T, ec, start, goal)
        st = path_stats(path, grid)

        c = _path_cost(path, ec_map)
        checks = {
            "endpoints": path[0] == tuple(start) and path[-1] == goal,
            "edges_valid": c is not None,
            "follows_field": c is not None and abs(c - T[start]) < 1e-6 * max(1, T[start]),
            "fwd==bwd": abs(F[goal] - T[start]) < 1e-6 * max(1, T[start]),
            "caps_ok": st["max_up"] <= cfg.max_up + 1e-9 and st["max_down"] <= cfg.max_down + 1e-9,
            "loose<=strict": cost_to_go(grid, ec_loose, goal)[start] <= T[start] + 1e-6,
        }
        rows.append((name, goal, straight, T[start] / 60, st, checks))
        paths[name] = path

    print(f"\n{'bucket':17s} {'goal':>12s} {'straight':>9s} {'ETA':>7s} {'path':>7s} "
          f"{'detour':>7s} {'up':>6s} {'down':>6s} {'maxUp':>6s} {'maxDn':>6s}  checks")
    for name, goal, straight, eta, st, checks in rows:
        bad = [k for k, v in checks.items() if not v]
        print(f"{name:17s} {str(goal):>12s} {straight:8.0f}m {eta:5.0f}min {st['distance_m']:6.0f}m "
              f"{st['distance_m'] / max(straight, 1):6.2f}x {st['ascent_m']:5.0f}m {st['descent_m']:5.0f}m "
              f"{st['max_up']:6.1%} {st['max_down']:6.1%}  "
              + ("all pass" if not bad else "FAIL: " + ", ".join(bad)))

    if plot and paths:
        from plotting import plot_routes
        plot_routes(grid, paths, start, reach_mask=np.isfinite(F), basemap=basemap,
                    title="routes to goals of increasing difficulty")
    return rows


if __name__ == "__main__":
    # synthetic rugged terrain with a ridge that has one gap (pass)
    from scipy.ndimage import gaussian_filter
    R = C = 220
    yy, xx = np.mgrid[0:R, 0:C]
    rng = np.random.default_rng(1)
    dem = (0.25 * yy + 120 * np.exp(-((xx - 110) / 10.0) ** 2) * (1 - np.exp(-((yy - 60) / 10.0) ** 2))
           + gaussian_filter(rng.normal(0, 40, (R, C)), 6) * 4)
    grid = Grid(dem=dem, cell=10.0)
    run_benchmark(grid, (150, 40), CostConfig.injured())
