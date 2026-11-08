"""
hiker_router.py - slope-limited routing for the lost-hiker app.

Pipeline:
    Grid (DEM + masks) --build_edge_costs--> per-offset cost arrays
                       --cost_to_go--------> T[r, c] = seconds to reach goal
                       --descend-----------> path from any (r, c)

Conventions
    * DEM is a 2D float array in METRES, north-up, row 0 = north edge.
      Project lat/lon to a metric CRS (e.g. UTM) before building the grid.
    * Grad "g" is rise/run (0.10 = 10% grade, ~5.7 deg).
    * Edge cost is in SECONDS and is DIRECTIONAL: cost[u -> v] != cost[v -> u].
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.ndimage import uniform_filter
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


@dataclass
class CostConfig:
    # Hard limits (rise/run). Moves steeper than these are removed from the graph.
    max_up: float = 0.25
    max_down: float = 0.25
    max_cross: float = 0.50  # cross-slope hard limit (sidehilling)

    # Soft penalties
    speed_factor: float = 1.0      # multiply Tobler speed (injured: ~0.6)
    steep_penalty: float = 1.0     # extra cost as |g| approaches its cap (quadratic)
    cross_penalty: float = 2.0     # extra cost as cross-slope approaches max_cross
    rough_penalty: float = 5.0     # per metre of local elevation std-dev
    trail_bonus: float = 0.6       # multiplier on cost when moving trail -> trail

    connectivity: int = 8          # 8 or 16 (adds knight moves, less grid bias)

    @classmethod
    def healthy(cls) -> "CostConfig":
        return cls(max_up=0.25, max_down=0.25, speed_factor=1.0)

    @classmethod
    def injured(cls) -> "CostConfig":
        return cls(
            max_up=0.15,
            max_down=0.12,     # descents are the riskiest for knees/ankles
            max_cross=0.30,
            speed_factor=0.6,
            steep_penalty=3.0,
            cross_penalty=4.0,
        )


@dataclass
class Grid:
    dem: np.ndarray                     # (R, C) metres
    cell: float                         # cell size in metres
    origin_xy: tuple[float, float] = (0.0, 0.0)  # (x_west, y_north) of cell (0,0) corner
    blocked: np.ndarray | None = None   # bool (R, C): cliffs, water, ...
    trails: np.ndarray | None = None    # bool (R, C)
    water: np.ndarray | None = None     # uint8 classes from water.py (display + messages)
    cost_mult: np.ndarray | None = None  # float (R, C) >= 1: multiplier on time to ENTER a cell

    def __post_init__(self):
        R, C = self.dem.shape
        self.blocked = np.zeros((R, C), bool) if self.blocked is None else self.blocked
        self.trails = np.zeros((R, C), bool) if self.trails is None else self.trails

    # --- coordinate helpers (x east, y north, metres in the same CRS as origin_xy)
    def xy_to_rc(self, x: float, y: float) -> tuple[int, int]:
        x0, y0 = self.origin_xy
        c = int(round((x - x0) / self.cell - 0.5))
        r = int(round((y0 - y) / self.cell - 0.5))
        return (min(max(r, 0), self.dem.shape[0] - 1),
                min(max(c, 0), self.dem.shape[1] - 1))

    def rc_to_xy(self, r: float, c: float) -> tuple[float, float]:
        x0, y0 = self.origin_xy
        return (x0 + (c + 0.5) * self.cell, y0 - (r + 0.5) * self.cell)


# --------------------------------------------------------------------------
# Cost model (step 3)
# --------------------------------------------------------------------------

_OFFSETS_8 = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
_OFFSETS_16_EXTRA = [(-2, -1), (-2, 1), (-1, -2), (-1, 2), (1, -2), (1, 2), (2, -1), (2, 1)]


def tobler_speed(g: np.ndarray) -> np.ndarray:
    """Tobler's hiking function. g = rise/run. Returns speed in m/s."""
    return 6.0 * np.exp(-3.5 * np.abs(g + 0.05)) / 3.6


def _shifted(a: np.ndarray, dr: int, dc: int, fill) -> np.ndarray:
    """out[r, c] = a[r+dr, c+dc]; out-of-bounds -> fill."""
    R, C = a.shape
    out = np.full_like(a, fill)
    r0, r1 = max(0, -dr), R - max(0, dr)
    c0, c1 = max(0, -dc), C - max(0, dc)
    out[r0:r1, c0:c1] = a[r0 + dr:r1 + dr, c0 + dc:c1 + dc]
    return out


def build_edge_costs(grid: Grid, cfg: CostConfig):
    """
    Returns list of ((dr, dc), cost) where cost[r, c] is the time in seconds
    to move from (r, c) to (r+dr, c+dc). np.inf = move not allowed.
    """
    dem, cell = grid.dem, grid.cell
    offsets = list(_OFFSETS_8)
    if cfg.connectivity == 16:
        offsets += _OFFSETS_16_EXTRA

    # Terrain gradient vector (dz/drow, dz/dcol) for cross-slope
    gy, gx = np.gradient(dem, cell)

    # Roughness: local elevation std-dev in a 3x3 window (metres)
    m1 = uniform_filter(dem, 3, mode="nearest")
    m2 = uniform_filter(dem * dem, 3, mode="nearest")
    rough = np.sqrt(np.maximum(m2 - m1 * m1, 0.0))

    out = []
    for dr, dc in offsets:
        dist = cell * np.hypot(dr, dc)
        ur, uc = dr / np.hypot(dr, dc), dc / np.hypot(dr, dc)

        dz = _shifted(dem, dr, dc, np.nan) - dem
        g = dz / dist                                     # along-travel gradient
        cross = np.abs(-uc * gy + ur * gx)                # cross-slope at the source cell

        speed = tobler_speed(g) * cfg.speed_factor
        t = dist / speed

        # Soft penalties (multipliers on time)
        g_cap = np.where(g >= 0, cfg.max_up, cfg.max_down)
        mult = 1.0 + cfg.steep_penalty * (np.abs(g) / g_cap) ** 2
        mult += cfg.cross_penalty * (cross / cfg.max_cross) ** 2
        mult += cfg.rough_penalty * 0.5 * (rough + _shifted(rough, dr, dc, 0.0)) / 10.0
        both_trail = grid.trails & _shifted(grid.trails, dr, dc, False)
        mult = np.where(both_trail, mult * cfg.trail_bonus, mult)
        if grid.cost_mult is not None:                      # streams, wet ground, ...
            mult = mult * _shifted(grid.cost_mult, dr, dc, 1.0)
        cost = t * mult

        # Hard blocks
        bad = (
            np.isnan(g)
            | (g > cfg.max_up)
            | (g < -cfg.max_down)
            | (cross > cfg.max_cross)
            | grid.blocked
            | _shifted(grid.blocked, dr, dc, True)
        )
        # Moves must not squeeze through a gap between blocked cells (e.g. a diagonal step between
        # two water cells that only touch at their corners): every cell the move passes beside or
        # over has to be walkable.
        if abs(dr) == 1 and abs(dc) == 1:
            passed = [(dr, 0), (0, dc)]
        elif abs(dr) == 2:
            sr = int(np.sign(dr))
            passed = [(sr, 0), (sr, dc)]
        elif abs(dc) == 2:
            sc = int(np.sign(dc))
            passed = [(0, sc), (dr, sc)]
        else:
            passed = []
        for mr, mc in passed:
            bad |= _shifted(grid.blocked, mr, mc, True)
        out.append(((dr, dc), np.where(bad, np.inf, cost)))
    return out


# --------------------------------------------------------------------------
# Search (step 4)
# --------------------------------------------------------------------------


def cost_to_go(grid: Grid, edge_costs, goal_rc: tuple[int, int],
               forward: bool = False) -> np.ndarray:
    """
    Backward Dijkstra from goal. T[r, c] = minimum seconds to reach the goal
    from (r, c), following the directional edge costs. inf = unreachable.

    forward=True flips it: T[r, c] = seconds to get from `goal_rc` (really a
    start cell) TO (r, c). Use it to find everything reachable from a position.
    """
    R, C = grid.dem.shape
    G = _graph(grid, edge_costs, forward)
    d = dijkstra(G, directed=True, indices=goal_rc[0] * C + goal_rc[1])
    return d.reshape(R, C)


def _graph(grid: Grid, edge_costs, forward: bool):
    R, C = grid.dem.shape
    ids = np.arange(R * C).reshape(R, C)
    rows, cols, w = [], [], []
    for (dr, dc), cost in edge_costs:
        ok = np.isfinite(cost)
        u = ids[ok]
        v = _shifted(ids, dr, dc, -1)[ok]
        if forward:
            rows.append(u)
            cols.append(v)
        else:         # transposed graph: edge v -> u so Dijkstra runs "backwards"
            rows.append(v)
            cols.append(u)
        w.append(cost[ok])
    G = csr_matrix((np.concatenate(w), (np.concatenate(rows), np.concatenate(cols))),
                   shape=(R * C, R * C))
    return G


def forward_tree(grid: Grid, edge_costs, start_rc):
    """
    One Dijkstra from `start_rc`. Returns (F, pred):
      F[r, c]    seconds-of-cost from start to (r, c), inf if unreachable
      pred[r, c] flat index (r*C + c) of the previous cell on the best route,
                 negative if none. Follow pred from any goal back to the start.
    This is what makes free destination selection instant.
    """
    R, C = grid.dem.shape
    G = _graph(grid, edge_costs, forward=True)
    d, pred = dijkstra(G, directed=True, indices=start_rc[0] * C + start_rc[1],
                       return_predecessors=True)
    return d.reshape(R, C), pred.reshape(R, C)


def descend(T: np.ndarray, edge_costs, start_rc, goal_rc, max_steps: int = 100_000):
    """Follow the cost-to-go field downhill from start to goal. Returns [(r, c), ...]."""
    if not np.isfinite(T[start_rc]):
        return None  # no feasible route under the current constraints
    r, c = start_rc
    path = [(r, c)]
    R, C = T.shape
    for _ in range(max_steps):
        if (r, c) == tuple(goal_rc):
            return path
        best, best_rc = np.inf, None
        for (dr, dc), cost in edge_costs:
            nr, nc = r + dr, c + dc
            if 0 <= nr < R and 0 <= nc < C:
                total = cost[r, c] + T[nr, nc]
                if total < best:
                    best, best_rc = total, (nr, nc)
        if best_rc is None:
            return None
        r, c = best_rc
        path.append((r, c))
    return None


# --------------------------------------------------------------------------
# Handling localization uncertainty
# --------------------------------------------------------------------------


def expected_cost(T: np.ndarray, posterior: np.ndarray) -> float:
    """Posterior-weighted time to goal (seconds). posterior sums to 1, same shape as T."""
    finite = np.isfinite(T)
    p = np.where(finite, posterior, 0.0)
    return float((p * np.where(finite, T, 0.0)).sum() / max(p.sum(), 1e-12))


def unreachable_mass(T: np.ndarray, posterior: np.ndarray) -> float:
    """Probability mass sitting on cells with no feasible route."""
    return float(posterior[~np.isfinite(T)].sum())


def first_leg_agreement(T, edge_costs, goal_rc, posterior, n_samples=50, leg_cells=8, seed=0):
    """
    Sample positions from the posterior, descend from each, and return the
    headings (deg true north) of each sample's first `leg_cells` cells. If they
    agree, it is safe to give one instruction; if they scatter, ask for another fix.
    """
    rng = np.random.default_rng(seed)
    flat = posterior.ravel() / posterior.sum()
    picks = rng.choice(flat.size, size=n_samples, p=flat)
    headings = []
    for k in picks:
        s = divmod(int(k), posterior.shape[1])
        p = descend(T, edge_costs, s, goal_rc)
        if p and len(p) > 1:
            q = p[min(leg_cells, len(p) - 1)]
            headings.append(bearing_deg(s, q))
    return np.array(headings)


def circular_stats(headings_deg: np.ndarray):
    """Mean heading (deg) and circular std (deg). Handles the 359/1 wraparound."""
    a = np.radians(headings_deg)
    C, S = np.cos(a).mean(), np.sin(a).mean()
    mean = float(np.degrees(np.arctan2(S, C)) % 360)
    Rbar = np.hypot(C, S)
    std = float(np.degrees(np.sqrt(-2 * np.log(max(Rbar, 1e-12)))))
    return mean, std


# --------------------------------------------------------------------------
# Guidance helpers
# --------------------------------------------------------------------------


def bearing_deg(a_rc, b_rc) -> float:
    """True-north bearing from cell a to cell b (north-up grid)."""
    dy = -(b_rc[0] - a_rc[0])   # rows increase southward
    dx = b_rc[1] - a_rc[1]
    return float(np.degrees(np.arctan2(dx, dy)) % 360)


def next_leg(path, grid: Grid, leg_m: float = 150.0):
    """Walk ~leg_m along the path; return (bearing_deg, distance_m, waypoint_rc)."""
    acc, prev = 0.0, path[0]
    for p in path[1:]:
        acc += grid.cell * np.hypot(p[0] - prev[0], p[1] - prev[1])
        prev = p
        if acc >= leg_m:
            break
    return bearing_deg(path[0], prev), acc, prev


def path_stats(path, grid: Grid):
    """Distance, ascent, descent, max up/down gradient along a path (sanity check)."""
    d = up = down = 0.0
    g_up = g_dn = 0.0
    for a, b in zip(path[:-1], path[1:]):
        dist = grid.cell * np.hypot(b[0] - a[0], b[1] - a[1])
        dz = grid.dem[b] - grid.dem[a]
        d += dist
        up += max(dz, 0)
        down += max(-dz, 0)
        g_up, g_dn = max(g_up, dz / dist), max(g_dn, -dz / dist)
    return dict(distance_m=d, ascent_m=up, descent_m=down, max_up=g_up, max_down=g_dn)


# --------------------------------------------------------------------------
# Demo
# --------------------------------------------------------------------------

if __name__ == "__main__":
    # Synthetic terrain: a big hill between start and goal
    R = C = 200
    cell = 20.0
    yy, xx = np.mgrid[0:R, 0:C]
    dem = (400 * np.exp(-(((yy - 100) / 40) ** 2 + ((xx - 100) / 40) ** 2))
           + 30 * np.sin(xx / 15) * np.cos(yy / 20))
    grid = Grid(dem=dem.astype(float), cell=cell)

    start, goal = (100, 20), (100, 180)   # straight line goes over the summit

    for name, cfg in [("healthy", CostConfig.healthy()), ("injured", CostConfig.injured())]:
        ec = build_edge_costs(grid, cfg)
        T = cost_to_go(grid, ec, goal)
        path = descend(T, ec, start, goal)
        if path is None:
            print(f"{name}: no feasible route")
            continue
        s = path_stats(path, grid)
        b, d, wp = next_leg(path, grid)
        print(f"{name}: {s['distance_m']:.0f} m, ETA {T[start] / 60:.0f} min, "
              f"max up {s['max_up']:.1%}, max down {s['max_down']:.1%}, "
              f"first leg: {b:.0f} deg for {d:.0f} m")

    # Uncertain start: a blob of probability around `start`
    post = np.exp(-(((yy - start[0]) / 6) ** 2 + ((xx - start[1]) / 6) ** 2))
    post /= post.sum()
    ec = build_edge_costs(grid, CostConfig.injured())
    T = cost_to_go(grid, ec, goal)
    print(f"expected ETA over posterior: {expected_cost(T, post) / 60:.0f} min, "
          f"unreachable mass: {unreachable_mass(T, post):.1%}")
    hd = first_leg_agreement(T, ec, goal, post)
    m, sd = circular_stats(hd)
    print(f"first-leg heading: mean {m:.0f} deg, circular std {sd:.1f} deg")