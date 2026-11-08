"""Magnetic declination from the World Magnetic Model 2025 (NOAA NCEI and BGS, public domain).

A phone without GPS cannot compute true north, because the declination depends on where you are.
The terrain tile carries its own declination instead (it changes by well under 0.1 deg across a 10 km tile).

  python -I team8_wmm.py 43.835 -110.776 [height_km] [decimal_year]    prints the declination
  python -I team8_wmm.py --test                                          checks NOAA's official test values

Coefficients: scripts/wmm/WMM.COF (WMM2025, valid 2025.0 to 2030.0).
"""
import math
import os
import sys

COF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wmm", "WMM.COF")


def _load(path=COF):
    g, h, dg, dh = {}, {}, {}, {}
    with open(path, encoding="ascii") as f:
        epoch = float(f.readline().split()[0])
        for line in f:
            p = line.split()
            if not p or p[0].startswith("9999"):
                break
            n, m = int(p[0]), int(p[1])
            g[n, m], h[n, m], dg[n, m], dh[n, m] = map(float, p[2:6])
    return epoch, g, h, dg, dh


_EPOCH, _G, _H, _DG, _DH = _load()
_NMAX = max(n for n, _ in _G)


def field(lat, lon, h_km=0.0, year=2026.0):
    """Return (X north, Y east, Z down) in nT at a geodetic point."""
    a_wgs, f = 6378.137, 1 / 298.257223563
    e2 = f * (2 - f)
    phi, lam = math.radians(lat), math.radians(lon)
    rc = a_wgs / math.sqrt(1 - e2 * math.sin(phi) ** 2)
    p = (rc + h_km) * math.cos(phi)
    z = (rc * (1 - e2) + h_km) * math.sin(phi)
    r = math.hypot(p, z)
    phis = math.asin(z / r)                          # geocentric latitude
    ct, st = math.sin(phis), math.cos(phis)          # cos and sin of the colatitude
    st = max(st, 1e-12)
    dt = year - _EPOCH
    a = 6371.2
    P = {(0, 0): 1.0}
    dP = {(0, 0): 0.0}
    for n in range(1, _NMAX + 1):
        for m in range(0, n + 1):
            if n == m:
                k = 1.0 if n == 1 else math.sqrt((2 * n - 1) / (2 * n))
                P[n, m] = k * st * P[n - 1, m - 1]
                dP[n, m] = k * (st * dP[n - 1, m - 1] + ct * P[n - 1, m - 1])
            else:
                k1 = math.sqrt(n * n - m * m)
                p2 = P.get((n - 2, m), 0.0)
                d2 = dP.get((n - 2, m), 0.0)
                k2 = math.sqrt((n - 1) ** 2 - m * m) if n - 2 >= m else 0.0
                P[n, m] = ((2 * n - 1) * ct * P[n - 1, m] - k2 * p2) / k1
                dP[n, m] = ((2 * n - 1) * (ct * dP[n - 1, m] - st * P[n - 1, m]) - k2 * d2) / k1
    Xs = Ys = Zs = 0.0
    for n in range(1, _NMAX + 1):
        ar = (a / r) ** (n + 2)
        for m in range(0, n + 1):
            gnm = _G[n, m] + dt * _DG[n, m]
            hnm = _H[n, m] + dt * _DH[n, m]
            cm, sm = math.cos(m * lam), math.sin(m * lam)
            Xs += ar * (gnm * cm + hnm * sm) * dP[n, m]
            Ys += ar * m * (gnm * sm - hnm * cm) * P[n, m] / st
            Zs -= (n + 1) * ar * (gnm * cm + hnm * sm) * P[n, m]
    d = phis - phi
    return Xs * math.cos(d) - Zs * math.sin(d), Ys, Xs * math.sin(d) + Zs * math.cos(d)


def declination(lat, lon, h_km=0.0, year=2026.0):
    """Degrees, east positive (magnetic north lies east of true north)."""
    X, Y, _ = field(lat, lon, h_km, year)
    return math.degrees(math.atan2(Y, X))


def _test():
    path = os.path.join(os.path.dirname(COF), "WMM2025_TestValues.txt")
    rows = [l.split() for l in open(path) if l.strip() and not l.startswith("#")] if os.path.exists(path) else [
        "2025.0 28 89 -121 -99.77".split(), "2025.0 48 80 -96 -29.91".split(), "2025.0 54 82 87 54.89".split(),
        "2025.0 65 43 93 0.50".split(), "2025.0 51 -33 109 -5.49".split(), "2025.0 39 -59 -8 -15.75".split()]
    worst = 0.0
    for r in rows:
        y, hk, la, lo, dref = map(float, r[:5])
        d = declination(la, lo, hk, y)
        worst = max(worst, abs(d - dref))
        print(f"{y:7.1f} {la:6.1f} {lo:7.1f}  ours {d:8.3f}  NOAA {dref:8.2f}")
    print("worst difference deg", round(worst, 3))
    return worst


if __name__ == "__main__":
    if "--test" in sys.argv:
        sys.exit(0 if _test() < 0.01 else 1)
    la, lo = float(sys.argv[1]), float(sys.argv[2])
    hk = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
    yr = float(sys.argv[4]) if len(sys.argv) > 4 else 2026.85
    print(round(declination(la, lo, hk, yr), 2))
