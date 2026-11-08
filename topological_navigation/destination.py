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

from hiker_router import (CostConfig, Grid, build_edge_costs, fmt_duration, forward_tree,
                          snow_extra, tobler_speed)
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

# Route "profiles": same terrain caps as the tier that connects the destination, different priorities.
# (label, change to CostConfig, extra multiplier on stream cells). A profile whose lever does not exist
# on this map (no snow loaded, no streams) is skipped.
SNOW_MIN_CM = 5.0          # depth counted as "snowy" in route metrics
PROFILES = {
    "balanced":  ("Balanced",             lambda c: c, 1.0),
    "fastest":   ("Fastest",              lambda c: replace(c, steep_penalty=0.0, cross_penalty=0.0,
                                                             rough_penalty=0.0), 1.0),
    "gentlest":  ("Gentlest on the body", lambda c: replace(c, steep_penalty=c.steep_penalty * 3,
                                                             cross_penalty=c.cross_penalty * 3,
                                                             rough_penalty=c.rough_penalty * 3), 1.0),
    "least snow": ("Least snow",          lambda c: replace(c, snow_weight=c.snow_weight * 6), 1.0),
    "fewest crossings": ("Fewest stream crossings", lambda c: c, 6.0),
}
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
        self._gy, self._gx = np.gradient(grid.dem, grid.cell)
        self._slope = np.hypot(self._gy, self._gx)

    # ------------------------------------------------------------------ core
    def _blocked(self):
        """Cells nobody may walk on: grid.blocked plus ALL open water, whenever the water was loaded."""
        g = self.grid
        return g.blocked if g.water is None else (g.blocked | (g.water == OPEN))

    def _routing_grid(self, stream_mult: float = 1.0):
        g = self.grid
        cm = g.cost_mult
        if stream_mult != 1.0 and g.water is not None:
            base = np.ones(g.dem.shape) if cm is None else cm
            cm = base * np.where(g.water == STREAM, stream_mult, 1.0)
        return replace(g, blocked=self._blocked(), cost_mult=cm)

    def _sync(self):
        """Drop cached routes if the water, blocked cells or the snow changed since they were built."""
        w, s = self.grid.water, self.grid.snow_depth_cm
        sig = (None if w is None else (id(w), int((w == OPEN).sum())), int(self.grid.blocked.sum()),
               None if s is None else (id(s), float(s.sum()), self.grid.snowline_m))
        if sig != getattr(self, "_sig", None):
            self._layers = {}
            self.__dict__.pop("_edt", None)
            self._sig = sig

    def audit(self, path):
        """Number of path cells that lie in open water. Must be 0."""
        w = self.grid.water
        if w is None or not len(path):
            return 0
        p = np.asarray(path)
        return int((w[p[:, 0], p[:, 1]] == OPEN).sum())

    def profile_setup(self, i, profile="balanced"):
        """(routing grid, CostConfig) for tier i under a profile. Navigator uses the same pair."""
        _, change, stream_mult = PROFILES[profile]
        return self._routing_grid(stream_mult), change(self.tiers[i][1])

    def _layer(self, i, profile="balanced"):
        self._sync()
        key = (i, profile)
        if key not in self._layers:
            rg, cfg = self.profile_setup(i, profile)
            ec = build_edge_costs(rg, cfg)
            F, pred = forward_tree(rg, ec, self.start)
            self._layers[key] = dict(name=self.tiers[i][0], cfg=cfg, F=F, pred=pred)
        return self._layers[key]

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

    def plan(self, goal, snap_m: float | None = None, profile: str = "balanced"):
        """
        Route to `goal` (row, col). A goal on blocked cells (open water, missing data) is moved to
        the nearest walkable cell, however far away that is (snap_m=None), and the warning says so.
        Pass snap_m to refuse snapping farther than that many metres.
        """
        self._sync()
        R, C = self.grid.dem.shape
        goal = tuple(int(x) for x in goal)
        note = None
        if 0 <= goal[0] < R and 0 <= goal[1] < C and self._blocked()[goal]:
            snapped, d = self._nearest_walkable(goal)
            if snapped is not None and (snap_m is None or d <= snap_m):
                what = ("in open water (lake or river)"
                        if self.grid.water is not None and self.grid.water[goal] == OPEN
                        else "on blocked terrain (missing map data or an obstacle)")
                note = (f"That point is {what}, so the route goes to the nearest walkable "
                        f"point, {d:.0f} m away.")
                requested, goal = goal, snapped
        res = self._plan_cell(goal, profile)
        if note:
            res["warnings"].insert(0, note)
            res["requested_goal"] = requested
        res["water_cells"] = self.audit(res["path"])
        if res["water_cells"]:
            res["warnings"].insert(0, f"WARNING: this route passes through {res['water_cells']} open-water "
                                      f"cells, which should never happen. Please report it (the cells are "
                                      f"marked in magenta).")
        return res

    def _nearest_walkable(self, goal):
        if not hasattr(self, "_edt"):
            from scipy.ndimage import distance_transform_edt
            self._edt = distance_transform_edt(self._blocked(), return_indices=True)
        dist, (ir, ic) = self._edt
        d = float(dist[goal]) * self.grid.cell
        snapped = (int(ir[goal]), int(ic[goal]))
        return (None, d) if self._blocked()[snapped] else (snapped, d)

    def _plan_cell(self, goal, profile="balanced"):
        R, C = self.grid.dem.shape
        if not (0 <= goal[0] < R and 0 <= goal[1] < C):
            return dict(severity="invalid", path=[self.start], warnings=["That destination is outside the map."])
        if goal == self.start:
            return dict(severity="ok", path=[self.start], warnings=["You are already at that location."],
                        tier="comfortable", eta_min=0.0, distance_m=0.0, ascent_m=0.0,
                        descent_m=0.0, hard_sections=[])

        for i in range(len(self.tiers)):
            L = self._layer(i, profile)
            if np.isfinite(L["F"][goal]):
                return self._report(self._trace(L, goal), i, goal, profile)
        return self._unreachable(goal)

    def plan_latlon(self, lat, lon, crs):
        from pyproj import Transformer
        x, y = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(lon, lat)
        x0, y0 = self.grid.origin_xy
        c = int(np.floor((x - x0) / self.grid.cell))
        r = int(np.floor((y0 - y) / self.grid.cell))
        return self.plan((r, c))

    # ---------------------------------------------------------- alternatives
    def _profile_applies(self, profile):
        g = self.grid
        if profile == "least snow":
            return g.snow_depth_cm is not None and bool((g.snow_depth_cm >= SNOW_MIN_CM).any())
        if profile == "fewest crossings":
            return g.water is not None and bool((g.water == STREAM).any())
        return True

    @staticmethod
    def _overlap(a, b, grid, tol_cells=2):
        """Fraction of path a that lies within tol_cells of path b."""
        from scipy.ndimage import binary_dilation
        m = np.zeros(grid.dem.shape, bool)
        pb = np.asarray(b)
        m[pb[:, 0], pb[:, 1]] = True
        m = binary_dilation(m, iterations=tol_cells)
        pa = np.asarray(a)
        return float(m[pa[:, 0], pa[:, 1]].mean())

    ROLES = ["Primary", "Secondary", "Tertiary"]

    @staticmethod
    def _advantages(base, r):
        """How r beats the primary route, as {name: (score, text)}. Score is a fraction (bigger = better);
        safety-relevant items (stream crossings, snow) are weighted up."""
        adv = {}
        d = base["eta_min"] - r["eta_min"]
        adv["faster"] = (d / max(base["eta_min"], 1.0), f"{fmt_duration(d)} faster")
        d = base["distance_m"] - r["distance_m"]
        adv["shorter"] = (d / max(base["distance_m"], 1.0), f"{d / 1000:.2f} km shorter")
        d = base["effort_m"] - r["effort_m"]
        adv["less tiring"] = (d / max(base["effort_m"], 50.0),
                              f"less tiring ({base['ascent_m'] - r['ascent_m']:+.0f} m climb)")
        if base["snow_m"] > 0:
            d = base["snow_m"] - r["snow_m"]
            adv["less snow"] = (1.2 * d / base["snow_m"], f"{d:.0f} m less snow")
        if base["n_crossings"] > 0:
            d = base["n_crossings"] - r["n_crossings"]
            adv["fewer crossings"] = (1.5 * d / base["n_crossings"], f"{d} fewer stream crossing(s)")
        return adv

    def plan_alternatives(self, goal, n_alt=2, min_gain=0.05, same_if=0.92, max_slower=2.0, snap_m=None):
        """
        Up to 1 + n_alt routes to one destination: PRIMARY (balanced), then SECONDARY and TERTIARY, picked
        because each is clearly better than the primary at something that matters (time, distance,
        tiredness, snow, stream crossings), not just different. All use the gentlest tier that connects the
        destination, so none is steeper than the primary needs. Candidates that are nearly the same line as
        the primary are merged into it (its 'also' list). Fewer than 3 come back if nothing qualifies.

        Each result: role, label ('Secondary: fewer crossings'), advantage (text), profile, also, vs_balanced.
        """
        base = self.plan(goal, snap_m=snap_m, profile="balanced")
        base.update(role="Primary", label="Primary: balanced", advantage="", also=[], vs_balanced={})
        if base["severity"] in ("unreachable", "invalid") or len(base["path"]) < 2:
            return [base]
        i = [n for n, _ in self.tiers].index(base["tier"])
        goal = base["goal"]
        near = lambda p, q: (self._overlap(p, q, self.grid) >= same_if and self._overlap(q, p, self.grid) >= same_if)

        cands = []
        for prof in PROFILES:
            if prof == "balanced" or not self._profile_applies(prof):
                continue
            L = self._layer(i, prof)
            if not np.isfinite(L["F"][goal]):
                continue
            path = self._trace(L, goal)
            if near(path, base["path"]):
                base["also"].append(PROFILES[prof][0])
                continue
            if any(near(path, c["path"]) for c in cands):
                next(c for c in cands if near(path, c["path"]))["also"].append(PROFILES[prof][0])
                continue
            r = self._report(path, i, goal, prof)
            r["also"], r["water_cells"] = [], self.audit(path)
            adv = self._advantages(base, r)
            good = sorted((k for k in adv if adv[k][0] >= min_gain), key=lambda k: -adv[k][0])
            top = good[0] if good else max(adv, key=lambda k: adv[k][0])
            r["_score"], r["_top"] = adv[top][0], top
            r["_names"] = good[:2] or [top]                    # up to two real strengths, best first
            r["advantage"] = "; ".join(adv[k][1] for k in r["_names"])
            if r["_score"] >= min_gain and r["eta_min"] <= max_slower * base["eta_min"]:
                cands.append(r)

        chosen = []
        for _ in range(n_alt):
            pool = [c for c in cands if c not in chosen and not any(near(c["path"], k["path"]) for k in chosen)]
            if not pool:
                break
            fresh = [c for c in pool if c["_top"] not in {k["_top"] for k in chosen}]    # prefer a different strength
            pick = max(fresh or pool, key=lambda c: c["_score"])
            chosen.append(pick)
        out = [base]
        for role, r in zip(self.ROLES[1:], chosen):
            r["role"], r["label"] = role, f"{role}: {', '.join(r['_names'])}"
            out.append(r)
        for r in out:
            r["vs_balanced"] = dict(
                eta_min=r["eta_min"] - base["eta_min"], distance_m=r["distance_m"] - base["distance_m"],
                snow_m=r["snow_m"] - base["snow_m"], ascent_m=r["ascent_m"] - base["ascent_m"],
                crossings=r["n_crossings"] - base["n_crossings"])
        return out

    @staticmethod
    def compare_table(alts):
        """Fixed-width text table of the routes (for the picker's message box / printing)."""
        rows = [f"{'#':>2} {'route':<40}{'time':>10}{'dist':>9}{'climb':>7}{'snow':>13}{'streams':>8}  severity"]
        for k, r in enumerate(alts, 1):
            snow = f"{r['snow_m']:.0f}m/{r['snow_max_cm']:.0f}cm" if r.get("snow_m") else "-"
            rows.append(f"{k:>2} {r['label']:<40}{fmt_duration(r.get('eta_min', float('nan'))):>10}"
                        f"{r.get('distance_m', 0.0) / 1000:>6.2f} km{r.get('ascent_m', 0.0):>6.0f}m{snow:>13}"
                        f"{r.get('n_crossings', 0):>8}  {r['severity']}")
        for k, r in enumerate(alts, 1):
            if r.get("advantage"):
                rows.append(f"   {k}: {r['advantage']}")
        return rows

    def navigator(self, res, **kw):
        """A Navigator for this route (its profile and tier). Pass locked=True to engage the route lock."""
        from navigator import Navigator
        i = [n for n, _ in self.tiers].index(res["tier"])
        rg, cfg = self.profile_setup(i, res.get("profile", "balanced"))
        return Navigator(rg, cfg, res["goal"], res["path"], **kw)

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
        t_seg = dist / (tobler_speed(g) * cfg0.speed_factor)
        cum = np.concatenate([[0.0], np.cumsum(dist)])
        eta_dry = float(t_seg.sum() / 60)
        snow = dict(snow_m=0.0, snow_frac=0.0, snow_max_cm=0.0, snow_mean_cm=0.0, snow_delay_min=0.0,
                    snow_sections=[], above_snowline_m=0.0)
        if g_.snow_depth_cm is not None:
            d = g_.snow_depth_cm[b[:, 0], b[:, 1]].astype(float)
            t_seg = t_seg * (1.0 + snow_extra(d))            # realistic (weight 1) slowdown for the ETA
            on = d >= SNOW_MIN_CM
            snow.update(snow_m=float(dist[on].sum()), snow_frac=float(dist[on].sum() / max(dist.sum(), 1.0)),
                        snow_max_cm=float(d.max()), snow_mean_cm=float(d[on].mean()) if on.any() else 0.0,
                        snow_delay_min=float(t_seg.sum() / 60 - eta_dry))
            if on.any():
                for grp in np.split(np.flatnonzero(on), np.flatnonzero(np.diff(np.flatnonzero(on)) > 1) + 1):
                    snow["snow_sections"].append(dict(from_m=float(cum[grp[0]]),
                                                      length_m=float(dist[grp].sum()),
                                                      depth_cm=float(d[grp].max()),
                                                      rc=tuple(int(x) for x in p[grp[0]])))
            if g_.snowline_m is not None:
                snow["above_snowline_m"] = float(max(g_.dem[p[:, 0], p[:, 1]].max() - g_.snowline_m, 0.0))
        eta_min = float(t_seg.sum() / 60)

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
                    max_cross=float(cross.max()), hard_sections=sections,
                    effort_m=float(dz[dz > 0].sum() + 0.5 * -dz[dz < 0].sum()
                                   + 0.25 * sum(s_['length_m'] for s_ in sections)), **snow)

    def _stream_crossings(self, path):
        """Number of separate stream/river-line stretches the route passes through."""
        wcls = self.grid.water
        if wcls is None or len(path) < 2:
            return 0
        on = np.array([wcls[r, c] == STREAM for r, c in path])
        return int(on[0]) + int(np.sum(on[1:] & ~on[:-1]))

    def _report(self, path, tier_idx, goal, profile="balanced"):
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
        wx = self.grid.weather or {}
        if n_cross:
            w.append(f"The route crosses {n_cross} stream or river channel(s). Crossings can be slow or "
                     f"dangerous, especially in high water; pick a safe spot to cross.")
            if wx.get("rain_24h_mm", 0) >= 15:
                w.append(f"About {wx['rain_24h_mm']:.0f} mm of rain in the last 24 h: streams are likely "
                         f"running higher than normal.")
        snow_flag = False
        if m["snow_m"] > 0:
            snow_flag = m["snow_frac"] >= 0.25 and m["snow_max_cm"] >= 15
            w.append(f"{m['snow_m']:.0f} m of the route ({m['snow_frac']:.0%}) is on snow of {SNOW_MIN_CM:.0f} cm "
                     f"or more, up to about {m['snow_max_cm']:.0f} cm deep. Snow adds roughly "
                     f"{fmt_duration(m['snow_delay_min'])} to the trip and makes slopes slipperier.")
            if m["above_snowline_m"] > 0 and snow_flag and m["snow_frac"] < 0.9:
                w.append("Consider the 'least snow' alternative (press 'a' in the picker) if you can "
                         "accept a longer route to stay lower and drier.")
        if wx.get("stale") or wx.get("source") == "cache":
            w.append(f"Weather data is {wx.get('age_h', 0):.0f} h old"
                     f"{' (from the cache, no signal)' if wx.get('source') == 'cache' else ''}: "
                     f"conditions may have changed.")
        if sev == "ok" and snow_flag:
            sev = "caution"
        if detour > 1.8 and straight > 200:
            w.append(f"Terrain forces a detour: the route is {detour:.1f}x longer than the straight line.")
        if m["ascent_m"] > 300:
            w.append(f"Significant climb: {m['ascent_m']:.0f} m of ascent in total.")
        if m["eta_min"] > 120:
            w.append(f"Long trip: about {fmt_duration(m['eta_min'])} at an injured pace.")

        return dict(severity=sev, tier=name, path=path, warnings=w, straight_m=float(straight),
                    detour=float(detour), goal=goal, profile=profile, n_crossings=n_cross, **m)

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
        elif self._blocked()[goal]:
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
    if grid.water is not None and len(res["path"]) > 1:
        pp = np.asarray(res["path"])
        wet = grid.water[pp[:, 0], pp[:, 1]] == OPEN
        if wet.any():
            wx, wy = _xy(grid, pp[wet])
            art.append(ax.scatter(wx, wy, c="magenta", s=22, zorder=9))
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
        s += (f": {res['distance_m'] / 1000:.2f} km, ~{fmt_duration(res['eta_min'])}, "
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


ALT_COLORS = ["#984ea3", "#ff7f00", "#f781bf", "#222222"]    # none blue; the chosen route uses severity colour
SNOW_RGB = (0.96, 0.93, 1.0)                                 # white with a lilac tint: blue is for water


def _snow_rgba(depth):
    """Snow overlay: nearly transparent at ~1 cm, fairly opaque by ~25 cm."""
    a = np.zeros(depth.shape + (4,), np.float32)
    a[..., :3] = SNOW_RGB
    a[..., 3] = 0.38 * np.clip((depth - 1.0) / 40.0, 0, 1)      # capped: altitude colours stay readable
    return a


def _remove_contour(cs):
    try:
        cs.remove()
    except Exception:                       # older matplotlib
        for c in getattr(cs, "collections", []):
            c.remove()


def pick_destination(planner: Planner, radius_m: float | None = None, show: bool = True,
                     water=None, conditions=None, t=None):
    """
    Interactive map. Opens on the WHOLE map, as large as the window allows.

      left-click ........ choose destination
      left-drag ......... pan
      scroll wheel ...... zoom at the cursor
      r / h ............. whole map        z ... zoom to start (1.5 km)
      x ................. clear route      f ... fullscreen
      a ................. show up to 3 routes (primary / secondary / tertiary) with a trade-off table
      1 - 3 ............. highlight that route
      l ................. LOCK the highlighted route (engage); press l again to disengage. While locked
                          nothing can change the route: no new destination, no automatic re-routing.
      right-click ....... (locked) simulate a position fix; off route you get a warning + suggestion only
      g ................. (locked) re-route from the last fix: an explicit, deliberate change
      y / n ............. accept / decline a route offered because the weather changed
      s ................. snow display: light tint + depth lines -> lines only -> off (altitude colours
                          alone, exactly as before weather)
      [  ] .............. move the weather time back / forward 3 hours (routes re-plan)
    The toolbar's zoom/pan tools still work (clicks are ignored while one is active).
    radius_m:   start zoomed to this radius around the start instead of the whole map.
    water:      KNOWN water to draw blue: a bool mask or the class raster from water.py. Defaults
                to grid.water (set by load_water). Nothing else on the map is blue.
    conditions: weather.Conditions (live, cached or simulated) or None. Snow is drawn white/lilac.
    t:          unix time to show the weather for (default now).
    """
    import time as _time
    import warnings
    import matplotlib.pyplot as plt
    from plotting import draw_water, shaded_relief

    warnings.filterwarnings("ignore", message="Ignoring fixed .* limits")

    g = planner.grid
    if water is None:
        water = g.water                                               # from water.load_water()
    cond = conditions
    if cond is not None:
        from weather import apply_conditions, describe
        t_view = _time.time() if t is None else float(t)
        if g.weather is None or g.weather.get("t") != t_view:
            apply_conditions(g, cond, t=t_view)
    else:
        describe = lambda grid: "Weather: off (terrain only)"
        t_view = _time.time()
    H, W = g.dem.shape
    x0, y0 = g.origin_xy
    extent = (x0, x0 + W * g.cell, y0 - H * g.cell, y0)               # left, right, bottom, top
    sx, sy = (float(v[0]) for v in _xy(g, [planner.start]))

    fig = plt.figure(figsize=(16, 10))
    ax = fig.add_axes([0.075, 0.07, 0.915, 0.90])                    # map fills the window

    ax.imshow(shaded_relief(g.dem, g.cell), extent=extent, origin="upper", zorder=1)
    snow_im = None
    if g.snow_depth_cm is not None:
        snow_im = ax.imshow(_snow_rgba(g.snow_depth_cm), extent=extent, origin="upper", zorder=1.3)
    draw_water(ax, water, extent)
    # snow display modes, cycled with 's': light tint + lines -> lines only -> off
    snowline = {"cs": None, "depth_cs": None, "mode": 0}
    SNOW_MODES = ["tint + depth lines", "depth lines only", "off"]

    def draw_snowline():
        for k in ("cs", "depth_cs"):
            if snowline[k] is not None:
                _remove_contour(snowline[k])
                snowline[k] = None
        if snowline["mode"] == 2:
            return
        sl = g.snowline_m
        if sl is not None and g.dem.min() < sl < g.dem.max():
            snowline["cs"] = ax.contour(g.dem, levels=[sl], extent=extent, origin="upper",
                                        colors="#7a4fb3", linestyles="--", linewidths=1.8, zorder=3)
        d = g.snow_depth_cm
        if d is not None:
            lv = [v for v in (5, 15, 30, 60) if d.min() < v < d.max()]
            if lv:
                xs = np.linspace(extent[0], extent[1], d.shape[1])
                ys = np.linspace(extent[3], extent[2], d.shape[0])
                snowline["depth_cs"] = ax.contour(xs, ys, d, levels=lv, colors="#8e6bbf",
                                                  linewidths=[0.6, 0.9, 1.2, 1.6][:len(lv)],
                                                  alpha=0.9, zorder=2.5)
    draw_snowline()

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
    ax.text(0.01, 0.99, "click: destination   drag: pan   scroll: zoom   r: whole map   z: zoom to start   "
            "x: clear   a: 3 routes (1-3)   l: lock route   s: snow   [ ]: weather time   f: fullscreen",
            transform=ax.transAxes, va="top", fontsize=9,
            bbox=dict(facecolor="white", alpha=0.75, edgecolor="none"), zorder=10)
    wx_text = ax.text(0.01, 0.962, describe(g), transform=ax.transAxes, va="top", fontsize=9,
                      color="#4b2e83", bbox=dict(facecolor="white", alpha=0.75, edgecolor="none"), zorder=10)
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
    state = {"art": [], "press": None, "moved": False, "goal": None, "alts": None, "sel": 0,
             "res": None, "nav": None, "lres": None, "fix": None, "offer": None, "sug": None}

    def locked():
        return state["nav"] is not None and state["nav"].locked

    def say(msg):
        note.set_text(msg)
        fig.canvas.draw_idle()

    def tier_of(r):
        return [n for n, _ in planner.tiers].index(r["tier"])

    def tool_active():
        return bool(str(getattr(getattr(fig.canvas, "toolbar", None), "mode", "")))

    def clear_art():
        for a_ in state["art"]:
            try:
                a_.remove()
            except Exception:
                pass
        state["art"] = []

    def busy(msg):
        ax.set_title(msg)
        fig.canvas.draw_idle()
        fig.canvas.flush_events()

    def plan_rc(rc):
        if locked():
            return say("ROUTE LOCKED: press 'l' to disengage before choosing a new destination.")
        busy("Planning route...")
        state["goal"], state["alts"] = rc, None
        res = planner.plan(rc)
        state["res"] = res
        clear_art()
        state["art"] = draw_plan(ax, planner, res)
        ax.set_title(_summary(res))
        note.set_text("\n".join(textwrap.fill("- " + w, 130) for w in res["warnings"]) or "No warnings.")
        print(_summary(res))
        for w in res["warnings"]:
            print("  -", w)
        fig.canvas.draw_idle()

    def choose(x, y):
        if not (extent[0] <= x <= extent[1] and extent[2] <= y <= extent[3]):
            return
        plan_rc(g.xy_to_rc(x, y))

    def show_alts(keep=False):
        if locked():
            return say("ROUTE LOCKED: press 'l' to disengage before comparing routes.")
        if state["goal"] is None:
            return say("Click a destination first, then press 'a'.")
        busy("Computing alternative routes (a few seconds)...")
        prev_idx = state["sel"]
        prev = state["alts"][state["sel"]] if (keep and state["alts"]) else None
        state["alts"] = planner.plan_alternatives(state["goal"])
        state["sel"] = 0
        if prev is not None:                       # keep the route the user had highlighted, if it still exists
            same = [k for k, r in enumerate(state["alts"]) if r.get("profile") == prev.get("profile")]
            state["sel"] = same[0] if same else min(prev_idx, len(state["alts"]) - 1)
        redraw_alts()

    def redraw_alts():
        alts, sel = state["alts"], state["sel"]
        clear_art()
        for k, r in enumerate(alts):
            if k == sel or len(r["path"]) < 2:
                state["art"] += draw_plan(ax, planner, r)
            else:
                x, y = _xy(g, r["path"])
                state["art"] += ax.plot(x, y, color=ALT_COLORS[(k - 1) % len(ALT_COLORS)], lw=2.2,
                                        ls=(0, (5, 3)), zorder=4.5)
            if len(r["path"]) > 1:
                mx, my = _xy(g, [r["path"][len(r["path"]) // 2]])
                state["art"].append(ax.annotate(str(k + 1), (mx[0], my[0]), ha="center", va="center",
                                                fontsize=10, fontweight="bold", zorder=8,
                                                bbox=dict(boxstyle="circle,pad=0.25", fc="white", ec="k")))
        r = alts[sel]
        ax.set_title(f"[{sel + 1}/{len(alts)}] {r['label']}: {_summary(r)}")
        lines = planner.compare_table(alts)
        if len(alts) == 1:
            lines.append("(no alternative is clearly better at anything that matters here)")
        lines.append("Press l to lock the highlighted route.")
        also = [f"route {k + 1} ({r2['role']}) is also the {', '.join(r2['also']).lower()} route"
                for k, r2 in enumerate(alts) if r2["also"]]
        lines += also
        lines.append("")
        lines += [textwrap.fill("- " + w_, 130) for w_ in r["warnings"]]
        note.set_text("\n".join(lines))
        fig.canvas.draw_idle()

    def draw_locked(extra=""):
        r, nav = state["lres"], state["nav"]
        clear_art()
        x, y = _xy(g, r["path"])
        state["art"] += ax.plot(x, y, color="k", lw=8, alpha=0.35, zorder=4.2)       # the "engaged" halo
        state["art"] += draw_plan(ax, planner, r)
        if state["fix"] is not None:
            fx, fy = _xy(g, [state["fix"]])
            state["art"].append(ax.scatter(fx, fy, marker="o", s=110, color="white", edgecolor="k",
                                           linewidths=2, zorder=8))
        if state["sug"]:
            sx_, sy_ = _xy(g, state["sug"])
            state["art"] += ax.plot(sx_, sy_, color="#ff7f00", lw=2.2, ls=(0, (5, 3)), zorder=4.6)
        ax.set_title(f"LOCKED - {r['label']}: {_summary(r)}")
        lines = ["ROUTE LOCKED: it will not change unless you re-route (g), accept an offer (y) or disengage (l).",
                 "right-click: simulate a position fix"]
        if extra:
            lines += ["", textwrap.fill(extra, 130)]
        lines += [""] + [textwrap.fill("- " + w_, 130) for w_ in r["warnings"]]
        note.set_text("\n".join(lines))
        fig.canvas.draw_idle()

    def relabel(new, old):
        new.update(label=old["label"], role=old.get("role", "Primary"), advantage=old.get("advantage", ""),
                   also=old.get("also", []))
        return new

    def lock_toggle():
        if locked():
            r = state["lres"]
            state["nav"] = state["lres"] = state["fix"] = state["sug"] = state["offer"] = None
            clear_art()
            state["art"] = draw_plan(ax, planner, r)
            state["res"] = r
            ax.set_title(f"Unlocked - {r['label']}: {_summary(r)}")
            return say("Route disengaged. You can choose a new destination or compare routes again.")
        r = state["alts"][state["sel"]] if state["alts"] else state["res"]
        if r is None or len(r["path"]) < 2 or r["severity"] in ("unreachable", "invalid"):
            return say("Choose a reachable destination first, then press 'l' to lock a route.")
        r.setdefault("role", "Primary")
        r.setdefault("label", "Primary: balanced")
        r.setdefault("also", [])
        r.setdefault("advantage", "")
        state["nav"] = planner.navigator(r, locked=True)
        state["lres"], state["alts"], state["fix"], state["sug"] = r, None, None, None
        draw_locked()

    def simulate_fix(rc):
        nav = state["nav"]
        ev = nav.update(rc, sigma_m=15.0)
        state["fix"] = tuple(int(v) for v in rc)
        state["sug"] = ev.get("suggested_path") if ev["status"] == "off_route_locked" else None
        bits = [ev["message"]]
        if ev.get("bearing") is not None:
            bits.append(f"Next leg: bearing {ev['bearing']:.0f} deg true for {ev['leg_m']:.0f} m. "
                        f"Time left: {fmt_duration(ev['eta_min'])}.")
        if state["sug"]:
            bits.append(f"Suggested new route (dashed orange): {fmt_duration(ev['suggested_eta_min'])} left. "
                        f"Press g to switch to it; otherwise your route stays as it is.")
        draw_locked(" ".join(bits))

    def reroute_now():
        if not locked() or state["fix"] is None:
            return say("Nothing to re-route: lock a route and give a position fix first (right-click).")
        nav, old = state["nav"], state["lres"]
        ev = nav.reroute(state["fix"])
        if ev["status"] == "goal_unreachable" or len(nav.path) < 2:
            return draw_locked(ev["message"])
        state["lres"] = relabel(planner._report(nav.path, tier_of(old), old["goal"],
                                                old.get("profile", "balanced")), old)
        state["lres"]["label"] = old["label"] + " (re-routed)"
        state["sug"] = None
        draw_locked(ev["message"])

    def conditions_changed():
        """Weather moved while a route is locked: re-analyse it, and OFFER (never apply) a better one."""
        nav, old = state["nav"], state["lres"]
        prof = old.get("profile", "balanced")
        fix = state["fix"] or nav.path[min(nav.progress, len(nav.path) - 1)]
        off = nav.refresh_conditions(planner.profile_setup(tier_of(old), prof)[0], fix)
        state["offer"] = off
        state["lres"] = relabel(planner._report(old["path"], tier_of(old), old["goal"], prof), old)
        if off:
            if off.get("path"):
                state["sug"] = off["path"]
            draw_locked(f"{off['message']} Offered route: dashed orange, {fmt_duration(off['new_eta_min'])}. "
                        f"y: accept   n: keep my route.")
        else:
            state["sug"] = None
            draw_locked("Conditions updated. Your route is unchanged.")

    def answer_offer(accept):
        nav, old, off = state["nav"], state["lres"], state["offer"]
        if not locked() or not off:
            return say("No route offer is pending.")
        if accept:
            nav.accept_offer()
            state["lres"] = relabel(planner._report(nav.path, tier_of(old), old["goal"],
                                                    old.get("profile", "balanced")), old)
            state["lres"]["label"] = old["label"] + " (updated)"
        else:
            nav.decline_offer()
        state["offer"] = state["sug"] = None
        draw_locked("Offer accepted." if accept else "Kept your route.")

    def refresh_weather_view():
        if snow_im is not None:
            snow_im.set_data(_snow_rgba(g.snow_depth_cm))
            snow_im.set_visible(snowline["mode"] == 0)
        draw_snowline()
        wx_text.set_text(describe(g))

    def shift_time(hours):
        if cond is None:
            note.set_text("No weather loaded: pass conditions=... to pick_destination.")
            fig.canvas.draw_idle()
            return
        nonlocal t_view
        t_view += hours * 3600
        apply_conditions(g, cond, t=t_view)
        refresh_weather_view()
        if state["nav"] is not None:                 # a route is engaged: NEVER re-plan or re-select anything
            try:
                conditions_changed()
            except Exception as e:                   # show the failure, keep the locked route on screen
                import traceback
                traceback.print_exc()
                draw_locked(f"Could not re-check the locked route against the new weather ({type(e).__name__}: "
                            f"{e}). Your route is unchanged.")
        elif state["alts"] is not None:
            show_alts(keep=True)
        elif state["goal"] is not None:
            plan_rc(state["goal"])
        else:
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
        if p is None or moved or tool_active() or ev.inaxes is not ax:
            return
        if ev.button == 3 and locked():
            return simulate_fix(g.xy_to_rc(ev.xdata, ev.ydata))
        if ev.button != 1:
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
        elif ev.key == "f":
            try:
                fig.canvas.manager.full_screen_toggle()
            except Exception:
                pass
            return
        elif ev.key == "x":
            if locked():
                return say("ROUTE LOCKED: press 'l' to disengage first.")
            clear_art()
            state["goal"] = state["alts"] = None
            note.set_text("")
            ax.set_title("Click a destination")
        elif ev.key == "a":
            show_alts()
            return
        elif ev.key == "l":
            lock_toggle()
            return
        elif ev.key == "g":
            reroute_now()
            return
        elif ev.key in ("y", "n"):
            answer_offer(ev.key == "y")
            return
        elif ev.key in ("1", "2", "3"):
            if locked():
                return say("ROUTE LOCKED: press 'l' to disengage before switching routes.")
            if state["alts"] and int(ev.key) <= len(state["alts"]):
                state["sel"] = int(ev.key) - 1
                redraw_alts()
            return
        elif ev.key == "s":
            snowline["mode"] = (snowline["mode"] + 1) % 3
            refresh_weather_view()
            note.set_text(f"Snow display: {SNOW_MODES[snowline['mode']]} (press s to change)")
        elif ev.key in ("[", "]"):
            shift_time(-3 if ev.key == "[" else 3)
            return
        else:
            return
        fig.canvas.draw_idle()

    try:                                             # our keys replace matplotlib's default shortcuts
        fig.canvas.mpl_disconnect(fig.canvas.manager.key_press_handler_id)
    except Exception:
        pass

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
