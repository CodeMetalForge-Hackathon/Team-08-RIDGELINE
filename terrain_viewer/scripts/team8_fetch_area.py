"""Fetch the inputs for a terrain tile anywhere: elevation, land cover, place names, declination.

  python -I scripts/team8_fetch_area.py --id washington --name "Mount Washington" --region "White Mountains, New Hampshire" \
      --center 44.2706 -71.3033 --size-km 10 8 --dem 3dep10
  python -I scripts/team8_fetch_area.py --id fuji --name "Mount Fuji" --region "Shizuoka and Yamanashi, Japan" \
      --center 35.3606 138.7274 --size-km 11 9 --dem cop30
  python -I scripts/team8_fetch_area.py --id moran --name "Mount Moran" --region "Grand Teton National Park, Wyoming" \
      --dem file --dem-file output_USGS1m.tif

Writes <cache>/<id>/ (default .cache/<id>, git-ignored): dem.tif, landcover.tif, places.json, area.json.
Only the windows that cover the tile are read from the cloud-optimized GeoTIFFs, so a 10 km tile is a few MB.

Elevation   3dep10  USGS 3DEP 1/3 arc-second, about 10 m, United States, public domain (AWS prd-tnm)
            cop30   Copernicus DEM GLO-30, about 30 m, worldwide (AWS registry of open data)
            file    a local GeoTIFF in any projection, e.g. a 1 m lidar mosaic (the tile is its footprint)
Land cover  ESA WorldCover 2021 v200, 10 m, CC BY 4.0 (AWS)
Names       Wikidata (peaks, lakes, glaciers with coordinates), CC0
"""
import argparse, json, math, os, sys, time, urllib.parse, urllib.request
import numpy as np
import rasterio
from rasterio.merge import merge
from rasterio.warp import transform_bounds

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from team8_wmm import declination  # noqa: E402

UA = "team8-terrain-prep/1.0 (Code Metal Forge hackathon prototype)"
GDAL_ENV = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
                GDAL_HTTP_USERAGENT=UA, GDAL_HTTP_MAX_RETRY="3", GDAL_HTTP_RETRY_DELAY="2", VSI_CACHE="TRUE")
CREDITS = {
    "3dep10": "USGS 3D Elevation Program 1/3 arc-second DEM, public domain",
    "cop30": "Copernicus DEM GLO-30 (produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence "
             "and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA)",
    "landcover": "ESA WorldCover 10 m 2021 v200 (© ESA WorldCover project 2021, contains modified Copernicus Sentinel data "
                 "processed by the ESA WorldCover consortium), CC BY 4.0",
    "places": "Place names and coordinates from Wikidata, CC0",
}
CLASSES = {  # Wikidata classes kept, and the map kind they become
    "Q8502": "peak", "Q207326": "peak", "Q8072": "peak", "Q169358": "peak", "Q54050": "peak", "Q1595289": "peak",
    "Q23397": "lake", "Q131681": "lake", "Q3253281": "lake", "Q204324": "lake", "Q35666": "glacier", "Q133056": "pass",
}


def log(*a):
    print(f"[{time.time() - T0:5.1f}s]", *a, flush=True)


def m_per_deg(lat):
    p = math.radians(lat)
    return (111132.954 - 559.822 * math.cos(2 * p) + 1.175 * math.cos(4 * p),
            111412.84 * math.cos(p) - 93.5 * math.cos(3 * p) + 0.118 * math.cos(5 * p))


def tiles_1deg(b):
    w, s, e, n = b
    return [(la, lo) for la in range(math.floor(s), math.floor(n - 1e-9) + 1) for lo in range(math.floor(w), math.floor(e - 1e-9) + 1)]


def url_3dep(la, lo):   # named by the north-west corner
    name = f"{'n' if la + 1 >= 0 else 's'}{abs(la + 1):02d}{'w' if lo < 0 else 'e'}{abs(lo):03d}"
    return f"https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/current/{name}/USGS_13_{name}.tif"


def url_cop30(la, lo):  # named by the south-west corner
    t = f"Copernicus_DSM_COG_10_{'N' if la >= 0 else 'S'}{abs(la):02d}_00_{'E' if lo >= 0 else 'W'}{abs(lo):03d}_00_DEM"
    return f"https://copernicus-dem-30m.s3.amazonaws.com/{t}/{t}.tif"


def url_worldcover(la, lo):  # 3 degree tiles named by the south-west corner
    return ("https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/ESA_WorldCover_10m_2021_v200_"
            f"{'N' if la >= 0 else 'S'}{abs(la):02d}{'E' if lo >= 0 else 'W'}{abs(lo):03d}_Map.tif")


def read_window(urls, bounds, path, nodata):
    """Mosaic only the part of the remote tiles that covers bounds and write it as a GeoTIFF."""
    with rasterio.Env(**GDAL_ENV):
        srcs = []
        for u in urls:
            try:
                srcs.append(rasterio.open("/vsicurl/" + u))
            except Exception as ex:  # tiles over open ocean simply do not exist
                log("skip", u.rsplit("/", 1)[-1], type(ex).__name__)
        if not srcs:
            raise SystemExit("no source tile covers this box")
        arr, tf = merge(srcs, bounds=bounds, nodata=nodata)
        prof = dict(driver="GTiff", width=arr.shape[2], height=arr.shape[1], count=1, dtype=arr.dtype, crs=srcs[0].crs,
                    transform=tf, nodata=nodata, compress="deflate", tiled=True)
        res = abs(srcs[0].res[0])
        for s_ in srcs:
            s_.close()
    with rasterio.open(path, "w", **prof) as d:
        d.write(arr[0], 1)
    return arr[0], res


def wikidata(b):
    w, s, e, n = b
    vals = " ".join("wd:" + q for q in CLASSES)
    q = f"""SELECT ?item ?itemLabel ?lat ?lon ?elev ?cls WHERE {{
  SERVICE wikibase:box {{ ?item wdt:P625 ?coord .
    bd:serviceParam wikibase:cornerSouthWest "Point({w} {s})"^^geo:wktLiteral .
    bd:serviceParam wikibase:cornerNorthEast "Point({e} {n})"^^geo:wktLiteral . }}
  ?item wdt:P31 ?cls . VALUES ?cls {{ {vals} }}
  BIND(geof:latitude(?coord) AS ?lat) BIND(geof:longitude(?coord) AS ?lon)
  OPTIONAL {{ ?item p:P2044/psn:P2044/wikibase:amount ?elev }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,mul,ja,de,fr". }} }}"""
    body = urllib.parse.urlencode({"query": q}).encode()
    for attempt in range(4):
        req = urllib.request.Request("https://query.wikidata.org/sparql", data=body, method="POST",
                                     headers={"User-Agent": UA, "Accept": "application/sparql-results+json",
                                              "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                rows = json.load(r)["results"]["bindings"]
            break
        except Exception as ex:
            log("wikidata retry", attempt + 1, type(ex).__name__, getattr(ex, "code", ""))
            time.sleep(10 * (attempt + 1))
    else:
        log("wikidata unavailable, the map will show elevations only")
        return []
    order = ["peak", "glacier", "lake", "pass"]
    out = {}
    for r in rows:
        qid = r["item"]["value"].rsplit("/", 1)[-1]
        name = r["itemLabel"]["value"]
        if name == qid:  # no label in any listed language
            continue
        kind = CLASSES[r["cls"]["value"].rsplit("/", 1)[-1]]
        p = dict(qid=qid, name=name, kind=kind, lat=round(float(r["lat"]["value"]), 6), lon=round(float(r["lon"]["value"]), 6),
                 source="https://www.wikidata.org/wiki/" + qid)
        if "elev" in r:
            p["elev_m"] = round(float(r["elev"]["value"]), 1)
        if qid not in out or order.index(kind) < order.index(out[qid]["kind"]):
            out[qid] = p
    return sorted(out.values(), key=lambda p: (order.index(p["kind"]), p["name"]))


ap = argparse.ArgumentParser()
ap.add_argument("--id", required=True)
ap.add_argument("--name", required=True, help="the landmark people will recognise, e.g. Mount Washington")
ap.add_argument("--region", default="")
ap.add_argument("--center", nargs=2, type=float, metavar=("LAT", "LON"))
ap.add_argument("--size-km", nargs=2, type=float, default=[10.0, 8.0], metavar=("EAST_WEST", "NORTH_SOUTH"))
ap.add_argument("--bbox", nargs=4, type=float, metavar=("WEST", "SOUTH", "EAST", "NORTH"))
ap.add_argument("--dem", choices=["3dep10", "cop30", "file"], default="cop30")
ap.add_argument("--dem-file")
ap.add_argument("--cache", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache"))
ap.add_argument("--places-only", action="store_true", help="only refresh places.json for an area fetched before")
a = ap.parse_args()
T0 = time.time()
out = os.path.join(a.cache, a.id)
os.makedirs(out, exist_ok=True)
if a.places_only:
    area = json.load(open(os.path.join(out, "area.json"), encoding="utf-8"))
    places = wikidata(area["bbox"])
    with open(os.path.join(out, "places.json"), "w", encoding="utf-8") as f:
        json.dump(places, f, ensure_ascii=False, indent=1)
    area["places"]["count"] = len(places)
    with open(os.path.join(out, "area.json"), "w", encoding="utf-8") as f:
        json.dump(area, f, ensure_ascii=False, indent=1)
    log("places", len(places), [p["name"] for p in places if p["kind"] == "peak"][:12])
    sys.exit(0)

if a.dem == "file":
    with rasterio.open(a.dem_file) as ds:
        w, s, e, n = transform_bounds(ds.crs, "EPSG:4326", *ds.bounds, densify_pts=21)
        res = abs(ds.res[0]) * (1.0 if ds.crs.is_projected else 111000.0)
    mlat, mlon = m_per_deg((s + n) / 2)
    bbox = [w + 15 / mlon, s + 15 / mlat, e - 15 / mlon, n - 15 / mlat]   # stay inside the footprint
    dem_info = dict(source="file", path=os.path.abspath(a.dem_file), native_res_m=round(res, 2))
elif a.bbox:
    bbox = list(a.bbox)
else:
    la, lo = a.center
    mlat, mlon = m_per_deg(la)
    hw, hh = a.size_km[0] * 500 / mlon, a.size_km[1] * 500 / mlat
    bbox = [lo - hw, la - hh, lo + hw, la + hh]
bbox = [round(v, 7) for v in bbox]
pad = 0.003
pbox = (bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad)
log("tile", bbox)

if a.dem != "file":
    fn = url_3dep if a.dem == "3dep10" else url_cop30
    urls = [fn(la, lo) for la, lo in tiles_1deg(pbox)]
    z, res_deg = read_window(urls, pbox, os.path.join(out, "dem.tif"), -9999.0)
    zz = z[z > -1000]
    log("dem", a.dem, z.shape, "elev", round(float(zz.min())), "to", round(float(zz.max())))
    dem_info = dict(source=a.dem, urls=urls, native_res_m=round(res_deg * 111000 * math.cos(math.radians((bbox[1] + bbox[3]) / 2)), 1),
                    path=os.path.join(out, "dem.tif"))
dem_info["credit"] = CREDITS.get(a.dem, "local elevation file")

wc_tiles = sorted({(3 * math.floor(la / 3), 3 * math.floor(lo / 3)) for la, lo in tiles_1deg(pbox)})
lc, _ = read_window([url_worldcover(la, lo) for la, lo in wc_tiles], pbox, os.path.join(out, "landcover.tif"), 0)
vals, cnt = np.unique(lc, return_counts=True)
log("landcover classes", dict(zip(vals.tolist(), (cnt / cnt.sum()).round(3).tolist())))

places = wikidata(bbox)
log("places", len(places), [p["name"] for p in places if p["kind"] == "peak"][:12])
with open(os.path.join(out, "places.json"), "w", encoding="utf-8") as f:
    json.dump(places, f, ensure_ascii=False, indent=1)

lat_c, lon_c = (bbox[1] + bbox[3]) / 2, (bbox[0] + bbox[2]) / 2
tm = time.gmtime()
year = tm.tm_year + (tm.tm_yday - 0.5) / 365.25
area = dict(schema="team8-area/v1", id=a.id, name=a.name, region=a.region, bbox=bbox, center=[round(lon_c, 6), round(lat_c, 6)],
            dem=dem_info, landcover=dict(path=os.path.join(out, "landcover.tif"), credit=CREDITS["landcover"]),
            places=dict(path=os.path.join(out, "places.json"), credit=CREDITS["places"], count=len(places)),
            declination=dict(deg=round(declination(lat_c, lon_c, 0.0, year), 2), year=round(year, 2), model="WMM2025"),
            fetched=time.strftime("%Y-%m-%d", tm))
with open(os.path.join(out, "area.json"), "w", encoding="utf-8") as f:
    json.dump(area, f, ensure_ascii=False, indent=1)
log("declination", area["declination"]["deg"], "deg, wrote", out)
