"""Export the deterministic terrain demo consumed by index.html.

Run with python -I export_phone_state.py. No source preview is imported. The
geometry, likelihoods and routing equations are adapted from make_map_preview.py.
Only this script's directory can receive output. Requires numpy, scipy, rasterio.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import transform
from scipy import ndimage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra


ROOT = Path(__file__).resolve().parent
DEFAULT_STATS = ROOT.parents[2] / "3_Resources" / "attachments" / "2026-11-07_CodeMetalForge_地図見本" / "stats.json"
EYE_M = 1.7
CURVATURE = (1 - 0.13) / (2 * 6371000.0)
COMPASS_SIGMA = 6.0
BARO_SIGMA = 25.0


def wrap(value):
    return (value + 180) % 360 - 180


def bearing_likelihood(delta, sigma=COMPASS_SIGMA):
    return np.exp((np.cos(np.radians(delta)) - 1) / math.radians(sigma) ** 2)


class Terrain:
    def __init__(self, dem: Path, stats: dict):
        with rasterio.open(dem) as ds:
            if ds.count != 1 or ds.crs is None or ds.crs.to_epsg() != 26912:
                raise ValueError("Expected a one-band EPSG:26912 DEM")
            if ds.transform.b or ds.transform.d or ds.res[0] > 5.1 or ds.res[1] > 5.1:
                raise ValueError("Use the provided north-up 5 m DEM for cliff checks")
            self.bounds, self.crs = ds.bounds, ds.crs
            self.w = round((self.bounds.right - self.bounds.left) / 30)
            self.h = round((self.bounds.top - self.bounds.bottom) / 30)
            if ([self.h, self.w] != stats["grid_30m"]
                    or not np.allclose(list(self.bounds), stats["bounds_utm"], rtol=0, atol=.01)):
                raise ValueError("DEM bounds and grid must match the reference peak indices")
            raw = ds.read(1, out_shape=(self.h * 6, self.w * 6),
                          resampling=Resampling.average, masked=True)
            self.valid5 = ~np.ma.getmaskarray(raw) & np.isfinite(raw.data)
            if not self.valid5.any():
                raise ValueError("DEM contains no usable cells")
            self.z5 = raw.filled(np.nan).astype(np.float32)
            self.input_info = {"width": ds.width, "height": ds.height,
                               "resolution_m": list(ds.res), "crs": str(ds.crs)}
        self.rx = (self.bounds.right - self.bounds.left) / self.w
        self.ry = (self.bounds.top - self.bounds.bottom) / self.h
        self.valid30 = self.block(self.valid5).all(axis=(1, 3))
        # Fill only for numerical operators, then exclude cells influenced by a hole.
        if not self.valid5.all():
            nearest = ndimage.distance_transform_edt(~self.valid5, return_distances=False,
                                                     return_indices=True)
            self.z5 = self.z5[tuple(nearest)]
        self.z = self.block(self.z5).mean(axis=(1, 3))
        gy, gx = np.gradient(self.z5, self.ry / 6, self.rx / 6)
        self.s5 = np.degrees(np.arctan(np.hypot(gx, gy)))
        gy, gx = np.gradient(self.z, self.ry, self.rx)
        self.s30 = np.degrees(np.arctan(np.hypot(gx, gy)))
        self.smax = self.block(self.s5).max(axis=(1, 3))
        # This is the reference's flat-area heuristic, not surveyed water data.
        flat = ((ndimage.maximum_filter(self.z5, 5) - ndimage.minimum_filter(self.z5, 5)) < .15)
        flat &= self.valid5
        labels, n = ndimage.label(flat)
        sizes = np.bincount(labels.ravel())
        big = sizes > 300
        big[0] = False
        lake5 = ndimage.binary_closing(big[labels], iterations=2)
        self.lake = self.block(lake5).mean(axis=(1, 3)) > .5
        buffered_valid5 = ndimage.binary_erosion(self.valid5, iterations=1, border_value=1)
        self.valid = self.valid30 & self.block(buffered_valid5).all(axis=(1, 3)) & ~self.lake
        self.r, self.c = np.mgrid[:self.h, :self.w].astype(np.float64)
        xx = self.bounds.left + (self.c + .5) * self.rx
        yy = self.bounds.top - (self.r + .5) * self.ry
        lon, lat = transform(self.crs, "EPSG:4326", xx.ravel(), yy.ravel())
        self.lon = np.asarray(lon).reshape(self.h, self.w)
        self.lat = np.asarray(lat).reshape(self.h, self.w)
        # Local direction of true north in the conformal UTM projection.
        xn, yn = transform("EPSG:4326", self.crs, lon, (self.lat.ravel() + .0001).tolist())
        self.north_grid = np.degrees(np.arctan2(np.asarray(xn).reshape(xx.shape) - xx,
                                              np.asarray(yn).reshape(yy.shape) - yy))
        self.peaks = []
        for i, p in enumerate(stats["peaks"]):
            self.peaks.append({"id": f"peak_{i}", "name": p["name"],
                               "lat": p["lat"], "lon": p["lon"],
                               "elevation_m": p["z"], "r5": p["r5"], "c5": p["c5"]})
        self.geometry()

    def block(self, values):
        return values.reshape(self.h, 6, self.w, 6)

    def cell(self, latlon):
        lat, lon = latlon
        x, y = transform("EPSG:4326", self.crs, [lon], [lat])
        col = math.floor((x[0] - self.bounds.left) / self.rx)
        row = math.floor((self.bounds.top - y[0]) / self.ry)
        if not (0 <= row < self.h and 0 <= col < self.w):
            raise ValueError("Reference position is outside the DEM")
        return row * self.w + col

    def point(self, index):
        return {"lat": round(float(self.lat.flat[index]), 7),
                "lon": round(float(self.lon.flat[index]), 7),
                "elevation_m": round(float(self.z.flat[index]), 2)}

    def geometry(self):
        n = len(self.peaks)
        self.visible = np.zeros((n, self.h, self.w), bool)
        self.bearing = np.zeros((n, self.h, self.w), np.float32)
        self.elevation = np.zeros_like(self.bearing)
        self.distance = np.zeros_like(self.bearing)
        ts = np.linspace(.02, .97, 120).astype(np.float32)[:, None, None]
        for k, p in enumerate(self.peaks):
            pr, pc = (p["r5"] + .5) / 6 - .5, (p["c5"] + .5) / 6 - .5
            if not (0 <= pr < self.h and 0 <= pc < self.w):
                raise ValueError("Peak index is outside the DEM")
            for r0 in range(0, self.h, 24):
                r1 = min(self.h, r0 + 24)
                rr, cc = self.r[r0:r1], self.c[r0:r1]
                de, dn = (pc - cc) * self.rx, (rr - pr) * self.ry
                dist = np.hypot(de, dn)
                zo = self.z[r0:r1] + EYE_M
                zp = p["elevation_m"] - CURVATURE * dist ** 2
                grid_bearing = np.degrees(np.arctan2(de, dn))
                self.bearing[k, r0:r1] = (grid_bearing - self.north_grid[r0:r1]) % 360
                self.elevation[k, r0:r1] = np.degrees(np.arctan2(zp - zo, dist))
                self.distance[k, r0:r1] = dist
                sr = np.rint(rr + ts * (pr - rr)).astype(int)
                sc = np.rint(cc + ts * (pc - cc)).astype(int)
                zs = self.z[sr, sc] - CURVATURE * (ts * dist) ** 2
                zl = zo + ts * (zp - zo)
                self.visible[k, r0:r1] = (~np.any(zs > zl + 2, axis=0)
                    & np.all(self.valid30[sr, sc], axis=0) & (dist > 90))

    def norm(self, probability):
        probability = np.where(self.valid, probability, 0)
        total = float(probability.sum())
        if not math.isfinite(total) or total <= 0:
            raise ValueError("The observations leave no probability mass")
        return probability / total

    def skyline(self, index):
        row, col = divmod(index, self.w)
        # Angles are true-north indexed. The raster uses grid-north coordinates.
        angles = np.radians(np.arange(360) + self.north_grid.flat[index])[:, None]
        distances = np.arange(15.0, 14000.0, 15.0)[None, :]
        rr = row - np.cos(angles) * distances / self.ry
        cc = col + np.sin(angles) * distances / self.rx
        inside = (rr >= 0) & (rr <= self.h - 1) & (cc >= 0) & (cc <= self.w - 1)
        valid = ndimage.map_coordinates(self.valid30.astype(np.float32), [rr, cc],
                                        order=0, mode="constant", cval=0) > .5
        usable = np.logical_and.accumulate(inside & valid, axis=1)
        z = ndimage.map_coordinates(self.z, [rr, cc], order=1, mode="nearest")
        elevation = np.degrees(np.arctan2(z - CURVATURE * distances ** 2
                                         - self.z.flat[index] - EYE_M, distances))
        horizon = np.where(usable, elevation, -90).max(axis=1)
        reach = np.where(usable, distances, 0).max(axis=1)
        return np.round(horizon, 3).tolist(), np.round(reach, 1).tolist()

    def stage(self, probability, stage_id, locks):
        smoothed = ndimage.gaussian_filter(probability, .7)
        estimate = int(np.argmax(np.where(self.valid, smoothed, -1)))
        # Diffuse stages can have tied maxima. Pick the tied mode closest to the
        # posterior centroid instead of the arbitrary top-left cell.
        candidates = np.flatnonzero(self.valid & np.isclose(smoothed, smoothed.flat[estimate],
                                                           rtol=1e-8, atol=0))
        if len(candidates) > 1:
            cy, cx = float((probability * self.r).sum()), float((probability * self.c).sum())
            cr, cc = np.divmod(candidates, self.w)
            estimate = int(candidates[np.argmin(((cr - cy) * self.ry) ** 2 + ((cc - cx) * self.rx) ** 2)])
        er, ec = divmod(estimate, self.w)
        # A circular 95% model-mass radius around the estimate, not an equal-area
        # radius. Disconnected posterior modes are included by distance order.
        dist = np.hypot((self.r - er) * self.ry, (self.c - ec) * self.rx).ravel()
        ordered = np.argsort(dist)
        cumulative = np.cumsum(probability.ravel()[ordered])
        ci = min(int(np.searchsorted(cumulative, .95)), len(ordered) - 1)
        radius = float(dist[ordered[ci]]) + math.hypot(self.rx, self.ry) / 2
        sorted_p = np.sort(probability.ravel())[::-1]
        n95 = min(int(np.searchsorted(np.cumsum(sorted_p), .95)) + 1, len(sorted_p))
        peaks = []
        for k, peak in enumerate(self.peaks):
            peaks.append({"id": peak["id"], "name": peak["name"],
                          "bearing_deg": round(float(self.bearing[k].flat[estimate]), 3),
                          "elevation_deg": round(float(self.elevation[k].flat[estimate]), 3),
                          "distance_m": round(float(self.distance[k].flat[estimate]), 1),
                          "visible": bool(self.visible[k].flat[estimate])})
        skyline, coverage = self.skyline(estimate)
        result = {"id": stage_id, "locks": locks, "estimate": self.point(estimate),
                  "confidence_radius_m": math.ceil(radius),
                  "confidence_label": "95% model mass, uncalibrated",
                  "area95_km2": round(n95 * self.rx * self.ry / 1e6, 5),
                  "skyline_deg": skyline, "skyline_range_m": coverage,
                  "skyline_complete": False, "peaks": peaks,
                  "visible_peaks": [p for p in peaks if p["visible"]]}
        return result, estimate

    def route(self, source, target, limit):
        allowed = self.valid & (self.s30 <= limit) & (self.smax <= 45)
        empty = {"available": False, "limit_deg": limit, "points": [],
                 "reason": "No connected route under the terrain model"}
        # Never override a blocked start or endpoint to manufacture a route.
        if not (allowed.flat[source] and allowed.flat[target]):
            return {**empty, "reason": "Start or rendezvous exceeds this slope limit"}, []
        indices = np.arange(self.h * self.w).reshape(self.h, self.w)
        rows, cols, weights = [], [], []
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if not dr and not dc:
                    continue
                ra, rb = max(0, -dr), self.h - max(0, dr)
                ca, cb = max(0, -dc), self.w - max(0, dc)
                a, b = indices[ra:rb, ca:cb], indices[ra+dr:rb+dr, ca+dc:cb+dc]
                good = allowed[ra:rb, ca:cb] & allowed[ra+dr:rb+dr, ca+dc:cb+dc]
                if dr and dc:
                    good &= allowed[ra+dr:rb+dr, ca:cb] & allowed[ra:rb, ca+dc:cb+dc]
                length = math.hypot(dr * self.ry, dc * self.rx)
                grade = (self.z[ra+dr:rb+dr, ca+dc:cb+dc] - self.z[ra:rb, ca:cb]) / length
                speed = .6 * 6 * np.exp(-3.5 * np.abs(grade + .05))
                rows.append(a[good]); cols.append(b[good]); weights.append((length / (speed / 3.6))[good])
        graph = coo_matrix((np.concatenate(weights), (np.concatenate(rows), np.concatenate(cols))),
                           shape=(self.h*self.w, self.h*self.w)).tocsr()
        distance, previous = dijkstra(graph, directed=True, indices=source, return_predecessors=True)
        if not np.isfinite(distance[target]):
            return empty, []
        path = [target]
        while path[-1] != source:
            parent = int(previous[path[-1]])
            if parent < 0 or len(path) > self.h * self.w:
                raise ValueError("Invalid route predecessor chain")
            path.append(parent)
        path.reverse()
        rr, cc = np.divmod(path, self.w)
        length = float(np.hypot(np.diff(rr)*self.ry, np.diff(cc)*self.rx).sum())
        climb = float(np.maximum(0, np.diff(self.z.ravel()[path])).sum())
        result = {"available": True, "limit_deg": limit, "points": [self.point(i) for i in path],
                  "length_m": round(length, 1), "time_s": round(float(distance[target]), 1),
                  "climb_m": round(climb, 1), "max_average_slope_deg": round(float(self.s30.ravel()[path].max()), 3),
                  "max_fine_slope_deg": round(float(self.smax.ravel()[path].max()), 3),
                  "model_only": True}
        return result, path


def export(dem, stats_path):
    stats = json.loads(stats_path.read_text(encoding="utf-8-sig"))
    terrain = Terrain(dem, stats)
    truth = terrain.cell(stats["example"]["truth_latlon"])
    target = terrain.cell(stats["rendezvous_latlon"])
    probability = terrain.norm(np.ones((terrain.h, terrain.w)))
    prior, _ = terrain.stage(probability, "prior", 0)
    probability = terrain.norm(probability * np.exp(-.5 * ((terrain.z - stats["example"]["baro_obs_m"]) / BARO_SIGMA) ** 2))
    barometer, _ = terrain.stage(probability, "barometer", 0)
    stages = [prior, barometer]
    names = {p["name"]: k for k, p in enumerate(terrain.peaks)}
    observations = []
    visible_elevations = np.where(terrain.visible, terrain.elevation, -np.inf)
    tallest = np.argmax(visible_elevations, axis=0)
    has_peak = np.isfinite(visible_elevations.max(axis=0))
    first_bearing = np.take_along_axis(terrain.bearing, tallest[None], 0)[0]
    for i, obs in enumerate(stats["example"]["bearings"]):
        k = names[obs["peak"]]
        # Reference bearings were UTM grid angles. Convert the synthetic sample
        # at its known simulated origin to true north before replaying.
        true_bearing = (obs["obs_deg"] - float(terrain.north_grid.flat[truth])) % 360
        if i == 0:
            main = bearing_likelihood(true_bearing - first_bearing)
            any_peak = np.max(np.where(terrain.visible, bearing_likelihood(true_bearing - terrain.bearing), 0), axis=0)
            likelihood = np.where(has_peak, .8 * main + .2 * any_peak, 1e-3)
        else:
            likelihood = np.where(terrain.visible[k], bearing_likelihood(true_bearing - terrain.bearing[k]), .02)
        probability = terrain.norm(probability * likelihood)
        stage, estimate = terrain.stage(probability, f"lock_{i+1}", i+1)
        stages.append(stage)
        observations.append({"peak_id": terrain.peaks[k]["id"], "peak_name": obs["peak"],
                             "bearing_true_deg": round(true_bearing, 3),
                             "elevation_deg": round(float(terrain.elevation[k].flat[truth]), 3),
                             "timestamp": f"2026-11-08T00:00:{i*10:02d}Z",
                             "synthetic": True, "elevation_used_in_estimate": False})
    healthy, route_cells = terrain.route(estimate, target, 30)
    injured, injured_cells = terrain.route(estimate, target, 18)
    if not healthy["available"] or injured["available"]:
        raise ValueError("Scenario no longer matches the demo; inspect terrain and reference inputs")
    er, ec = divmod(estimate, terrain.w)
    tr, tc = divmod(truth, terrain.w)
    error_m = math.hypot((er-tr)*terrain.ry, (ec-tc)*terrain.rx)
    confidence_mass = float(probability[np.hypot((terrain.r-er)*terrain.ry, (terrain.c-ec)*terrain.rx)
                                         <= stages[-1]["confidence_radius_m"]].sum())
    assert confidence_mass >= .95
    assert len({p["peak_id"] for p in observations}) == 3
    assert all(len(s["skyline_deg"]) == 360 and np.isfinite(s["skyline_deg"]).all() for s in stages)
    assert healthy["max_average_slope_deg"] <= 30 and healthy["max_fine_slope_deg"] <= 45
    result = {
        "schema_version": 1, "demo": True, "scenario": "Mount Moran terrain replay",
        "sources": [{"name": "USGS 3DEP", "url": "https://www.usgs.gov/3d-elevation-program"},
                    {"name": "OpenTopography", "url": "https://opentopography.org/"}],
        "settings": {"declination_deg": 0, "declination_verified": False,
                     "bearing_reference": "true_north", "eye_height_m": EYE_M,
                     "compass_sigma_deg": COMPASS_SIGMA, "barometer_sigma_m": BARO_SIGMA,
                     "barometer_demo_altitude_m": stats["example"]["baro_obs_m"]},
        "metadata": {"dem": dem.name, "dem_sha256": hashlib.sha256(dem.read_bytes()).hexdigest(),
                     "reference_sha256": hashlib.sha256(stats_path.read_bytes()).hexdigest(),
                     "grid_rows": terrain.h, "grid_columns": terrain.w,
                     "grid_resolution_m": [terrain.rx, terrain.ry], "input": terrain.input_info,
                     "bounds_utm": list(terrain.bounds),
                     "confidence_method": "Smallest radius about the smoothed MAP estimate containing at least 95% posterior mass, plus half a cell diagonal",
                     "confidence_calibrated": False,
                     "bearing_method": "Local true-north rotation of UTM grid bearings",
                     "replay_notice": "Fixed synthetic observations drive these estimates. Browser locks do not recompute Bayes inference.",
                     "skyline_notice": "30 m terrain sampled every 15 m; horizons stop at this tile or nodata. Outside peaks and tree cover are absent.",
                     "route_notice": "Model route only. Uses 30 m mean slope, maximum 5 m slope and a heuristic lake mask; no surveyed trails, rivers, vegetation or hazard review.",
                     "limitations": ["Not validated for real navigation or rescue", "Declination is an unverified placeholder of zero degrees",
                                     "Peak labels except Mt Moran are elevation labels, not verified geographic names",
                                     "Posterior stage estimates can jump between distant modes before enough observations",
                                     "Recorded elevation angle is not used by the reference localization model",
                                     "Route excludes diagonal corner crossing and does not force blocked endpoints open"]},
        "peaks": [{k: v for k, v in p.items() if k not in ("r5", "c5")} for p in terrain.peaks],
        "target_order": [o["peak_id"] for o in observations], "stages": stages,
        "observations": observations, "routes": {"healthy": healthy, "injured": injured},
        "rendezvous": {**terrain.point(target), "name": "Demo rendezvous", "synthetic": True},
        "validation": {"synthetic_error_m": round(error_m, 2),
                       "reference_error_m": stats["example"]["err_m"],
                       "final_radius_posterior_mass": round(confidence_mass, 8),
                       "route_nodes": len(route_cells), "injured_route_nodes": len(injured_cells),
                       "reference_length_m": stats["routes"]["healthy"]["length_km"] * 1000,
                       "checks": ["finite JSON", "360 samples at every stage", "three distinct targets",
                                  "healthy connected route", "injured no route", "slope constraints",
                                  "95% circular model mass", "no diagonal corner cutting"]}}
    json.dumps(result, allow_nan=False)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dem", type=Path, default=ROOT / "input" / "dem_5m.tif")
    parser.add_argument("--stats", type=Path, default=DEFAULT_STATS)
    parser.add_argument("--output", type=Path, default=ROOT / "phone_state.json")
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT):
        parser.error("Output must stay inside this prototype directory")
    if output.suffix.lower() != ".json" or output.resolve() in (args.stats.resolve(), args.dem.resolve()):
        parser.error("Choose a JSON output distinct from the inputs")
    state = export(args.dem.resolve(), args.stats.resolve())
    output.write_text(json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                      encoding="utf-8", newline="\n")
    print(json.dumps({"output": str(output), "bytes": output.stat().st_size,
                      "validation": state["validation"],
                      "stages": [{"id": s["id"], "radius_m": s["confidence_radius_m"],
                                  "estimate": s["estimate"]} for s in state["stages"]]}, indent=2))


if __name__ == "__main__":
    main()
