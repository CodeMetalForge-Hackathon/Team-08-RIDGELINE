"""
navigator.py - follow a route, and re-route as the hiker moves or the conditions change.

    nav = planner.navigator(res)                 # res = a planner.plan(...) result
    ev = nav.update(fix_rc, sigma_m=15)          # every position fix
    ev = nav.refresh_conditions(new_routing_grid, fix_rc)   # when new weather arrives
    nav.accept_offer()  /  nav.decline_offer()

Why re-routing is cheap
  One BACKWARD Dijkstra from the goal gives T[r, c] = best cost to the goal from EVERY cell. A re-route
  from any new position is just "walk downhill on T": no new search. A search only happens again if
  the world changes (new snow, new water), because then T itself changes.

ROUTE LOCK (locked=True, what the picker uses once you commit to a route)
  A locked route is NEVER changed by the app. Not when you drift off it, not when the weather changes,
  not when a fix is noisy. Off route you get a warning, the heading back to the route and a SUGGESTED
  new route that is not applied; only reroute(fix) (the user pressing re-route) or unlock() changes
  anything. Condition changes only ever create offers, locked or not.

Rules (so it never gives whiplash advice)
  * Off route means: farther from the route than  off_route_m + 2 * sigma_m  (a noisy fix does not
    count as leaving the route), confirmed by `confirm_fixes` fixes in a row, or by one fix that is
    far beyond the threshold. Until confirmed the instruction stays "continue along the route".
  * A new route after leaving the old one is applied (the old one is no longer where you are), and
    the message says whether it rejoins the original route.
  * A change in CONDITIONS never swaps the route silently. It produces an OFFER only if the current
    route became impossible, or the new best route saves at least switch_frac of the remaining
    time AND switch_min_s seconds. Declining keeps your route (and updates the ETA).
  * If the goal can no longer be reached, you get the closest reachable point and a clear message.
"""
from __future__ import annotations

import numpy as np

from hiker_router import (bearing_deg, build_edge_costs, cost_to_go, descend, fmt_duration,
                          forward_tree, next_leg)


def _tail_cost(path, ec_map):
    """Cost of walking `path` under an edge-cost set; inf if any step is no longer allowed."""
    tot = 0.0
    for a, b in zip(path[:-1], path[1:]):
        off = (b[0] - a[0], b[1] - a[1])
        c = ec_map[off][a] if off in ec_map else np.inf
        tot += c
        if not np.isfinite(c):
            return np.inf
    return tot


class Navigator:
    def __init__(self, grid, cfg, goal, path, off_route_m=30.0, confirm_fixes=2,
                 switch_frac=0.10, switch_min_s=300.0, leg_m=150.0, locked=False):
        self.grid, self.cfg = grid, cfg
        self.goal = tuple(int(x) for x in goal)
        self.path = [tuple(int(x) for x in p) for p in path]
        self.off_route_m, self.confirm_fixes = off_route_m, confirm_fixes
        self.switch_frac, self.switch_min_s, self.leg_m = switch_frac, switch_min_s, leg_m
        self.progress, self._strikes, self.offer, self._declined_gain = 0, 0, None, 0.0
        self.locked = bool(locked)
        self._rebuild()

    # ---------------------------------------------------------------- lock
    def lock(self):
        """Engage: the route can no longer change except by an explicit reroute() / accept_offer()."""
        self.locked = True

    def unlock(self):
        """Disengage (the user's deliberate action). Afterwards update() may re-route automatically."""
        self.locked = False

    # ---------------------------------------------------------------- field
    def _rebuild(self, grid=None):
        if grid is not None:
            self.grid = grid
        self.ec = build_edge_costs(self.grid, self.cfg)
        self.ec_map = dict(self.ec)
        self.T = cost_to_go(self.grid, self.ec, self.goal)          # the ONLY search

    def _snap(self, rc):
        """A fix that lands on blocked ground (GPS-less error, water) is moved to the nearest walkable cell."""
        rc = tuple(int(x) for x in rc)
        R, C = self.grid.dem.shape
        rc = (min(max(rc[0], 0), R - 1), min(max(rc[1], 0), C - 1))
        if not self.grid.blocked[rc]:
            return rc
        from scipy.ndimage import distance_transform_edt
        _, (ir, ic) = distance_transform_edt(self.grid.blocked, return_indices=True)
        return (int(ir[rc]), int(ic[rc]))

    def eta_min(self, rc):
        t = self.T[self._snap(rc)]
        return float(t / 60) if np.isfinite(t) else float("inf")

    # ---------------------------------------------------------------- guidance
    def _guidance(self, rc, path):
        if len(path) < 2:
            return dict(bearing=None, leg_m=0.0)
        b, d, wp = next_leg(path, self.grid, self.leg_m)
        return dict(bearing=b, leg_m=d, waypoint=wp)

    def _nearest_on_route(self, rc):
        lo = max(0, self.progress - 5)
        seg = np.asarray(self.path[lo:])
        d = np.hypot(seg[:, 0] - rc[0], seg[:, 1] - rc[1])
        k = int(np.argmin(d))
        return lo + k, float(d[k] * self.grid.cell)

    # ---------------------------------------------------------------- position fixes
    def update(self, fix_rc, sigma_m=10.0):
        """
        One position fix. Returns an event dict:
          status   arrived | on_route | off_route_unconfirmed | rerouted | goal_unreachable
          message  text for the UI
          path     the route to follow now (list of (r, c))
          bearing, leg_m   next instruction (true-north bearing, metres to the next waypoint)
          eta_min, off_route_m
        """
        rc = self._snap(fix_rc)
        cell = self.grid.cell
        goal_d = float(np.hypot(rc[0] - self.goal[0], rc[1] - self.goal[1]) * cell)
        if goal_d <= max(2 * cell, sigma_m):
            return self._ev("arrived", "You have arrived at the destination.", rc, 0.0)

        k, off = self._nearest_on_route(rc)
        thr = self.off_route_m + 2.0 * sigma_m
        if off <= thr:
            self._strikes = 0
            self.progress = max(self.progress, k)
            return self._ev("on_route", "On route.", rc, off, path=self.path[self.progress:])

        self._strikes += 1
        if self._strikes < self.confirm_fixes and off <= 3 * thr:
            return self._ev(
                "off_route_unconfirmed",
                f"You may be {off:.0f} m off the route (position uncertainty {sigma_m:.0f} m). "
                f"Keep heading for the route; I will re-route if the next fix agrees.",
                rc, off, path=self.path[self.progress:])
        if self.locked:
            return self._off_route_locked(rc, off, sigma_m)
        return self._reroute(rc, off)

    def reroute(self, fix_rc):
        """EXPLICIT re-route from a position (the user asked for it). Works whether or not locked."""
        rc = self._snap(fix_rc)
        _, off = self._nearest_on_route(rc)
        return self._reroute(rc, off)

    def _off_route_locked(self, rc, off, sigma_m):
        """Locked: warn and advise, never change the route. Carries a suggested path the user may accept."""
        k, _ = self._nearest_on_route(rc)
        back = self.path[k:]
        ev = self._ev("off_route_locked",
                      f"You are {off:.0f} m off your locked route. The route has NOT been changed. Head back "
                      f"toward it (bearing below), or re-route from here.", rc, off,
                      path=[rc] + list(back))
        if np.isfinite(self.T[rc]):
            ev["suggested_path"] = descend(self.T, self.ec, rc, self.goal)
            ev["suggested_eta_min"] = self.eta_min(rc)
        return ev

    def _reroute(self, rc, off):
        self._strikes = 0
        if not np.isfinite(self.T[rc]):
            return self._unreachable(rc, off)
        new = descend(self.T, self.ec, rc, self.goal)
        if not new:
            return self._unreachable(rc, off)
        old = np.asarray(self.path)
        rejoin = None
        for j, q in enumerate(new):
            if np.hypot(old[:, 0] - q[0], old[:, 1] - q[1]).min() <= 1.5:
                rejoin = j * self.grid.cell
                break
        self.path, self.progress = new, 0
        msg = (f"You are {off:.0f} m off the route, so I re-routed from your position. "
               + (f"It rejoins the original route in about {rejoin:.0f} m." if rejoin is not None and rejoin > 0
                  else "It is a new route to the destination."))
        return self._ev("rerouted", msg, rc, off)

    def _unreachable(self, rc, off):
        """Goal cannot be reached from here: offer the closest reachable point instead."""
        F, pred = forward_tree(self.grid, self.ec, rc)
        reach = np.argwhere(np.isfinite(F))
        d = np.hypot(reach[:, 0] - self.goal[0], reach[:, 1] - self.goal[1])
        near = tuple(int(x) for x in reach[int(np.argmin(d))])
        R, C = F.shape
        out, i, s = [], near[0] * C + near[1], rc[0] * C + rc[1]
        while i != s and i >= 0:
            out.append((i // C, i % C))
            i = int(pred.ravel()[i])
        path = [rc] + list(reversed(out)) if out else [rc]
        ev = self._ev("goal_unreachable",
                      f"The destination can't be reached from here under the current conditions. The closest "
                      f"reachable point is {d.min() * self.grid.cell:.0f} m from it. Showing the way there; "
                      f"consider choosing a new destination.", rc, off, path=path)
        ev["closest_point"] = near
        return ev

    def _ev(self, status, message, rc, off, path=None):
        path = self.path if path is None else path
        g = self._guidance(rc, path) if len(path) > 1 else dict(bearing=None, leg_m=0.0)
        return dict(status=status, message=message, path=path, off_route_m=off,
                    eta_min=self.eta_min(rc), **g)

    # ---------------------------------------------------------------- new conditions
    def refresh_conditions(self, new_grid, fix_rc):
        """
        New weather/water arrived (new_grid = routing grid with updated snow etc.). The cost field is
        updated at once, but THE ROUTE IS NOT CHANGED: returns an offer dict (or None) to accept.
        """
        rc = self._snap(fix_rc)
        old_tail = self.path[min(self.progress, len(self.path) - 1):]
        old_eta_before = self.eta_min(rc)
        self._rebuild(new_grid)
        c_old = _tail_cost(old_tail, self.ec_map)
        c_new = float(self.T[rc])
        self.offer = None
        if not np.isfinite(c_new):
            ev = self._unreachable(rc, 0.0)
            self.offer = dict(reason="goal_unreachable", path=ev["path"], message=ev["message"],
                              old_eta_min=float("inf"), new_eta_min=float("inf"), gain_min=float("inf"))
            return self.offer
        new_path = descend(self.T, self.ec, rc, self.goal)
        gain = c_old - c_new
        why = None
        if not np.isfinite(c_old):
            why, gain = "route_blocked", float("inf")
            msg = "Conditions changed and your current route is no longer passable."
        elif gain >= max(self.switch_frac * c_old, self.switch_min_s) and gain > self._declined_gain * 1.5:
            why = "faster_route"
            msg = (f"Conditions changed: a different route is now about {fmt_duration(gain / 60)} faster "
                   f"({fmt_duration(c_new / 60)} instead of {fmt_duration(c_old / 60)}).")
            if self.locked:
                msg += " Your locked route is unchanged unless you accept."
        if why is None:
            return None
        self.offer = dict(reason=why, path=new_path, message=msg, gain_min=gain / 60,
                          old_eta_min=c_old / 60, new_eta_min=c_new / 60,
                          previous_eta_min=old_eta_before)
        return self.offer

    def accept_offer(self):
        if self.offer:
            self.path, self.progress, self._strikes = self.offer["path"], 0, 0
            self.offer, self._declined_gain = None, 0.0
        return self.path

    def decline_offer(self):
        """Keep the current route. Do not offer again unless the gain grows by 50%."""
        if self.offer and np.isfinite(self.offer["gain_min"]):
            self._declined_gain = self.offer["gain_min"] * 60
        self.offer = None
        return self.path
