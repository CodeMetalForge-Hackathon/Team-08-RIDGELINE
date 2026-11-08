"""Cursor on Target (CoT 2.0) events for ATAK, built from the RIDGELINE server STATE.

server.py streams these events to ATAK (ATAK connects to it as a plain TCP "TAK Server") and can also
push them to an ATAK TCP input. This module only builds and reads XML, it opens no sockets.

What ATAK gets for one fix
  RIDGELINE-1          the survivor, friendly ground (a-f-G-U-C-I), how m-f, ce = 1 sigma of the fix
  RIDGELINE-1.r95      the 95% circle, an ATAK drawing circle (u-d-c-c)
  RIDGELINE-1.route    the way out, an ATAK drawing polyline (u-d-f)
  RIDGELINE-1.hide-1   HIDE and SIGNAL marks as spot markers (b-m-p-s-m), labeled HIDE 1, SIGNAL 1, ...
  RIDGELINE-1.gps      a simulated GPS reading outside the terrain circle, red spot marker "GPS DISAGREES"
Items that disappear (reset, new fix, fewer marks) are removed with t-x-d-d delete events (__forcedelete).

Sources for the formats
  * CoT Base-Event Schema (PUBLIC RELEASE).xsd: ce is "the one sigma point for a zero mean normal
    (Gaussian) distribution" when it is an error, hae is height above the WGS-84 ellipsoid, and the how
    codes (m-c is "configured", so a computed fix uses m-f "fused").
  * ATAK-CIV (github.com/TAK-Product-Center/atak-civ): takcot/examples "Drawing Shapes - Circle.cot",
    "Drawing Shapes - Free Form.cot", "Marker - Spot.cot", and the readers DrawingCircleImporter
    (ellipse major = minor = radius in meters), EditablePolyline.setPoints (a line is closed when its first
    and last points match), CotDeleteImporter (link uid, relation, type and __forcedelete).
  * commo (atak-civ/commoncommo): a streaming reader splits on "</event>" with a 64 KiB buffer, and an
    inbound TCP input parses everything one connection sends as ONE event.
"""
from __future__ import annotations

import base64
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from array import array
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
GRID_PATH = HERE / 'data' / 'moran.json'

UID = 'RIDGELINE-1'
CALLSIGN = 'RIDGELINE 1'
SURVIVOR_TYPE = 'a-f-G-U-C-I'   # friendly, ground, dismounted (the infantry frame)
HOW_FIX = 'm-f'                 # machine, fused: the Locks, the barometer and the lidar terrain together
HOW_GPS = 'm-g'                 # machine, derived from a GPS receiver
STALE_S = 600.0                 # every send moves stale 10 minutes ahead
FIX_OLD_S = 600.0               # past this age the remarks say the person may have moved
UNKNOWN = 9999999.0             # CoT "no value" for hae, ce and le
R95_SIGMAS = 2.4477             # circular normal: 95% of the probability lies within sqrt(-2 ln 0.05) sigma
EYE_M = 1.7
GEOID_M = -8.7                  # GEOID18 height on the Moran tile (NGS: -8.59 to -8.87 m). HAE = NAVD88 height + this
MIN_LOCKS = 2                   # one Lock does not pin a position
FIX_STATUSES = ('more', 'enough')
ROUTE_MAX_POINTS = 600
EVENT_MAX_CHARS = 48000         # ATAK's stream reader holds 64 KiB, so one event must stay well inside it

ARGB = {'ring': 0xFF00FFFF, 'ring_fill': 0x3300FFFF, 'route': 0xFFFFFF00,
        'HIDE': 0xFF00FF00, 'SIGNAL': 0xFFFF7700, 'GPS': 0xFFFF0000, 'other': 0xFFFFFFFF}

KEEP = object()   # snapshot(): this state does not change what ATAK shows

_BAD_XML = re.compile('[^\t\n\r\x20-퟿-�\U00010000-\U0010ffff]')
_PING = re.compile(rb'''type\s*=\s*["']t-x-c-t["']''')
_KIND = re.compile(r'^[A-Z][A-Z0-9_]{0,11}$')
END = b'</event>'


# ---------- small helpers ----------

def finite(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def text(v, limit: int = 400) -> str:
    """Text safe for XML 1.0 (no control characters or lone surrogates), cut to `limit`."""
    return _BAD_XML.sub('', str(v))[:limit]


def cot_time(t: float) -> str:
    dt = datetime.fromtimestamp(t, timezone.utc)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + f'{dt.microsecond // 1000:03d}Z'


def signed(argb: int) -> int:
    """ARGB as the signed 32-bit int Android (and ATAK's CoT) uses."""
    return argb - (1 << 32) if argb >= 1 << 31 else argb


def fmt_m(m) -> str:
    return f'{m / 1000:.1f} km' if m >= 1000 else f'{round(m)} m'


def fmt_t(s) -> str:
    if s >= 3600:
        return f'{int(s // 3600)} h {round((s % 3600) / 60):02d} min'
    return f'{max(1, round(s / 60))} min'


def fmt_age(s) -> str:
    return 'under 1 min ago' if s < 60 else f'{fmt_t(s)} ago'


def position_key(fix):
    """What makes a new position: the place, its 95% radius and the Lock count. A new route or new
    marks around the same fix do not reset the fix time."""
    return (fix['lat'], fix['lon'], fix['r95'], fix['n']) if fix else None


def split_stream(buf: bytes) -> tuple[list[bytes], bytes]:
    """Complete events in a CoT byte stream, and the unfinished rest (commo splits the same way)."""
    out = []
    while True:
        i = buf.find(END)
        if i < 0:
            return out, buf
        out.append(buf[:i + len(END)])
        buf = buf[i + len(END):]


def is_ping(msg: bytes) -> bool:
    """A TAK client ping (t-x-c-t, not the t-x-c-t-r pong)."""
    return bool(_PING.search(msg))


# ---------- terrain ----------

class Terrain:
    """Ground height in meters (NAVD88, USGS 3DEP) from the 30 m grid in data/moran.json, decoded like solver.js."""

    def __init__(self, path: Path = GRID_PATH):
        self.ok, self.error = False, None
        try:
            grid = json.loads(Path(path).read_text(encoding='utf-8'))
            elev = array('H')
            elev.frombytes(base64.b64decode(grid['elev_dm_u16']))
            if sys.byteorder != 'little':
                elev.byteswap()
            self.nx, self.ny = int(grid['nx']), int(grid['ny'])
            self.west, self.south, self.east, self.north = (float(v) for v in grid['bounds'])
            if len(elev) != self.nx * self.ny:
                raise ValueError('grid size does not match nx * ny')
            self.elev, self.ok = elev, True
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.error = str(error)

    def ground_m(self, lat: float, lon: float):
        """Ground of the 30 m cell holding (lat, lon), or None outside the tile."""
        if not self.ok:
            return None
        c = math.floor((lon - self.west) / (self.east - self.west) * self.nx)
        r = math.floor((self.north - lat) / (self.north - self.south) * self.ny)
        if not (0 <= c < self.nx and 0 <= r < self.ny):
            return None
        v = self.elev[r * self.nx + c]
        return v / 10.0 if v else None


_TERRAIN = None


def terrain() -> Terrain:
    global _TERRAIN
    if _TERRAIN is None:
        _TERRAIN = Terrain()
    return _TERRAIN


# ---------- what ATAK should show ----------

def snapshot(state: dict):
    """The fix to show, from the server STATE: a small plain dict, None to clear ATAK, or KEEP.

    KEEP covers the moments that should not touch ATAK: the phone posts a Lock before it has solved
    (summary null), a mismatch (the phone asks for that card again, the last consistent fix stays),
    the board's simulated drops (no summary).
    """
    if state.get('source') == 'reset':
        return None
    if not (state.get('obs') or {}).get('locks'):
        return None
    s = state.get('phone')
    if not isinstance(s, dict):
        return KEEP
    n = finite(s.get('n'))
    if n is not None and n < MIN_LOCKS:
        return None
    lat, lon, r95 = finite(s.get('lat')), finite(s.get('lon')), finite(s.get('r95_m'))
    if (n is None or s.get('status') not in FIX_STATUSES or lat is None or lon is None or r95 is None
            or not (-90 <= lat <= 90 and -180 <= lon <= 180 and 0 < r95 < 1e6)):
        return KEEP
    mgrs = s.get('mgrs')
    return {
        'lat': round(lat, 7), 'lon': round(lon, 7), 'r95': r95, 'n': int(n), 'status': s['status'],
        'mgrs': text(mgrs, 40) if isinstance(mgrs, str) and mgrs.strip() else None,
        'gps': _gps(s.get('gps')), 'marks': _marks(s.get('marks')), 'route': _route(state.get('route')),
    }


def _gps(g):
    if not isinstance(g, dict):
        return None
    lat, lon = finite(g.get('lat')), finite(g.get('lon'))
    verdict = text(g.get('verdict') or '', 16).strip().upper()
    if lat is None or lon is None or not verdict or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return {'lat': round(lat, 7), 'lon': round(lon, 7), 'verdict': verdict, 'dist_m': finite(g.get('dist_m'))}


def _marks(marks) -> list:
    """HIDE and SIGNAL marks, numbered 1, 2, ... within each kind in rank order."""
    rows = []
    for i, m in enumerate(marks[:24] if isinstance(marks, list) else []):
        if not isinstance(m, dict):
            continue
        kind = text(m.get('kind') or '', 12).strip().upper()
        lat, lon = finite(m.get('lat')), finite(m.get('lon'))
        if not _KIND.match(kind) or lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        rank = finite(m.get('rank'))
        rows.append((kind, rank if rank is not None else 1e9 + i, i, round(lat, 7), round(lon, 7), text(m.get('why') or '', 240)))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    out, count = [], {}
    for kind, _, _, lat, lon, why in rows:
        count[kind] = count.get(kind, 0) + 1
        out.append({'kind': kind, 'num': count[kind], 'lat': lat, 'lon': lon, 'why': why})
    return out


def _route(route):
    """The way out as one line of (lat, lon), from the route the phone planned (paths are [lon, lat])."""
    if not isinstance(route, dict):
        return None
    pts = []
    for run in route.get('runs') or []:
        for p in (run.get('path') or []) if isinstance(run, dict) else []:
            if not isinstance(p, (list, tuple)) or len(p) < 2:
                continue
            lon, lat = finite(p[0]), finite(p[1])
            if lon is None or lat is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
            q = (round(lat, 6), round(lon, 6))
            if not pts or pts[-1] != q:
                pts.append(q)
    pts = thin(pts, ROUTE_MAX_POINTS)
    if len(pts) > 2 and pts[0] == pts[-1]:
        pts.pop()   # ATAK closes (and fills) a line whose first and last points match
    dest = route.get('dest') if isinstance(route.get('dest'), dict) else {}
    return {
        'points': pts if len(pts) >= 2 else [], 'label': text(dest.get('label') or '', 40).strip(),
        'length_m': finite(route.get('length_m')), 'time_s': finite(route.get('time_s')),
        'say': text(route.get('say') or '', 300).strip(), 'stay': bool(route.get('stay')),
        'cap_deg': finite(route.get('cap_deg')), 'reached': route.get('reached') is not False,
    }


def thin(pts: list, limit: int) -> list:
    """At most `limit` points, evenly spaced, keeping the first and the last."""
    if len(pts) <= limit or limit < 2:
        return list(pts)
    step = (len(pts) - 1) / (limit - 1)
    return [pts[round(i * step)] for i in range(limit)]


# ---------- events ----------

def _event(uid: str, kind: str, how: str, lat: float, lon: float, now: float, stale_s: float = STALE_S,
           hae: float = UNKNOWN, ce: float = UNKNOWN, le: float = UNKNOWN):
    t = cot_time(now)
    ev = ET.Element('event', {'version': '2.0', 'uid': uid, 'type': kind, 'how': how,
                              'time': t, 'start': t, 'stale': cot_time(now + stale_s)})
    ET.SubElement(ev, 'point', {'lat': f'{lat:.7f}', 'lon': f'{lon:.7f}', 'hae': f'{hae:.1f}', 'ce': f'{ce:.1f}', 'le': f'{le:.1f}'})
    return ev, ET.SubElement(ev, 'detail')


def xml(ev: ET.Element) -> str:
    return ET.tostring(ev, encoding='unicode')


def survivor_remarks(fix: dict, now: float | None = None, fix_at: float | None = None) -> str:
    parts = [f"Terrain fix from {fix['n']} peaks, no GPS, 95% radius {round(fix['r95'])} m."]
    if fix_at is not None and now is not None:
        # every resend restamps time and stale, so the age has to be said in words
        age = max(0.0, now - fix_at)
        clock = datetime.fromtimestamp(fix_at, timezone.utc).strftime('%H:%MZ')
        parts.append(f'Position as of {clock}, {fmt_age(age)}.')
        if age >= FIX_OLD_S:
            parts.append(f'Older than {round(FIX_OLD_S / 60)} min, the person may have moved since.')
    gps = fix.get('gps')
    if gps and gps['verdict'] == 'REJECTED':
        parts.append('Simulated GPS disagrees with the terrain estimate' + (f", {fmt_m(gps['dist_m'])} from this fix." if gps['dist_m'] is not None else '.') + ' Cause unknown. Either estimate may be wrong.')
    elif gps and gps['verdict'] == 'AGREE':
        parts.append('Simulated GPS is consistent with the terrain estimate' + (f", {fmt_m(gps['dist_m'])} from this fix." if gps['dist_m'] is not None else '.') + ' Agreement does not verify either position.')
    if fix.get('mgrs'):
        parts.append(f"MGRS {fix['mgrs']}.")
    route = fix.get('route')
    if route and route['stay']:
        cap = f" under {round(route['cap_deg'])}°" if route['cap_deg'] is not None else ''
        parts.append(f"No way to {route['label'] or 'the destination'}{cap}, STAY SIGNAL WAIT.")
    elif route and route['points'] and route['length_m'] is not None:
        eta = f", about {fmt_t(route['time_s'])}" if route['time_s'] is not None else ''
        parts.append(f"Way out to {route['label'] or 'the destination'}, {fmt_m(route['length_m'])}{eta}.")
    return ' '.join(parts)


def survivor_event(fix: dict, now: float, ground, fix_at: float | None = None) -> ET.Element:
    hae = ground + EYE_M + GEOID_M if ground is not None else UNKNOWN
    ev, d = _event(UID, SURVIVOR_TYPE, HOW_FIX, fix['lat'], fix['lon'], now, hae=hae, ce=fix['r95'] / R95_SIGMAS)
    ET.SubElement(d, 'contact', {'callsign': CALLSIGN})
    ET.SubElement(d, 'precisionlocation', {'geopointsrc': 'CALC', 'altsrc': 'LIDAR' if ground is not None else '???'})
    ET.SubElement(d, 'remarks').text = survivor_remarks(fix, now, fix_at)
    return ev


def circle_event(fix: dict, now: float) -> ET.Element:
    """The 95% circle in ATAK's own drawing-circle form (takcot/examples/Drawing Shapes - Circle.cot)."""
    uid, r = UID + '.r95', f"{fix['r95']:.1f}"
    ev, d = _event(uid, 'u-d-c-c', HOW_FIX, fix['lat'], fix['lon'], now)
    shape = ET.SubElement(d, 'shape')
    ET.SubElement(shape, 'ellipse', {'major': r, 'minor': r, 'angle': '360'})
    link = ET.SubElement(shape, 'link', {'uid': uid + '.Style', 'type': 'b-x-KmlStyle', 'relation': 'p-c'})
    style = ET.SubElement(link, 'Style')
    line = ET.SubElement(style, 'LineStyle')
    ET.SubElement(line, 'color').text = f"{ARGB['ring']:08x}"
    ET.SubElement(line, 'width').text = '3.0'
    poly = ET.SubElement(style, 'PolyStyle')
    ET.SubElement(poly, 'color').text = f"{ARGB['ring_fill']:08x}"
    ET.SubElement(d, '__shapeExtras', {'cpvis': 'false', 'editable': 'false'})
    ET.SubElement(d, 'strokeColor', {'value': str(signed(ARGB['ring']))})
    ET.SubElement(d, 'strokeWeight', {'value': '3.0'})
    ET.SubElement(d, 'fillColor', {'value': str(signed(ARGB['ring_fill']))})
    ET.SubElement(d, 'contact', {'callsign': f'{CALLSIGN} 95%'})
    ET.SubElement(d, 'remarks').text = f"95% area of the terrain fix, radius {round(fix['r95'])} m."
    ET.SubElement(d, 'labels_on', {'value': 'false'})
    ET.SubElement(d, 'precisionlocation', {'altsrc': '???'})
    return ev


def route_event(route: dict, now: float) -> ET.Element | None:
    """The way out as an ATAK drawing polyline (takcot/examples/Drawing Shapes - Free Form.cot, left open)."""
    pts = route['points']
    while len(pts) >= 2:
        mid = pts[len(pts) // 2]
        ev, d = _event(UID + '.route', 'u-d-f', HOW_FIX, mid[0], mid[1], now)
        for lat, lon in pts:
            ET.SubElement(d, 'link', {'point': f'{lat:.6f},{lon:.6f}'})
        ET.SubElement(d, 'strokeColor', {'value': str(signed(ARGB['route']))})
        ET.SubElement(d, 'strokeWeight', {'value': '4.0'})
        ET.SubElement(d, 'fillColor', {'value': '0'})
        label = route['label'] or 'the destination'
        ET.SubElement(d, 'contact', {'callsign': f'{CALLSIGN} ROUTE to {label}'[:60]})
        bits = [f'Way out to {label}']
        if route['length_m'] is not None:
            bits.append(fmt_m(route['length_m']))
        if route['time_s'] is not None:
            bits.append('about ' + fmt_t(route['time_s']))
        if route['cap_deg'] is not None:
            bits.append(f"slope limit {round(route['cap_deg'])}°")
        remarks = ', '.join(bits) + '.'
        if not route['reached']:
            remarks += f' It ends short of {label}, the rest is too steep.'
        if route['say']:
            remarks += ' ' + route['say']
        ET.SubElement(d, 'remarks').text = remarks
        ET.SubElement(d, 'labels_on', {'value': 'false'})
        ET.SubElement(d, '__shapeExtras', {'cpvis': 'false', 'editable': 'false'})
        if len(xml(ev)) <= EVENT_MAX_CHARS:
            return ev
        pts = thin(pts, len(pts) // 2)
    return None


def spot_event(uid: str, callsign: str, argb: int, lat: float, lon: float, now: float, remarks: str,
               how: str, ground) -> ET.Element:
    """An ATAK spot map marker (takcot/examples/Marker - Spot.cot)."""
    ev, d = _event(uid, 'b-m-p-s-m', how, lat, lon, now, hae=ground + GEOID_M if ground is not None else UNKNOWN)
    color = str(signed(argb))
    ET.SubElement(d, 'status', {'readiness': 'true'})
    ET.SubElement(d, 'contact', {'callsign': callsign})
    ET.SubElement(d, 'remarks').text = remarks
    ET.SubElement(d, 'color', {'argb': color})
    ET.SubElement(d, 'precisionlocation', {'geopointsrc': 'GPS' if how == HOW_GPS else 'CALC',
                                           'altsrc': 'LIDAR' if ground is not None else '???'})
    ET.SubElement(d, 'usericon', {'iconsetpath': f'COT_MAPPING_SPOTMAP/b-m-p-s-m/{color}'})
    return ev


def build(fix, now: float, ground_fn=None, fix_at: float | None = None) -> list[tuple[str, str]]:
    """[(uid, xml)] for a fix from snapshot(), in a stable order. Empty when there is no fix.
    fix_at is when this position was first seen (epoch s), shown as its age in the survivor remarks."""
    if not fix:
        return []
    ground = ground_fn or terrain().ground_m
    out = [(UID, xml(survivor_event(fix, now, ground(fix['lat'], fix['lon']), fix_at))),
           (UID + '.r95', xml(circle_event(fix, now)))]
    route = fix.get('route')
    if route and route['points']:
        ev = route_event(route, now)
        if ev is not None:
            out.append((UID + '.route', xml(ev)))
    for m in fix.get('marks') or []:
        uid = f"{UID}.{m['kind'].lower()}-{m['num']}"
        why = m['why'] or f"{m['kind'].title()} candidate from the terrain around the fix."
        out.append((uid, xml(spot_event(uid, f"{m['kind']} {m['num']}", ARGB.get(m['kind'], ARGB['other']),
                                         m['lat'], m['lon'], now, why, HOW_FIX, ground(m['lat'], m['lon'])))))
    gps = fix.get('gps')
    if gps and gps['verdict'] == 'REJECTED':
        far = f", {fmt_m(gps['dist_m'])} from the terrain fix" if gps['dist_m'] is not None else ''
        out.append((UID + '.gps', xml(spot_event(UID + '.gps', 'GPS DISAGREES', ARGB['GPS'], gps['lat'], gps['lon'], now,
                                                 f'Simulated GPS disagrees{far}, outside the modelled 95% circle. Cause unknown. Either estimate may be wrong. Cross-check before moving.', HOW_GPS,
                                                 ground(gps['lat'], gps['lon'])))))
    return out


def delete_event(uid: str, now: float) -> str:
    """Remove an item from ATAK (CotDeleteImporter: t-x-d-d with a link and __forcedelete)."""
    ev, d = _event(f'{uid}.delete.{int(now * 1000)}', 't-x-d-d', 'm-g', 0.0, 0.0, now, stale_s=60)
    ET.SubElement(d, 'link', {'uid': uid, 'relation': 'none', 'type': 'none'})
    ET.SubElement(d, '__forcedelete')
    return xml(ev)


def pong(now: float) -> str:
    """The answer to a client ping, as TAK Server sends it (uid takPong, type t-x-c-t-r)."""
    ev, _ = _event('takPong', 't-x-c-t-r', 'h-g-i-g-o', 0.0, 0.0, now, stale_s=20, hae=0.0)
    return xml(ev)
