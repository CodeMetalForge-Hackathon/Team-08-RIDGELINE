"""
destination.py - free destination selection with honest warnings.

    planner = Planner(grid, start)           # once per position fix (a few seconds / tier)
    res = planner.plan(goal_rc)              # instant per destination
    res = planner.plan_latlon(lat, lon, crs)
    pick_destination(planner)                # click-on-map demo

Each destination is routed with the GENTLEST tier that connects it. Tier 0 is the
comfortable injured preset; later tiers allow steeper ground and raise the
warning level. If nothing connects, you get the closest point that is safely
reachable and an explanation. Tiers are computed lazily and cached.

res["severity"]: ok | caution | hard | severe | extreme | unreachable | invalid
res["warnings"]: human-readable reasons, ready for the UI
"""
from dataclasses import replace
import textwrap

import numpy as np

from hiker_router import CostConfig, Grid, build_edge_costs, forward_tree, tobler_speed
from water import OPEN, STREAM

_INJ = CostConfig.injured()

# All tiers keep the injured speed/penalties; only the hard caps loosen.
TIERS = [
    ("comfortable", _INJ),
    ("moderate",    replace(_INJ, max_up=0.22, max_down=0.18, max_cross=0.50)),
    ("steep",       replace(_INJ, max_up=0.35, max_down=0.30, max_cross=0.80)),
    ("very steep",  replace(_INJ, max_up=0.55, max_down=0.45, max_cross=1.20)),
    ("scramble",    replace(_INJ, max_up=1.00, max_down=0.80, max_cross=2.00)),
]
SEVERITY = ["ok", "caution", "hard", "severe", "extreme"]
COLORS = {"ok": "#1a9850", "caution": "#d4b100", "hard": "#f46d43",
          "severe": "#d73027", "extreme": "#7f0000", "unreachable": "#3d0000",
          "invalid": "#888888"}


class Planner:
    def __init__(self, grid: Grid, start, tiers=TIERS):
        self.grid = grid
        self.start = tuple(int(x) for x in start)
        self.tiers = tiers
        self._layers = {}
        if grid.water is not None:                       # never route over open water, whatever the load order
            grid.blocked = grid.blocked | (grid.water == OPEN)
        self._gy, self._gx = np.gradient(grid.dem, grid.cell)
        self._slope = np.hypot(self._gy, self._gx)

    # ------------------------------------------------------------------ core
    def _layer(self, i):
        if i not in self._layers:
            name, cfg = self.tiers[i]
            ec = build_edge_costs(self.grid, cfg)
            F, pred = forward_tree(self.grid, ec, self.start)
            self._layers[i] = dict(name=name, cfg=cfg, F=F, pred=pred)
        return self._layers[i]

    def _trace(self, L, goal):
        R, C = self.grid.dem.shape
        pred = L["pred"].ravel()
        s = self.start[0] * C + self.start[1]
        i = goal[0] * C + goal[1]
        out = [i]
        while i != s:
            i = int(pred[i])
            if i < 0:
                return None
            out.append(i)
        return [(k // C, k % C) for k in reversed(out)]

    def plan(self, goal, snap_m: float | None = None):
        """
        Route to `goal` (row, col). A goal on blocked cells (open water, missing data) is moved to
        the nearest walkable cell, however far away that is (snap_m=None), and the warning says so.
        Pass snap_m to refuse snapping farther than that many metres.
        """
        R, C = self.grid.dem.shape
        goal = tuple(int(x) for x in goal)
        note = None
        if 0 <= goal[0] < R and 0 <= goal[1] < C and self.grid.blocked[goal]:
            snapped, d = self._nearest_walkable(goal)
            if snapped is not None and (snap_m is None or d <= snap_m):
                what = ("in open water (lake or river)"
                        if self.grid.water is not None and self.grid.water[goal] == OPEN
                        else "on blocked terrain (missing map data or an obstacle)")
                note = (f"That point is {what}, so the route goes to the nearest walkable "
                        f"point, {d:.0f} m away.")
                requested, goal = goal, snapped
        res = self._plan_cell(goal)
        if note:
            res["warnings"].insert(0, note)
            res["requested_goal"] = requested
        return res

    def _nearest_walkable(self, goal):
        if not hasattr(self, "_edt"):
            from scipy.ndimage import distance_transform_edt
            self._edt = distance_transform_edt(self.grid.blocked, return_indices=True)
        dist, (ir, ic) = self._edt
        d = float(dist[goal]) * self.grid.cell
        snapped = (int(ir[goal]), int(ic[goal]))
        return (None, d) if self.grid.blocked[snapped] else (snapped, d)

    def _plan_cell(self, goal):
        R, C = self.grid.dem.shape
        if not (0 <= goal[0] < R and 0 <= goal[1] < C):
            return dict(severity="invalid", path=[self.start], warnings=["That destination is outside the map."])
        if goal == self.start:
            return dict(severity="ok", path=[self.start], warnings=["You are already at that location."],
                        tier="comfortable", eta_min=0.0, distance_m=0.0, ascent_m=0.0,
                        descent_m=0.0, hard_sections=[])

        for i in range(len(self.tiers)):
            L = self._layer(i)
            if np.isfinite(L["F"][goal]):
                return self._report(self._trace(L, goal), i, goal)
        return self._unreachable(goal)

    def plan_latlon(self, lat, lon, crs):
        from pyproj import Transformer
        x, y = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(lon, lat)
        x0, y0 = self.grid.origin_xy
        c = int(np.floor((x - x0) / self.grid.cell))
        r = int(np.floor((y0 - y) / self.grid.cell))
        return self.plan((r, c))

    # -------------------------------------------------------------- analysis
    def _analyze(self, path):
        g_, cell = self.grid, self.grid.cell
        cfg0 = self.tiers[0][1]
        p = np.array(path)
        a, b = p[:-1], p[1:]
        dr, dc = b[:, 0] - a[:, 0], b[:, 1] - a[:, 1]
        n = np.hypot(dr, dc)
        dist = n * cell
        dz = g_.dem[b[:, 0], b[:, 1]] - g_.dem[a[:, 0], a[:, 1]]
        g = dz / dist
        cross = np.abs(-(dc / n) * self._gy[a[:, 0], a[:, 1]] + (dr / n) * self._gx[a[:, 0], a[:, 1]])

        # real walking-time estimate (the router's "cost" also includes penalties)
        eta_min = float((dist / (tobler_speed(g) * cfg0.speed_factor)).sum() / 60)
        cum = np.concatenate([[0.0], np.cumsum(dist)])

        ratios = np.vstack([g / cfg0.max_up, -g / cfg0.max_down, cross / cfg0.max_cross])
        viol = ratios.max(axis=0) > 1.0
        sections = []
        idx = np.flatnonzero(viol)
        if len(idx):
            for grp in np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1):
                s, e = grp[0], grp[-1]
                k = int(np.argmax(ratios[:, s:e + 1].max(axis=1)))
                kind = ["steep uphill", "steep downhill", "steep sideslope"][k]
                val = [g[s:e + 1].max(), (-g[s:e + 1]).max(), cross[s:e + 1].max()][k]
                sections.append(dict(kind=kind, from_m=float(cum[s]), to_m=float(cum[e + 1]),
                                     length_m=float(cum[e + 1] - cum[s]), grade=float(val),
                                     rc=tuple(int(x) for x in p[s]),
                                     ratio=float(ratios[:, s:e + 1].max())))
        return dict(distance_m=float(cum[-1]), eta_min=eta_min,
                    ascent_m=float(dz[dz > 0].sum()), descent_m=float(-dz[dz < 0].sum()) + 0.0,
                    max_up=float(max(g.max(), 0)), max_down=float(max((-g).max(), 0)),
                    max_cross=float(cross.max()), hard_sections=sections)

    def _stream_crossings(self, path):
        """Number of separate stream/river-line stretches the route passes through."""
        wcls = self.grid.water
        if wcls is None or len(path) < 2:
            return 0
        on = np.array([wcls[r, c] == STREAM for r, c in path])
        return int(on[0]) + int(np.sum(on[1:] & ~on[:-1]))

    def _report(self, path, tier_idx, goal):
        cfg0 = self.tiers[0][1]
        m = self._analyze(path)
        name = self.tiers[tier_idx][0]
        sev = SEVERITY[min(tier_idx, len(SEVERITY) - 1)]
        straight = np.hypot(goal[0] - self.start[0], goal[1] - self.start[1]) * self.grid.cell
        detour = m["distance_m"] / max(straight, 1.0)

        w = []
        if tier_idx > 0:
            w.append(f"This destination can't be reached within comfortable limits "
                     f"({cfg0.max_up:.0%} up / {cfg0.max_down:.0%} down). The route needs the "
                     f"'{name}' setting and reaches {m['max_up']:.0%} uphill and {m['max_down']:.0%} downhill.")
        secs = m["hard_sections"]
        if secs:
            worst = max(secs, key=lambda s: s["ratio"])
            w.append(f"{len(secs)} section(s) exceed comfortable limits, {sum(s['length_m'] for s in secs):.0f} m "
                     f"in total. The worst is {worst['kind']} of about {worst['grade']:.0%}, "
                     f"{worst['from_m']:.0f} m into the route.")
        if sev == "severe":
            w.append("Very steep terrain for an injured hiker: take it slowly, and consider a closer, easier destination.")
        if sev == "extreme":
            w.append("Scrambling-level terrain. Not recommended while injured: consider staying put and "
                     "signaling for help, or choosing a different destination.")
        n_cross = self._stream_crossings(path)
        if n_cross:
            w.append(f"The route crosses {n_cross} stream or river channel(s). Crossings can be slow or "
                     f"dangerous, especially in high water; pick a safe spot to cross.")
        if detour > 1.8 and straight > 200:
            w.append(f"Terrain forces a detour: the route is {detour:.1f}x longer than the straight line.")
        if m["ascent_m"] > 300:
            w.append(f"Significant climb: {m['ascent_m']:.0f} m of ascent in total.")
        if m["eta_min"] > 120:
            w.append(f"Long trip: about {m['eta_min']:.0f} min at an injured pace.")

        return dict(severity=sev, tier=name, path=path, warnings=w, straight_m=float(straight),
                    detour=float(detour), goal=goal, **m)

    def _unreachable(self, goal):
        g_ = self.grid
        L = self._layer(0)
        reach = np.argwhere(np.isfinite(L["F"]))
        d = np.hypot(reach[:, 0] - goal[0], reach[:, 1] - goal[1]) * g_.cell
        k = int(np.argmin(d))
        approach = tuple(int(x) for x in reach[k])
        path = self._trace(L, approach) if approach != self.start else [self.start]

        # steepest terrain on the straight line between the closest point and the goal
        t = np.linspace(0, 1, 60)
        rr = np.clip(np.round(approach[0] + t * (goal[0] - approach[0])).astype(int), 0, g_.dem.shape[0] - 1)
        cc = np.clip(np.round(approach[1] + t * (goal[1] - approach[1])).astype(int), 0, g_.dem.shape[1] - 1)
        barrier = float(self._slope[rr, cc].max())

        w = []
        if g_.water is not None and g_.water[goal] == OPEN:
            w.append("The destination is in open water (lake or river).")
        elif g_.blocked[goal]:
            w.append("The destination is on blocked terrain (missing map data or an obstacle).")
        elif g_.water is not None and (g_.water[max(goal[0] - 5, 0):goal[0] + 6,
                                                max(goal[1] - 5, 0):goal[1] + 6] == OPEN).sum() > 20:
            w.append("The destination looks like an island or a spit surrounded by water, with no "
                     "dry route to it from where you are.")
        else:
            w.append(f"No walkable route exists, even allowing scrambling-level slopes: the destination "
                     f"is walled off by blocked terrain or slopes steeper than "
                     f"{self.tiers[-1][1].max_up:.0%}.")
        w.append(f"The closest point you can safely reach is {d[k]:.0f} m from the destination "
                 f"and {g_.dem[goal] - g_.dem[approach]:+.0f} m in elevation. "
                 f"Terrain between there and the destination reaches about {barrier:.0%} gradient.")
        res = dict(severity="unreachable", tier=self.tiers[0][0], path=path, warnings=w,
                   goal=goal, approach=approach, short_by_m=float(d[k]),
                   elevation_gap_m=float(g_.dem[goal] - g_.dem[approach]), barrier_grade=barrier)
        res.update(self._analyze(path) if len(path) > 1 else
                   dict(distance_m=0.0, eta_min=0.0, ascent_m=0.0, descent_m=0.0, hard_sections=[]))
        return res


# --------------------------------------------------------------------------
# Map + click-to-pick (for testing / demo)
# --------------------------------------------------------------------------

def _xy(grid, rc):
    rc = np.asarray(rc, float).reshape(-1, 2)
    x0, y0 = grid.origin_xy
    return x0 + (rc[:, 1] + 0.5) * grid.cell, y0 - (rc[:, 0] + 0.5) * grid.cell


def draw_plan(ax, planner, res):
    """Draw one plan result; returns the artists so they can be removed next time."""
    grid, art = planner.grid, []
    color = COLORS[res["severity"]]
    if len(res["path"]) > 1:
        x, y = _xy(grid, res["path"])
        art += ax.plot(x, y, color=color, lw=3, zorder=5)
        for s in res.get("hard_sections", []):
            pp = np.array(res["path"])
            # overlay the offending stretch
            seg = [i for i, q in enumerate(res["path"])
                   if tuple(q) == tuple(s["rc"])]
            if seg:
                n = max(2, int(s["length_m"] / grid.cell) + 2)
                sx, sy = _xy(grid, pp[seg[0]:seg[0] + n])
                art += ax.plot(sx, sy, color="k", lw=5.5, alpha=0.35, zorder=4)
    gx, gy = _xy(grid, [res["goal"]]) if "goal" in res else (np.array([]), np.array([]))
    if res["severity"] == "unreachable":
        ax_, ay_ = _xy(grid, [res["approach"]])
        # dashed line across the gap that has no walkable route
        art += ax.plot([ax_[0], gx[0]], [ay_[0], gy[0]], color=color, lw=2, ls=(0, (4, 3)), zorder=4)
        art.append(ax.scatter(ax_, ay_, marker="X", s=150, color="white", edgecolor="k", zorder=6))
        # warning triangle at the unreachable destination
        art.append(ax.scatter(gx, gy, marker="^", s=650, color="#ffcc00", edgecolor="k",
                              linewidths=1.6, zorder=6))
        art.append(ax.annotate("!", (gx[0], gy[0]), xytext=(0, -2), textcoords="offset points",
                               ha="center", va="center", fontsize=13, fontweight="bold",
                               color="k", zorder=7))
    elif len(gx):
        art.append(ax.scatter(gx, gy, marker="*", s=300, color=color, edgecolor="k", zorder=6))
    return art


def _summary(res):
    if res["severity"] in ("invalid",):
        return "INVALID"
    s = f"{res['severity'].upper()}"
    if res.get("distance_m"):
        s += (f": {res['distance_m'] / 1000:.2f} km, ~{res['eta_min']:.0f} min, "
              f"+{res['ascent_m']:.0f} / -{res['descent_m']:.0f} m")
    return s


def _maximize(fig):
    """Best-effort window maximize (Qt / Tk). On the macOS backend press 'f' for fullscreen."""
    mng = fig.canvas.manager
    for attempt in (
        lambda: mng.window.showMaximized(),            # Qt
        lambda: mng.window.state("zoomed"),            # Tk on Windows
        lambda: mng.resize(*mng.window.maxsize()),     # Tk on mac/linux
    ):
        try:
            attempt()
            return
        except Exception:
            continue


def pick_destination(planner: Planner, radius_m: float | None = None, show: bool = True,
                     water=None):
    """
    Interactive map. Opens on the WHOLE map, as large as the window allows.

      left-click ........ choose destination
      left-drag ......... pan
      scroll wheel ...... zoom at the cursor
      r / h ............. whole map        z ... zoom to start (1.5 km)
      x ................. clear route      f ... fullscreen
    The toolbar's zoom/pan tools still work (clicks are ignored while one is active).
    radius_m: start zoomed to this radius around the start instead of the whole map.
    water:    KNOWN water to draw blue: a bool mask or the class raster from water.py. Defaults
              to grid.water (set by load_water). Nothing else on the map is blue.
    """
    import warnings
    import matplotlib.pyplot as plt
    from plotting import draw_water, shaded_relief

    warnings.filterwarnings("ignore", message="Ignoring fixed .* limits")

    g = planner.grid
    if water is None:
        water = g.water                                               # from water.load_water()
    H, W = g.dem.shape
    x0, y0 = g.origin_xy
    extent = (x0, x0 + W * g.cell, y0 - H * g.cell, y0)               # left, right, bottom, top
    sx, sy = (float(v[0]) for v in _xy(g, [planner.start]))

    fig = plt.figure(figsize=(16, 10))
    ax = fig.add_axes([0.075, 0.07, 0.915, 0.90])                    # map fills the window

    ax.imshow(shaded_relief(g.dem, g.cell), extent=extent, origin="upper", zorder=1)
    draw_water(ax, water, extent)

    relief = float(g.dem.max() - g.dem.min())
    step = 20 if relief < 800 else 40 if relief < 2000 else 100       # contour interval (m)
    lo, hi = np.floor(g.dem.min() / step) * step, np.ceil(g.dem.max() / step) * step
    levels = np.arange(lo, hi + step, step)
    ax.contour(g.dem, levels=levels, extent=extent, origin="upper",
               colors="k", linewidths=0.3, alpha=0.35, zorder=2)
    ax.contour(g.dem, levels=levels[::5], extent=extent, origin="upper",
               colors="k", linewidths=0.8, alpha=0.55, zorder=2)

    ax.scatter([sx], [sy], c="lime", s=190, edgecolor="k", zorder=7)
    ax.set_xlabel("UTM easting (m)")
    ax.set_ylabel("UTM northing (m)")
    ax.set_title("Click a destination")
    ax.text(0.01, 0.99, "click: destination   drag: pan   scroll: zoom   "
            "r: whole map   z: zoom to start   x: clear   f: fullscreen",
            transform=ax.transAxes, va="top", fontsize=9,
            bbox=dict(facecolor="white", alpha=0.75, edgecolor="none"), zorder=10)
    note = ax.text(0.01, 0.01, "", transform=ax.transAxes, va="bottom", fontsize=9,
                   family="monospace", zorder=10,
                   bbox=dict(facecolor="white", alpha=0.85, edgecolor="none"))

    def set_view(cx=None, cy=None, half=None):
        if half is None:
            ax.set_xlim(extent[0], extent[1])
            ax.set_ylim(extent[2], extent[3])
        else:
            ax.set_xlim(cx - half, cx + half)
            ax.set_ylim(cy - half, cy + half)
        ax.set_aspect("equal", adjustable="datalim")

    set_view(sx, sy, radius_m) if radius_m else set_view()
    state = {"art": [], "press": None, "moved": False}

    def tool_active():
        return bool(str(getattr(getattr(fig.canvas, "toolbar", None), "mode", "")))

    def choose(x, y):
        if not (extent[0] <= x <= extent[1] and extent[2] <= y <= extent[3]):
            return
        ax.set_title("Planning route...")
        fig.canvas.draw_idle()
        fig.canvas.flush_events()
        res = planner.plan(g.xy_to_rc(x, y))
        for a_ in state["art"]:
            a_.remove()
        state["art"] = draw_plan(ax, planner, res)
        ax.set_title(_summary(res))
        note.set_text("\n".join(textwrap.fill("- " + w, 130) for w in res["warnings"]) or "No warnings.")
        print(_summary(res))
        for w in res["warnings"]:
            print("  -", w)
        fig.canvas.draw_idle()

    def on_press(ev):
        if ev.inaxes is ax:
            state["press"] = (ev.x, ev.y, ax.get_xlim(), ax.get_ylim())
            state["moved"] = False

    def on_motion(ev):
        p = state["press"]
        if p is None or tool_active():
            return
        if abs(ev.x - p[0]) + abs(ev.y - p[1]) > 5:
            state["moved"] = True
        if not state["moved"]:
            return
        (xa, xb), (ya, yb) = p[2], p[3]
        dx = (ev.x - p[0]) * (xb - xa) / ax.bbox.width
        dy = (ev.y - p[1]) * (yb - ya) / ax.bbox.height
        ax.set_xlim(xa - dx, xb - dx)
        ax.set_ylim(ya - dy, yb - dy)
        fig.canvas.draw_idle()

    def on_release(ev):
        p, moved = state["press"], state["moved"]
        state["press"] = None
        if p is None or moved or tool_active() or ev.button != 1 or ev.inaxes is not ax:
            return
        choose(ev.xdata, ev.ydata)

    def on_scroll(ev):
        if ev.inaxes is not ax:
            return
        k = 1 / 1.3 if ev.button == "up" else 1.3
        xa, xb = ax.get_xlim()
        ya, yb = ax.get_ylim()
        ax.set_xlim(ev.xdata - (ev.xdata - xa) * k, ev.xdata + (xb - ev.xdata) * k)
        ax.set_ylim(ev.ydata - (ev.ydata - ya) * k, ev.ydata + (yb - ev.ydata) * k)
        fig.canvas.draw_idle()

    def on_key(ev):
        if ev.key in ("r", "h"):
            set_view()
        elif ev.key == "z":
            set_view(sx, sy, 1500.0)
        elif ev.key == "x":
            for a_ in state["art"]:
                a_.remove()
            state["art"] = []
            note.set_text("")
            ax.set_title("Click a destination")
        else:
            return
        fig.canvas.draw_idle()

    for name, fn in (("button_press_event", on_press), ("motion_notify_event", on_motion),
                     ("button_release_event", on_release), ("scroll_event", on_scroll),
                     ("key_press_event", on_key)):
        fig.canvas.mpl_connect(name, fn)

    if show:
        _maximize(fig)
        plt.show()
    return fig


# --------------------------------------------------------------------------
if __name__ == "__main__":
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # synthetic: valley, a steep N-S ridge across the whole map, and a lake
    R = C = 220
    yy, xx = np.mgrid[0:R, 0:C]
    dem = 0.15 * yy + 32 * np.exp(-((xx - 110) / 8.0) ** 2)
    blocked = np.hypot(yy - 150, xx - 170) < 8
    grid = Grid(dem=dem, cell=10.0, origin_xy=(500000.0, 4850000.0), blocked=blocked)

    planner = Planner(grid, (110, 40))
    tests = {"easy, same side": (60, 70), "across the ridge": (110, 180),
             "in the lake": (150, 170), "off the map": (500, 500)}
    fig, ax = plt.subplots(figsize=(10, 8))
    ax.imshow(dem, extent=(500000, 502200, 4847800, 4850000), cmap="terrain")
    for name, goal in tests.items():
        res = planner.plan(goal)
        print(f"\n[{name}] -> {_summary(res)} (tier: {res.get('tier')})")
        for wmsg in res["warnings"]:
            print("   -", wmsg)
        draw_plan(ax, planner, res)
    sx, sy = _xy(grid, [planner.start])
    ax.scatter(sx, sy, c="lime", s=150, edgecolor="k", zorder=7)
    plt.savefig("/tmp/plan_demo.png", dpi=100)
