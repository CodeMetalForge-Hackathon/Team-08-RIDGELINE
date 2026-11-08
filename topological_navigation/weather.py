"""
weather.py - OPTIONAL live conditions layer. The app works without it (offline-first).

    from weather import load_conditions, apply_conditions, Conditions

    cond = load_conditions(lat, lon, cache_path="weather_cache.json")
        # tries Open-Meteo if there is signal, else the cached copy, else None
    if cond is None:                       # no signal and nothing cached: just route on terrain
        cond = Conditions.simulated_for_grid(grid)   # (demo only) invented storm, clearly labelled
    info = apply_conditions(grid, cond)    # sets grid.snow_depth_cm / snowline_m / weather
    planner = Planner(grid, start)         # routes now account for snow

Re-run load_conditions + apply_conditions whenever signal appears; Planner / Navigator notice.

What it models (honestly approximate)
  * Open-Meteo gives ONE value per hour at the grid cell nearest to your point (a few km wide, with a
    smoothed model elevation). We turn that into a snow-depth RASTER over your DEM by assuming depth
    grows with elevation above a "lying-snow line". It does not know about wind loading, aspect or
    tree cover, so treat the snowline as roughly +/- 200 m.
  * Two lines are reported:
      falling snowline = freezing level - drop_m  (where precipitation falls as snow right now)
      lying snow line  = where the modelled snow cover starts to build on the ground
  * The depth ramps in smoothly over `ramp_m` metres: there is no wall at the line.

Open-Meteo variables used (hourly): temperature_2m, precipitation, snowfall (cm), snow_depth (m),
freezing_level_height (m), pressure_msl. Free for non-commercial use; a commercial app needs a licence.
"""
from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

import numpy as np

API = "https://api.open-meteo.com/v1/forecast"
HOURLY = ["temperature_2m", "precipitation", "snowfall", "snow_depth",
          "freezing_level_height", "pressure_msl"]
LAPSE_C_PER_M = 0.0065              # standard atmosphere, used only if freezing level is missing


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

def _arr(x):
    return np.array([np.nan if v is None else v for v in x], float)


@dataclass
class Conditions:
    lat: float
    lon: float
    model_elev_m: float            # elevation of the weather-model cell (NOT your DEM)
    t: np.ndarray                  # unix seconds (UTC), hourly
    temp_c: np.ndarray
    precip_mm: np.ndarray
    snowfall_cm: np.ndarray
    snow_depth_cm: np.ndarray
    freezing_m: np.ndarray         # freezing level, m above sea level (may hold NaN)
    pressure_hpa: np.ndarray
    fetched_at: float              # unix seconds when this data was downloaded
    source: str = "open-meteo"     # open-meteo | cache | simulated

    # ---- time helpers
    def age_h(self, now=None):
        return ((time.time() if now is None else now) - self.fetched_at) / 3600.0

    def index(self, t=None):
        """Index of the hour closest to t (unix s; default now), and whether t was outside the data."""
        t = time.time() if t is None else float(t)
        i = int(np.argmin(np.abs(self.t - t)))
        outside = t < self.t[0] - 1800 or t > self.t[-1] + 1800
        return i, outside

    def window(self, t, hours):
        """Sum of snowfall (cm) / precipitation (mm) in the `hours` ending at t (hours<0: starting at t)."""
        a, b = (t - hours * 3600, t) if hours > 0 else (t, t - hours * 3600)
        m = (self.t > a) & (self.t <= b)
        return float(np.nansum(self.snowfall_cm[m])), float(np.nansum(self.precip_mm[m]))

    def freezing_level(self, i):
        f = self.freezing_m[i]
        if np.isfinite(f):
            return float(f), "model"
        if np.isfinite(self.temp_c[i]):
            return float(self.model_elev_m + self.temp_c[i] / LAPSE_C_PER_M), "lapse-rate estimate"
        return float("nan"), "unknown"

    # ---- (de)serialisation
    def to_json(self):
        d = {k: (v.tolist() if isinstance(v, np.ndarray) else v) for k, v in self.__dict__.items()}
        return json.dumps(d)

    @classmethod
    def from_json(cls, s):
        d = json.loads(s)
        for k in ("t", "temp_c", "precip_mm", "snowfall_cm", "snow_depth_cm", "freezing_m", "pressure_hpa"):
            d[k] = _arr(d[k])
        return cls(**d)

    @classmethod
    def from_open_meteo(cls, js, lat, lon, now=None):
        h = js["hourly"]
        depth_m = _arr(h["snow_depth"])
        return cls(lat=lat, lon=lon, model_elev_m=float(js.get("elevation", float("nan"))),
                   t=np.array(h["time"], float), temp_c=_arr(h["temperature_2m"]),
                   precip_mm=_arr(h["precipitation"]), snowfall_cm=_arr(h["snowfall"]),
                   snow_depth_cm=depth_m * 100.0, freezing_m=_arr(h["freezing_level_height"]),
                   pressure_hpa=_arr(h["pressure_msl"]),
                   fetched_at=time.time() if now is None else now)

    # ---- synthetic data for demos and tests
    @classmethod
    def simulated(cls, model_elev_m, freezing_m=2600.0, depth_cm=10.0, trend_m_per_h=-20.0,
                  storm=(6, 24), storm_cm_per_h=1.5, hours=48, start=None, lat=0.0, lon=0.0):
        """
        An invented storm: freezing level drifting by `trend_m_per_h`, snow falling at
        `storm_cm_per_h` during hours storm[0]..storm[1] whenever it is below freezing at the model
        elevation, light melt otherwise. Starts `hours//4` hours in the past. Labelled "simulated".
        """
        now = time.time() if start is None else start
        t0 = math.floor(now / 3600) * 3600 - (hours // 4) * 3600
        h = np.arange(hours)
        t = t0 + h * 3600.0
        frz = freezing_m + trend_m_per_h * h
        temp = (frz - model_elev_m) * LAPSE_C_PER_M
        rate = np.where((h >= storm[0]) & (h < storm[1]) & (temp < 1.0), storm_cm_per_h, 0.0)
        depth = np.empty(hours)
        d = depth_cm
        for k in range(hours):
            d = max(d + rate[k] - (0.15 if temp[k] > 0.5 else 0.0), 0.0)
            depth[k] = d
        return cls(lat=lat, lon=lon, model_elev_m=float(model_elev_m), t=t, temp_c=temp,
                   precip_mm=rate * 1.0, snowfall_cm=rate, snow_depth_cm=depth, freezing_m=frz,
                   pressure_hpa=1013 - 0.2 * h, fetched_at=now, source="simulated")

    @classmethod
    def simulated_for_grid(cls, grid, **kw):
        """Demo storm scaled to the DEM: freezing level near the 70th elevation percentile."""
        lo, p70, hi = np.percentile(grid.dem, [5, 70, 98])
        kw.setdefault("freezing_m", float(p70 + 300))
        kw.setdefault("depth_cm", 8.0)
        return cls.simulated(model_elev_m=float(np.median(grid.dem)), **kw)


# --------------------------------------------------------------------------
# Fetch / cache (offline-first)
# --------------------------------------------------------------------------

def fetch_open_meteo(lat, lon, past_days=1, forecast_days=3, timeout=6.0):
    q = dict(latitude=f"{lat:.4f}", longitude=f"{lon:.4f}", hourly=",".join(HOURLY),
             timeformat="unixtime", past_days=past_days, forecast_days=forecast_days)
    req = urllib.request.Request(API + "?" + urllib.parse.urlencode(q),
                                 headers={"User-Agent": "lost-hiker-app-hackathon"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def load_conditions(lat, lon, cache_path="weather_cache.json", online=True, timeout=6.0,
                    max_cache_km=25.0, verbose=True):
    """
    Live data if reachable (and cached for next time), else the cached copy, else None.
    Never raises on a network problem: no signal is a normal state.
    """
    if online:
        try:
            cond = Conditions.from_open_meteo(fetch_open_meteo(lat, lon, timeout=timeout), lat, lon)
            try:
                with open(cache_path, "w") as f:
                    f.write(cond.to_json())
            except OSError:
                pass
            if verbose:
                print(f"Weather: live data from Open-Meteo ({len(cond.t)} hourly steps).")
            return cond
        except Exception as e:                       # offline, timeout, bad JSON, ...
            if verbose:
                print(f"Weather: no live data ({type(e).__name__}); trying the cache.")
    try:
        with open(cache_path) as f:
            cond = Conditions.from_json(f.read())
        km = math.hypot((cond.lat - lat) * 111.0, (cond.lon - lon) * 111.0 * math.cos(math.radians(lat)))
        if km > max_cache_km:
            if verbose:
                print(f"Weather: cached data is {km:.0f} km away; ignoring it.")
            return None
        cond.source = "cache"
        if verbose:
            print(f"Weather: using cached data, {cond.age_h():.1f} h old.")
        return cond
    except (OSError, ValueError, KeyError):
        if verbose:
            print("Weather: nothing cached either; routing on terrain only.")
        return None


def grid_center_latlon(grid, crs):
    """Lat/lon of the DEM centre (needs pyproj)."""
    from pyproj import Transformer
    H, W = grid.dem.shape
    x, y = grid.rc_to_xy(H / 2, W / 2)
    lon, lat = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform(x, y)
    return float(lat), float(lon)


# --------------------------------------------------------------------------
# Snowline + depth raster
# --------------------------------------------------------------------------

def _smoothstep(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


def snow_model(cond: Conditions, t=None, drop_m=300.0, cm_per_100m=10.0):
    """Reduce the series at time t to (lying_line_m, falling_line_m, info)."""
    i, outside = cond.index(t)
    frz, how = cond.freezing_level(i)
    falling = frz - drop_m if np.isfinite(frz) else float("nan")
    d_ref = float(cond.snow_depth_cm[i]) if np.isfinite(cond.snow_depth_cm[i]) else 0.0
    k = cm_per_100m / 100.0                                     # cm per metre of elevation
    z_ref = cond.model_elev_m
    if d_ref >= 1.0:
        lying = z_ref - d_ref / k                               # extrapolate depth down to zero
    else:
        lying = max(z_ref, falling) if np.isfinite(falling) else z_ref
    return lying, falling, dict(i=i, outside=outside, freezing_m=frz, freezing_how=how,
                                d_ref=d_ref, k=k, z_ref=z_ref)


def apply_conditions(grid, cond: Conditions, t=None, drop_m=300.0, cm_per_100m=10.0,
                     ramp_m=150.0, max_cm=150.0, now=None):
    """
    Build grid.snow_depth_cm for time t (unix s, default now) and set grid.snowline_m / grid.weather.
    depth(z) = cm_per_100m per 100 m above the lying-snow line, eased in over ramp_m, capped at max_cm.
    Returns grid.weather. Pass cond=None to REMOVE the weather layer (back to terrain only).
    """
    if cond is None:
        grid.snow_depth_cm = grid.snowline_m = grid.weather = None
        return None
    now = time.time() if now is None else now
    t = now if t is None else t
    lying, falling, m = snow_model(cond, t, drop_m, cm_per_100m)
    z = grid.dem
    # eased in without a wall: smoothstep weights the first ramp_m metres above the line
    depth = m["k"] * np.maximum(z - lying, 0.0) * _smoothstep((z - lying) / ramp_m)
    grid.snow_depth_cm = np.minimum(depth, max_cm).astype(np.float32)
    grid.snowline_m = float(lying)
    new24, _ = cond.window(t, 24)
    next12, rain12 = cond.window(t, -12)
    _, rain24 = cond.window(t, 24)
    i = m["i"]
    grid.weather = dict(
        source=cond.source, t=float(t), age_h=cond.age_h(now), stale=cond.age_h(now) > 6.0,
        outside_range=m["outside"], lying_line_m=float(lying), falling_line_m=float(falling),
        freezing_m=m["freezing_m"], freezing_how=m["freezing_how"], temp_c=float(cond.temp_c[i]),
        snow_ref_cm=m["d_ref"], new_snow_24h_cm=new24, snow_next_12h_cm=next12,
        rain_24h_mm=max(rain24 - new24 / 7.0, 0.0),     # precipitation minus snow water equivalent
        pressure_hpa=float(cond.pressure_hpa[i]),
        pressure_trend_3h=float(cond.pressure_hpa[min(i + 3, len(cond.t) - 1)] - cond.pressure_hpa[i]),
        max_snow_cm=float(grid.snow_depth_cm.max()),
        snow_area_frac=float((grid.snow_depth_cm >= 5).mean()))
    return grid.weather


def describe(grid):
    """One or two lines for the UI header."""
    w = grid.weather
    if not w:
        return "Weather: off (terrain only)"
    when = time.strftime("%a %H:%M", time.localtime(w["t"]))
    s = (f"Weather [{w['source']}, data {w['age_h']:.0f} h old{', STALE' if w['stale'] else ''}] for {when}: "
         f"snow starts ~{w['lying_line_m']:.0f} m, {w['temp_c']:.0f} C at the model point, "
         f"{w['snow_area_frac']:.0%} of the map has >=5 cm")
    if w["new_snow_24h_cm"] >= 1 or w["snow_next_12h_cm"] >= 1:
        s += f"; snowfall {w['new_snow_24h_cm']:.0f} cm last 24 h, {w['snow_next_12h_cm']:.0f} cm next 12 h"
    if w["outside_range"]:
        s += "  (time outside the data: showing the nearest hour)"
    return s


if __name__ == "__main__":
    from hiker_router import Grid
    yy, xx = np.mgrid[0:200, 0:200]
    g = Grid(dem=2000 + 6.0 * yy, cell=10.0)
    c = Conditions.simulated_for_grid(g)
    for dt in (0, 12, 24):
        apply_conditions(g, c, t=time.time() + dt * 3600)
        print(f"+{dt:2d} h:", describe(g))
