"""Export the 30 m solver grid that ridgeline/solver.js reads.

Inputs already in the repo (terrain viewer v2, same area and bounds as the big screen):
  terrain_viewer/data/moran/terrain_rgb.png   5 m Terrain-RGB, EPSG:4326, row 0 is north
  terrain_viewer/data/moran/terrain_meta.json bounds, decoder, named peaks
  terrain_viewer/data/moran/water_mask.png    water cells

Output: ridgeline/data/moran.json
  30 m elevation grid, land mask, the peak list, and for every peak the 30 m cells
  from which its summit is visible (line of sight on a 10 m DEM, eye height 1.7 m,
  earth curvature with refraction k = 0.13), plus the demo landing spot.

Peaks follow Rohan's find_true_peaks (topological_navigation/sprint2_localization.py),
with its pixel windows written in meters because that code ran on a 1 m DEM:
5 m smoothing, 800 m isolation window, altitude >= 2500 m, 1000 m regional mean,
macro TPI >= 150 m.

Run from the repo root:  python -I ridgeline/scripts/export_grid.py
"""
from __future__ import annotations

import argparse
import base64
import itertools
import json
import math
import time
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter, maximum_filter, uniform_filter

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
EYE_M = 1.7
K_REFRACTION = 0.13
R_EFF = 6371000.0 / (1.0 - K_REFRACTION)
FACTOR = 6            # 5 m source cells per 30 m solver cell
LOS_FACTOR = 2        # 10 m DEM for the line of sight
LOS_STEP_M = 15.0
SUMMIT_SKIP_M = 45.0  # ignore the last meters before the summit (DEM self-occlusion)
OBS_SKIP_M = 20.0
BEARING_SIGMA_DEG = 3.0
BARO_SIGMA_M = 25.0


def b64(arr: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(arr).tobytes()).decode('ascii')


def bits(mask: np.ndarray) -> str:
    return b64(np.packbits(mask.astype(np.uint8).ravel()))


def block_mean(a: np.ndarray, f: int) -> np.ndarray:
    h, w = a.shape
    a = a[: h - h % f, : w - w % f]
    return a.reshape(h // f, f, w // f, f).mean(axis=(1, 3))


def decode_terrain(path: Path, dec: dict) -> np.ndarray:
    rgb = np.asarray(Image.open(path).convert('RGB'), dtype=np.float64)
    return (rgb[..., 0] * dec['rScaler'] + rgb[..., 1] * dec['gScaler']
            + rgb[..., 2] * dec['bScaler'] + dec['offset'])


def find_true_peaks_m(elev: np.ndarray, cell_m: float, min_altitude=2500.0,
                      isolation_m=800.0, prominence_m=150.0) -> np.ndarray:
    """Rohan's find_true_peaks with the windows converted from 1 m pixels to meters."""
    smoothed = gaussian_filter(elev, sigma=5.0 / cell_m)
    is_max = smoothed == maximum_filter(smoothed, size=max(3, round(isolation_m / cell_m)))
    high = smoothed >= min_altitude
    regional = uniform_filter(smoothed, size=max(3, round(1000.0 / cell_m)))
    prominent = (smoothed - regional) >= prominence_m
    return np.argwhere(is_max & high & prominent)


def visibility(peak, obs_e, obs_n, obs_z, dem10, c10e, c10n):
    """True where the summit is visible from an eye at (obs_e, obs_n, obs_z)."""
    h10, w10 = dem10.shape
    de = peak['e_m'] - obs_e
    dn = peak['n_m'] - obs_n
    dist = np.hypot(de, dn)
    out = np.zeros(obs_e.shape[0], dtype=bool)
    chunk = 2048
    for s in range(0, obs_e.shape[0], chunk):
        sl = slice(s, s + chunk)
        d = dist[sl]
        steps = int(math.ceil(d.max() / LOS_STEP_M)) + 1
        t = (np.arange(1, steps, dtype=np.float32) / steps)[None, :]
        es = obs_e[sl, None] + de[sl, None] * t
        ns = obs_n[sl, None] + dn[sl, None] * t
        col = np.clip((es / c10e).astype(np.int32), 0, w10 - 1)
        row = np.clip(np.floor(h10 - ns / c10n).astype(np.int32), 0, h10 - 1)
        ter = dem10[row, col]
        sd = d[:, None] * t
        z0 = obs_z[sl, None]
        zp = peak['elev_m'] - (d[:, None] ** 2) / (2 * R_EFF)
        line = z0 + (zp - z0) * t
        blocked = (ter - sd ** 2 / (2 * R_EFF)) > line + 0.5
        blocked &= (sd > OBS_SKIP_M) & ((d[:, None] - sd) > SUMMIT_SKIP_M)
        out[sl] = ~blocked.any(axis=1)
    return out


def bearing_grad(pe, pn, e, n):
    x = pe - e
    y = pn - n
    d2 = x * x + y * y
    return np.degrees(np.arctan2(x, y)) % 360.0, -y / d2, x / d2, np.sqrt(d2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--area', default='moran')
    ap.add_argument('--out', default=None)
    args = ap.parse_args()
    t0 = time.time()
    area_dir = REPO / 'terrain_viewer' / 'data' / args.area
    meta = json.loads((area_dir / 'terrain_meta.json').read_text(encoding='utf-8'))
    west, south, east, north = meta['bounds']
    elev5 = decode_terrain(area_dir / meta['files']['terrain_rgb'], meta['elevation_decoder'])
    h5, w5 = elev5.shape
    assert (w5, h5) == (meta['grid']['width'], meta['grid']['height']), (w5, h5)
    c5e, c5n = meta['grid']['cell_m']
    c5 = (c5e + c5n) / 2

    water5 = np.asarray(Image.open(area_dir / meta['files']['water_mask']).convert('L'))
    if water5.shape != elev5.shape:
        water5 = np.asarray(Image.open(area_dir / meta['files']['water_mask']).convert('L')
                            .resize((w5, h5), Image.NEAREST))
    frac = float((water5 > 127).mean())
    water5 = water5 > 127 if abs(frac - meta['water_share']) <= abs(1 - frac - meta['water_share']) else water5 <= 127

    elev30 = block_mean(elev5, FACTOR)
    ny, nx = elev30.shape
    dx, dy = c5e * FACTOR, c5n * FACTOR
    land30 = block_mean(water5.astype(np.float64), FACTOR) < 0.5
    dem10 = block_mean(elev5, LOS_FACTOR).astype(np.float32)
    c10e, c10n = c5e * LOS_FACTOR, c5n * LOS_FACTOR

    def to_lonlat(e, n):
        return west + e / (w5 * c5e) * (east - west), south + n / (h5 * c5n) * (north - south)

    # Peaks, snapped to the raw summit within 40 m and merged within 250 m.
    raw = find_true_peaks_m(elev5, c5)
    snapped = []
    rad = int(round(40 / c5))
    for r, c in raw:
        r0, r1, c0, c1 = max(0, r - rad), min(h5, r + rad + 1), max(0, c - rad), min(w5, c + rad + 1)
        win = elev5[r0:r1, c0:c1]
        rr, cc = np.unravel_index(np.argmax(win), win.shape)
        snapped.append((float(win[rr, cc]), r0 + rr, c0 + cc))
    snapped.sort(reverse=True)
    peaks = []
    for h, r, c in snapped:
        e = (c + 0.5) * c5e
        n = (h5 - r - 0.5) * c5n
        if any(math.hypot(e - p['e_m'], n - p['n_m']) < 250 for p in peaks):
            continue
        lon, lat = to_lonlat(e, n)
        named = None
        for q in meta.get('peaks', []):
            qe = (q['lon'] - west) / (east - west) * w5 * c5e
            qn = (q['lat'] - south) / (north - south) * h5 * c5n
            dq = math.hypot(qe - e, qn - n)
            if dq < 300 and (named is None or dq < named[0]):
                named = (dq, q)
        peaks.append({
            'id': f'pk_{len(peaks) + 1}',
            'name': named[1]['name'] if named else f'Pk {round(h)}',
            'verified_name': bool(named and named[1].get('verified')),
            'lon': round(lon, 6), 'lat': round(lat, 6),
            'e_m': round(e, 1), 'n_m': round(n, 1), 'elev_m': round(h, 1),
        })
    print(f'{len(raw)} raw peak pixels -> {len(peaks)} peaks')

    jj, ii = np.meshgrid(np.arange(nx), np.arange(ny))
    obs_e = ((jj + 0.5) * dx).ravel()
    obs_n = ((ny - ii - 0.5) * dy).ravel()
    obs_z = (elev30.ravel() + EYE_M)
    vis = {}
    for p in peaks:
        v = visibility(p, obs_e, obs_n, obs_z, dem10, c10e, c10n)
        vis[p['id']] = v
        p['visible_share'] = round(float(v[land30.ravel()].mean()), 4)
        print(f"  {p['id']} {p['name']:<22} {p['elev_m']:7.1f} m  visible from {p['visible_share']:.1%} of land")

    # Demo landing spot: best resection geometry for three peaks that fit on one wall.
    gy, gx = np.gradient(elev30, dy, dx)
    slope = np.degrees(np.arctan(np.hypot(gx, gy))).ravel()
    dh_de, dh_dn = gx.ravel(), -gy.ravel()
    margin = ((obs_e > 800) & (obs_e < nx * dx - 800) & (obs_n > 800) & (obs_n < ny * dy - 800))
    cand = land30.ravel() & (slope < 25) & margin & (elev30.ravel() < 3100)
    sig = math.radians(BEARING_SIGMA_DEG)
    cinv = np.array([[2.0, 1.0], [1.0, 2.0]]) / (3 * sig * sig)
    best = None
    for a, b, c in itertools.combinations(range(len(peaks)), 3):
        trio = [peaks[a], peaks[b], peaks[c]]
        ok = cand & vis[trio[0]['id']] & vis[trio[1]['id']] & vis[trio[2]['id']]
        idx = np.flatnonzero(ok)
        if idx.size == 0:
            continue
        e, n = obs_e[idx], obs_n[idx]
        res = [bearing_grad(p['e_m'], p['n_m'], e, n) for p in trio]
        brg = np.stack([r[0] for r in res], axis=1)
        dist = np.stack([r[3] for r in res], axis=1)
        srt = np.sort(brg, axis=1)
        gaps = np.stack([srt[:, 1] - srt[:, 0], srt[:, 2] - srt[:, 1], 360 - srt[:, 2] + srt[:, 0]], axis=1)
        spread = 360 - gaps.max(axis=1)
        minsep = gaps.min(axis=1)
        hgt = elev30.ravel()[idx][:, None] + EYE_M
        tops = np.array([p['elev_m'] for p in trio])[None, :]
        up = np.degrees(np.arctan2(tops - hgt - dist ** 2 / (2 * R_EFF), dist))
        good = ((spread >= 55) & (spread <= 100) & (minsep >= 20) & (dist.min(axis=1) > 1200)
                & (dist.max(axis=1) < 7500) & (up.min(axis=1) >= 2.0))
        if not good.any():
            continue
        g = [np.stack([r[1], r[2]], axis=1) for r in res]
        j1, j2 = g[0] - g[1], g[1] - g[2]
        i11 = cinv[0, 0] * j1[:, 0] ** 2 + 2 * cinv[0, 1] * j1[:, 0] * j2[:, 0] + cinv[1, 1] * j2[:, 0] ** 2
        i22 = cinv[0, 0] * j1[:, 1] ** 2 + 2 * cinv[0, 1] * j1[:, 1] * j2[:, 1] + cinv[1, 1] * j2[:, 1] ** 2
        i12 = (cinv[0, 0] * j1[:, 0] * j1[:, 1] + cinv[0, 1] * (j1[:, 0] * j2[:, 1] + j2[:, 0] * j1[:, 1])
               + cinv[1, 1] * j2[:, 0] * j2[:, 1])
        hb = 1.0 / BARO_SIGMA_M ** 2
        i11 = i11 + hb * dh_de[idx] ** 2
        i22 = i22 + hb * dh_dn[idx] ** 2
        i12 = i12 + hb * dh_de[idx] * dh_dn[idx]
        det = i11 * i22 - i12 ** 2
        sigma = np.sqrt(np.where(det > 0, (i11 + i22) / np.maximum(det, 1e-30), np.inf))
        unnamed = sum(not p['verified_name'] for p in trio)
        bonus = (1 + 0.35 * unnamed) * (0.8 if any(p['name'] == meta.get('landmark') for p in trio) else 1.0)
        score = np.where(good, sigma * bonus, np.inf)
        k = int(np.argmin(score))
        if np.isfinite(score[k]) and (best is None or score[k] < best[0]):
            best = (float(score[k]), int(idx[k]), trio, float(sigma[k]), brg[k], dist[k], float(spread[k]))
    assert best is not None, 'no demo spot found'
    _, cell, trio, sigma_pos, brg, dist, spread = best
    e, n = float(obs_e[cell]), float(obs_n[cell])
    ground = float(elev30.ravel()[cell])
    lon, lat = to_lonlat(e, n)
    order = np.argsort(brg if spread < 180 else (brg - brg.min() + 180) % 360)
    mid = trio[order[1]]
    mid_brg = brg[order[1]]
    wall = []
    for k in order:
        p = trio[k]
        d = float(dist[k])
        elev_angle = math.degrees(math.atan2(p['elev_m'] - (ground + EYE_M) - d * d / (2 * R_EFF), d))
        wall.append({'peak_id': p['id'], 'name': p['name'], 'bearing_deg': round(float(brg[k]), 2),
                     'elevation_deg': round(elev_angle, 2), 'distance_m': round(d),
                     'rel_deg': round(((float(brg[k]) - mid_brg + 540) % 360) - 180, 1)})
    first = next((w for w in wall if w['name'] == meta.get('landmark')), wall[1])
    demo = {
        'e_m': round(e, 1), 'n_m': round(n, 1), 'lon': round(lon, 6), 'lat': round(lat, 6),
        'ground_m': round(ground, 1), 'baro_m': round(ground + EYE_M + 5.0, 1),
        'slope_deg': round(float(slope[cell]), 1), 'spread_deg': round(spread, 1),
        'model_sigma_m': round(sigma_pos, 1), 'wall': wall,
        'first_peak_id': first['peak_id'],
        'note': 'Synthetic landing spot chosen for resection geometry. Baro is ground + 1.7 m eye + 5 m error.',
    }
    print(f'demo spot {lat:.5f},{lon:.5f} ground {ground:.0f} m, spread {spread:.0f} deg, model sigma {sigma_pos:.0f} m')
    for w in wall:
        print(f"  wall {w['name']:<22} brg {w['bearing_deg']:6.1f}  rel {w['rel_deg']:+6.1f}  elev {w['elevation_deg']:5.1f}  dist {w['distance_m']} m")

    for p in peaks:
        p['vis_bits'] = bits(vis[p['id']])
    out = {
        'schema': 'ridgeline-grid/v1',
        'area': args.area, 'name': meta['name'], 'region': meta.get('region', ''),
        'bounds': [west, south, east, north],
        'nx': nx, 'ny': ny, 'dx_m': round(dx, 4), 'dy_m': round(dy, 4),
        'row0': 'north', 'north': 'true (grid aligned to meridians)',
        'eye_m': EYE_M, 'k_refraction': K_REFRACTION,
        'declination_deg': meta.get('declination', {}).get('deg'),
        'elev_dm_u16': b64(np.round(elev30 * 10).astype('<u2')),
        'land_bits': bits(land30),
        'peaks': peaks,
        'demo': demo,
        'peak_rule': "Rohan's find_true_peaks in meters: 5 m smoothing, 800 m isolation, >= 2500 m, 1000 m mean, TPI >= 150 m",
        'credits': meta.get('credits', []),
    }
    dest = Path(args.out) if args.out else HERE.parent / 'data' / f'{args.area}.json'
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, separators=(',', ':')), encoding='utf-8', newline='\n')
    print(f'wrote {dest} ({dest.stat().st_size / 1024:.0f} KB) in {time.time() - t0:.1f} s')


if __name__ == '__main__':
    main()
