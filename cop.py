"""
Maritime SAR Common Operating Picture - base layer starter.

Run:   python3 cop.py        -> http://127.0.0.1:8050
Feed:  python3 sim_feed.py   (fake drone/vessel/sensor data, with 30 min of backfill)

Data model
  - Assets: latest position + a rolling TRACK (history of positions) per asset id.
  - Sensor overlays (lidar/ir/thermal): every frame is kept (per source id, capped),
    each with its own timestamp.
  - Every sender may include `ts` (unix epoch SECONDS, when the data was captured).
    If omitted, server receive time is used.
  - Side panel: toggle each layer, and a per-layer slider for how far back to show.
    0 = latest only. Older frames/trail segments fade with age.
  - dl.LayersControl is deliberately NOT used (crashes with dash 4.x + dash-leaflet 1.0.15).
"""
import threading
import time

import dash
import dash_leaflet as dl
import requests
from dash import Input, Output, dcc, html
from flask import jsonify, request

# ----------------------------------------------------------------- config
INCIDENT = {"name": "Incident datum", "lat": 42.35, "lon": -70.75}  # Mass Bay placeholder
START_CENTER, START_ZOOM = [INCIDENT["lat"], INCIDENT["lon"]], 8
SENSORS = ["lidar", "ir", "thermal"]
LIVE_LAYERS = ["assets", "paths"] + SENSORS  # checkbox toggles
WINDOW_LAYERS = ["paths"] + SENSORS  # layers that get a history slider
DEFAULT_WINDOW_MIN = {"paths": 15, "lidar": 5, "ir": 5, "thermal": 5}
SLIDER_MAX_MIN = 60
ASSET_COLORS = {"drone": "#00e5ff", "vessel": "#ffd600", "uscg": "#ff5252", "target": "#ff00ff"}
STALE_AFTER_S = 120  # hide an asset marker if not heard from in 2 min
HISTORY_MAX_S = SLIDER_MAX_MIN * 60  # keep data this long
MAX_FRAMES_PER_ID = 60  # per overlay source, protects memory/browser
MAX_PATH_POINTS = 600  # decimate drawn trails beyond this

# ------------------------------------------------------- live data store
LOCK = threading.Lock()
ASSETS: dict = {}  # id -> latest report
TRACKS: dict = {}  # id -> [(ts, lat, lon), ...]
FRAMES: dict = {}  # "id@ts" -> overlay frame

app = dash.Dash(__name__)
server = app.server  # Flask instance


def _as_list(body):
    return body if isinstance(body, list) else [body]


@server.post("/api/assets")
def ingest_asset():
    """{id, type, lat, lon, heading?, label?, ts?}  (one object or a list)"""
    items = _as_list(request.get_json(force=True))
    with LOCK:
        for a in items:
            a["ts"] = float(a.get("ts") or time.time())
            TRACKS.setdefault(a["id"], []).append((a["ts"], a["lat"], a["lon"]))
            if a["id"] not in ASSETS or a["ts"] >= ASSETS[a["id"]]["ts"]:
                ASSETS[a["id"]] = a
    return jsonify(ok=True, n=len(items))


@server.post("/api/overlays")
def ingest_overlay():
    """{id, sensor: lidar|ir|thermal, url, bounds: [[south, west], [north, east]], opacity?, ts?}
    `url` = georeferenced PNG (http URL or data: URI). One object or a list.
    Each POST is a new frame; the same id with different ts builds up history."""
    items = _as_list(request.get_json(force=True))
    with LOCK:
        for o in items:
            if o.get("sensor") not in SENSORS:
                return jsonify(ok=False, error=f"sensor must be one of {SENSORS}"), 400
            o["ts"] = float(o.get("ts") or time.time())
            FRAMES[f'{o["id"]}@{o["ts"]}'] = o
    return jsonify(ok=True, n=len(items))


@server.get("/api/state")
def state():
    with LOCK:
        return jsonify(assets=list(ASSETS.values()), n_frames=len(FRAMES))


def prune(now):
    """Call with LOCK held."""
    for k in [k for k, a in ASSETS.items() if now - a["ts"] > HISTORY_MAX_S]:
        del ASSETS[k]
    for k, tr in list(TRACKS.items()):
        TRACKS[k] = [p for p in tr if now - p[0] <= HISTORY_MAX_S]
    by_id: dict = {}
    for k, f in list(FRAMES.items()):
        if now - f["ts"] > HISTORY_MAX_S:
            del FRAMES[k]
        else:
            by_id.setdefault(f["id"], []).append((f["ts"], k))
    for lst in by_id.values():  # cap frames per source, drop oldest
        for _, k in sorted(lst)[:-MAX_FRAMES_PER_ID]:
            del FRAMES[k]


# ------------------------------------------------- environmental data
_cond_cache = {"t": 0, "data": None}


def marine_conditions(lat, lon):
    """Open-Meteo Marine API (free, no key). Cached 10 min."""
    if time.time() - _cond_cache["t"] < 600 and _cond_cache["data"]:
        return _cond_cache["data"]
    try:
        r = requests.get(
            "https://marine-api.open-meteo.com/v1/marine",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "wave_height,wave_direction,wave_period,"
                "ocean_current_velocity,ocean_current_direction,sea_surface_temperature",
            },
            timeout=6,
        )
        r.raise_for_status()
        _cond_cache.update(t=time.time(), data=r.json().get("current", {}))
    except Exception as e:  # keep the UI alive if the API is down
        return {"error": str(e)}
    return _cond_cache["data"]


# ------------------------------------------------------------ map layers
BASES = {
    "Esri Ocean": lambda: dl.TileLayer(
        url="https://server.arcgisonline.com/ArcGIS/rest/services/Ocean/World_Ocean_Base/MapServer/tile/{z}/{y}/{x}",
        attribution="Esri, GEBCO, NOAA",
        maxZoom=13,
    ),
    "GEBCO bathymetry": lambda: dl.WMSTileLayer(
        url="https://wms.gebco.net/mapserv?", layers="GEBCO_LATEST", format="image/png"
    ),
    "Satellite": lambda: dl.TileLayer(
        url="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attribution="Esri",
    ),
    "Dark (low glare)": lambda: dl.TileLayer(
        url="https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png", attribution="CARTO / OSM"
    ),
}

ENV = {
    "Seamarks / nav aids": lambda: dl.TileLayer(
        url="https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png", attribution="OpenSeaMap"
    ),
    "NOAA radar (US)": lambda: dl.WMSTileLayer(
        url="https://nowcoast.noaa.gov/geoserver/weather_radar/wms",
        layers="base_reflectivity_mosaic",
        format="image/png",
        transparent=True,
        opacity=0.6,
    ),
}


def window_slider(name):
    return html.Div(
        style={"margin": "4px 0 14px"},
        children=[
            html.Div(f"{name.upper()} history (min back)", style={"fontSize": 12, "opacity": 0.8}),
            dcc.Slider(
                id=f"win-{name}",
                min=0,
                max=SLIDER_MAX_MIN,
                step=1,
                value=DEFAULT_WINDOW_MIN[name],
                marks={0: "latest", 15: "15", 30: "30", 60: "60"},
                tooltip={"placement": "bottom", "always_visible": False},
            ),
        ],
    )


app.layout = html.Div(
    style={"height": "100vh", "display": "flex", "fontFamily": "system-ui"},
    children=[
        dl.Map(
            id="map",
            center=START_CENTER,
            zoom=START_ZOOM,
            style={"flex": 1},
            children=[
                dl.LayerGroup(id="base-layer"),
                dl.LayerGroup(id="env-layer"),
                *[dl.LayerGroup(id=f"{name}-layer") for name in SENSORS],  # imagery under...
                dl.LayerGroup(id="paths-layer"),
                dl.LayerGroup(id="assets-layer"),  # ...markers on top
                dl.ScaleControl(position="bottomleft"),
                dl.CircleMarker(
                    center=[INCIDENT["lat"], INCIDENT["lon"]],
                    radius=8,
                    color="#ff1744",
                    children=dl.Tooltip(INCIDENT["name"]),
                ),
            ],
        ),
        html.Div(
            style={"width": 300, "padding": 12, "background": "#111", "color": "#eee", "overflowY": "auto"},
            children=[
                html.H3("COP status"),
                html.Div(id="conditions"),
                html.Hr(),
                html.B("Base map"),
                dcc.RadioItems(
                    id="base-choice",
                    options=list(BASES),
                    value="Esri Ocean",
                    style={"display": "grid", "gap": 4, "margin": "6px 0"},
                ),
                html.B("Environment"),
                dcc.Checklist(
                    id="env-choice",
                    options=list(ENV),
                    value=["Seamarks / nav aids"],
                    style={"display": "grid", "gap": 4, "margin": "6px 0"},
                ),
                html.B("Live layers"),
                dcc.Checklist(
                    id="toggles",
                    options=[{"label": f" {n.upper()}", "value": n} for n in LIVE_LAYERS],
                    value=LIVE_LAYERS,
                    style={"display": "grid", "gap": 4, "margin": "6px 0"},
                ),
                html.Hr(),
                html.B("History windows"),
                html.Div("0 = latest only. Older data fades with age.", style={"fontSize": 12, "opacity": 0.7}),
                html.Div(style={"height": 8}),
                *[window_slider(n) for n in WINDOW_LAYERS],
                html.Hr(),
                html.Div(id="asset-list"),
            ],
        ),
        dcc.Interval(id="tick", interval=2000),
        dcc.Interval(id="cond-tick", interval=600_000),
    ],
)


# ------------------------------------------------------------- callbacks
@app.callback(Output("base-layer", "children"), Input("base-choice", "value"))
def set_base(choice):
    return [BASES[choice]()]


@app.callback(Output("env-layer", "children"), Input("env-choice", "value"))
def set_env(chosen):
    return [ENV[n]() for n in (chosen or [])]


def select_frames(frames, window_s, now):
    """-> [(frame, fade 0..1)], oldest first. window_s <= 0 => latest frame per source only."""
    by_id: dict = {}
    for f in frames:
        by_id.setdefault(f["id"], []).append(f)
    out = []
    for fl in by_id.values():
        fl.sort(key=lambda f: f["ts"])
        if window_s <= 0:
            out.append((fl[-1], 1.0))
        else:
            out += [(f, 1 - 0.7 * (now - f["ts"]) / window_s) for f in fl if now - f["ts"] <= window_s]
    return sorted(out, key=lambda x: x[0]["ts"])


@app.callback(
    Output("assets-layer", "children"),
    Output("paths-layer", "children"),
    *[Output(f"{s}-layer", "children") for s in SENSORS],
    Output("asset-list", "children"),
    Input("tick", "n_intervals"),
    Input("toggles", "value"),
    *[Input(f"win-{n}", "value") for n in WINDOW_LAYERS],
)
def refresh(_, on, *windows):
    on = on or []
    win_s = {n: (w or 0) * 60 for n, w in zip(WINDOW_LAYERS, windows)}
    now = time.time()
    with LOCK:
        prune(now)
        assets = [a for a in ASSETS.values() if now - a["ts"] <= STALE_AFTER_S]
        tracks = {k: list(v) for k, v in TRACKS.items()} if "paths" in on else {}
        frames = [f for f in FRAMES.values() if f["sensor"] in on]
        type_of = {a["id"]: a.get("type") for a in ASSETS.values()}

    markers = [
        dl.CircleMarker(
            center=[a["lat"], a["lon"]],
            radius=7,
            color=ASSET_COLORS.get(a.get("type"), "#fff"),
            fillOpacity=0.9,
            children=dl.Tooltip(f'{a.get("label", a["id"])} ({a.get("type")}) hdg {a.get("heading", "-")}'),
        )
        for a in assets
    ]

    paths = []
    for aid, pts in tracks.items():
        w = win_s["paths"]
        if w <= 0:
            continue  # 0 = latest only: no trail
        pts = sorted(p for p in pts if now - p[0] <= w)
        if len(pts) < 2:
            continue
        step = max(1, len(pts) // MAX_PATH_POINTS)
        pts = pts[::step] + ([pts[-1]] if (len(pts) - 1) % step else [])
        paths.append(
            dl.Polyline(
                positions=[[p[1], p[2]] for p in pts],
                color=ASSET_COLORS.get(type_of.get(aid), "#fff"),
                weight=2,
                opacity=0.75,
            )
        )

    per_sensor = {s: [] for s in SENSORS}
    for s in SENSORS:
        sf = [f for f in frames if f["sensor"] == s]
        for f, fade in select_frames(sf, win_s[s], now):
            per_sensor[s].append(
                dl.ImageOverlay(url=f["url"], bounds=f["bounds"], opacity=f.get("opacity", 0.7) * max(fade, 0.15))
            )

    listing = [html.Div(f'{a.get("label", a["id"])}: {a["lat"]:.4f}, {a["lon"]:.4f}') for a in assets]
    return (
        markers if "assets" in on else [],
        paths if "paths" in on else [],
        *[per_sensor[s] if s in on else [] for s in SENSORS],
        listing or "No live assets",
    )


@app.callback(Output("conditions", "children"), Input("cond-tick", "n_intervals"))
def conditions(_):
    c = marine_conditions(INCIDENT["lat"], INCIDENT["lon"])
    if "error" in c:
        return f'Marine data unavailable: {c["error"]}'
    rows = [
        ("Wave height (m)", c.get("wave_height")),
        ("Wave dir (deg)", c.get("wave_direction")),
        ("Current speed", c.get("ocean_current_velocity")),
        ("Current dir (deg)", c.get("ocean_current_direction")),
        ("Sea temp (C)", c.get("sea_surface_temperature")),
    ]
    return [html.Div(f"{k}: {v}") for k, v in rows]


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8050)
