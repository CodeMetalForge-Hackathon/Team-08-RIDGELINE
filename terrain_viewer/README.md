# Team-8 terrain viewer (prototype)

GPS-denied land navigation, judge view. A person with no GPS is located from a pre-loaded map tile, a barometer,
and the angles between three summits (phone gyro plus tilt, no compass), then walked out on slopes that are safe
for their condition. One HTML page, deck.gl 9.3.11 shipped locally, no network and no API keys at run time.

Click anywhere on the map, at any step, to drop a pin. The route from the fix appears at once, with its distance and
walking time on the pin, planned in the browser around water, cliffs and slopes over the limit. Press `Move the pilot`
(or Shift-click) and click land to put the pilot somewhere else: the page works out what the phone would read at that
spot, with noise, and solves the fix from those readings alone in about 0.2 s, in the browser.

The map is painted in the team's relief style, the same as Noah's route plots (elevation tint, relief shading, water,
contours, a 1 km UTM grid), with the collar of a printed sheet (scale bar and declination), and any mountain on Earth
can be added with five commands.

![Three peaks, Mount Moran, step 4](screenshots/01_fix_3d.png)

## Open it

* Double-click `index.html`. It reads `team8_nav.js`, `data/areas.js` and `data/<area>/bundle.js` (images as data URLs), so it works from `file://` offline.
* Or serve the folder with `python -m http.server 8792` in this folder, then open http://localhost:8792/.
* Pick the mountain in the sidebar title, or open `?area=moran`, `?area=washington`, `?area=fuji`.
* Any window size works. The panels are one layout designed at 1440 x 760 and zoom down together. Under 760 px wide
  only the phone and the step bar stay.

## Add a mountain

```
python -I scripts/team8_fetch_area.py --id rainier --name "Mount Rainier" --region "Washington, USA" --center 46.8529 -121.7604 --size-km 10 8 --dem 3dep10
python -I scripts/team8_terrain_prep.py .cache/rainier data/rainier
python -I scripts/team8_simulate.py data/rainier
python -I scripts/team8_masks.py data/rainier
python -I scripts/team8_bundle.py data/rainier
```

`--dem 3dep10` is the USGS 1/3 arc-second DEM (United States), `--dem cop30` is Copernicus GLO-30 (anywhere),
and `--dem file --dem-file <tif>` takes a local GeoTIFF such as a 1 m lidar mosaic. The fetch step reads only the
needed window from cloud-optimised GeoTIFFs, plus ESA WorldCover land cover and Wikidata names inside the box.
The magnetic declination for the tile is computed offline with WMM2025 (`scripts/team8_wmm.py --test` checks it
against the NOAA test values). Simulate takes about a minute (`TEAM8_MC=50` makes it faster). Masks writes
`nogo.png` (water and cliffs) for the route planner in the browser. Bundle rewrites `data/areas.js`, so the new tile
appears in the picker. Needs rasterio, numpy, scipy, contourpy and Pillow. Downloads are cached in `.cache/`, which git ignores.

## What the judge sees

| Step | Map | Phone (no map) |
| --- | --- | --- |
| 0 GPS lost | the whole tile is possible, the red ring is where the pilot really is (the phone never sees it) | no service, no fix |
| 1 Altitude | one contour band, barometer ±30 m | altitude |
| 2 Peak A | aim at the first summit and press. The tilt gives its distance, a pencil ring | skyline strip, the peak boxed, an arrow to it |
| 3 Peak B | the angle from A to B on the gyro. The heading is unknown, so only angles between peaks count | the next peak boxed |
| 4 Peak C | the third angle, the pencil lines cross, the fix as ±r m with the meter A B C | lat, lon, MGRS |
| 5 Go to | a click anywhere drops the pin, from any step. The pin shows the distance and walking time, the route is coloured by how hard each stretch is. A pin in a lake or on an island stops at the closest shore and says so, with no line across the water | distance, time, arrow |
| 6 Walk | trail, breadcrumbs, a dead-reckoning circle that grows and re-fixes | compass tape, arrow, next instruction |
| Reveal | the true spot and its distance from the fix | error in metres |

* **Move the pilot.** Press `Move the pilot` (or Shift-click) and click land: the pilot comes down there and the
  readings come in one by one. `Demo spot`, `Spot 2` and `Spot 3` are fixed spots for a safe live demo (Spot 2 and 3
  are picked after load: three peaks spread at least 60° and a fix that holds up).
* **Narrow screens.** Under 760 px wide the phone covers the map, so the `Map` button at the end of the steps swaps
  the phone for the map, the hint and the pin.
* **The meter.** A B C fill as the peaks are aimed at. The chip says `Enough peaks`, `aim at one more`, or how much
  of the probability sits on another spot.
* **Aim ±1° 2° 4°.** Re-lands the same spot with a steadier or shakier hand.
* **Goal.** A click in steps 5 and 6 sets it. Water and cliffs are blocked, an unreachable spot gets the nearest
  reachable point and a thin red line. `Hold to lock` (0.7 s) stops a stray tap from moving it, unlocking takes
  two taps. `Back to start` routes back to the first fix (from where you are, while walking).
* **Mark here.** Saves the spot with its own error circle. A note for the user, never used for the fix.
* `Healthy 30°` and `Injured 18°` (half speed). Where no safe route exists the phone says STAY, SIGNAL, WAIT.
* `Map`, `3D`, `Eye level` (first person from the fix, drag to look around). Keys are the left and right arrows for
  the step, space to pause the walk, Q and E to turn the phone.
* Deep links such as `?area=fuji&step=5&view=3d&health=healthy`, `land=lon,lat`, `dest=lon,lat`, `aim=1`, `t=2400`
  (seconds into the walk), plus `still=1` for headless screenshots.

## Test result (browser solver, random drops with three peaks in view, aim ±2°)

| Tile | Elevation data | Demo spot | Truth inside 95% area | Median error | p90 | Three peaks in view from |
| --- | --- | --- | --- | --- | --- | --- |
| Mount Moran | USGS 1 m lidar, shown at 5 m | 137 m off, ±350 m | 98% of 120 | 81 m | 256 m | not measured |
| Mount Washington | USGS 1/3 arc-second | 36 m off, ±81 m | 96% of 80 | 93 m | 299 m | 18% of the land |
| Mount Fuji | Copernicus GLO-30 | 113 m off, ±167 m (two peaks) | 99% of 80 | 56 m | 153 m | 7% (two peaks: 95% inside) |

The demo route on Mount Moran is 6.6 km, 2 h 20 min healthy. No route exists with an injured ankle from the demo spot.

## Model

* Grid Bayes on about 20 m cells over the whole tile, in `team8_nav.js`.
* Barometer Gaussian σ 30 m. Tilt to each summit Gaussian σ 1°, with curvature and refraction (k = 0.13).
* Gyro angles between summits Gaussian σ 2°. The gyro heading is unknown and is integrated out (the residuals are
  taken about their mean), so a bad compass cannot pull the fix. The compass is used only when one peak is in view
  (σ 5° after the declination stored in the tile).
* A summit that the coarse grid calls hidden from a cell makes that cell 0.3 times as likely, not impossible.
  With 0.02 the truth fell outside the 95% area on 9% of random drops.
* The three peaks are chosen by geometry (Fisher information with the heading as a nuisance), unnamed summits pay a penalty.
* The 95% and 50% areas are the highest-probability cells. `±r m` is the radius of a circle with the area of the
  main connected part, and the rest is reported as probability on another spot.
* Route by Dijkstra over the working grid with Tobler off-trail speed (x 0.6), a slope limit (30° healthy, 18°
  injured at half speed), water and cliffs (> 45°) blocked from `nogo.png`. Stretches are easy up to 10°, moderate
  up to 20°, steep above.
* Dead reckoning on the phone, 4% step-length error and 3° heading bias, the circle grows 5% of the distance and
  re-fixes when it grows 90 m past the fix radius.

Not modelled yet. The same DEM is used for truth and model, forest and night hide summits, real sensor noise,
summits outside the tile.

## Design rules

* One map style for the whole team. The texture is painted like Noah's route plots (`scripts/team8_relief.py`,
  from `plotting.py` on `Sprint2-NavAlgorithm`): elevation tint from green valleys through tan and brown to grey
  summits between the 2nd and 98th percentile, soft relief shading lit from the north-west (vertical exaggeration 2),
  blue water with italic names (only water is blue), dark contours with bold index lines and elevation labels along
  the line, a white 1 km UTM grid with its digits on the edge, summits as black triangles with names and heights.
* The collar sits at the bottom of the sidebar, as on a paper map. Scale bar, contour interval, UTM zone and
  100 km square, and a declination diagram that turns with the map.
* Plotting follows the paper habit of land navigation. A pencil line from each aimed summit, the fix as a circle with a dot.
* The camera is fitted with the real 3D projection, so the aimed summits, the whole 95% area and the landing stay
  between the panels, and it turns around a point at terrain height, not sea level.
* The phone stays a dark phone, because that is what the person in the field holds.
* One button style, `aria-pressed` on toggles, `aria-current` on the active step, visible focus rings, a label on
  the walk slider, and `prefers-reduced-motion` turns off the camera flights, the count-down and the auto walk.

## Files

| File | Content |
| --- | --- |
| `index.html` | the page, its styles and its code |
| `team8_nav.js` | the solver and the route planner that run in the browser (`sight`, `solve`, `reach`, `pathTo`, `buildRoute`, `describe`) |
| `data/areas.js` | the list of tiles the page can open |
| `data/<area>/bundle.js` | everything one tile needs, as one script (mesh PNG, map JPEG, slope PNG, nogo PNG, probability PNGs, meta, sim) |
| `data/<area>/nogo.png` | water (red) and cliffs (green) on the working grid, from `scripts/team8_masks.py` |
| `data/<area>/terrain_meta.json` | bounds, grid, decoder, contours and their labels, UTM grid ticks, declination, peaks, lake and glacier names, credits |
| `data/<area>/terrain_rgb.png`, `terrain_rgb_mesh.png` | Mapbox terrain-RGB, h = -10000 + (R*65536 + G*256 + B) * 0.1, EPSG:4326 grid, edges = `bounds` |
| `data/<area>/topo.jpg` | the map texture in the team's relief style (elevation tint, relief shading, water, contours, 1 km grid), from `scripts/team8_relief.py`; the tint range is in `terrain_meta.json` as `map_style` |
| `data/<area>/slope.png` | slope in degrees x 2 on a 30 m smoothed surface. The route planner and the red hatching read the same file |
| `data/<area>/sim.json` | the demo spot (truth), the Python run of the first version, the Monte Carlo test of that version |
| `scripts/` | fetch, prep, relief (prep calls it; run it alone to repaint a prepared tile), simulate, masks, bundle, the WMM2025 model and its coefficients, the font packer |
| `lib/deck.gl@9.3.11/` | deck.gl standalone bundle and its MIT license |
| `lib/fonts/fonts.css` | Noto Sans JP and B612 Mono as data URLs, because Chrome blocks font files on `file://` |
| `screenshots/` | 1600 x 900 headless captures |
| `tests/nav_water.test.mjs` | nothing the route planner draws lies on water, for pins in lakes, on islands, on shores and on land, on all three tiles. Run `node --test terrain_viewer/tests/nav_water.test.mjs` from the repo root (about 25 s) |
| `tests/nav_harness.mjs` | loads `team8_nav.js` and a tile's `bundle.js` in Node, with a small PNG decoder in place of the browser canvas |

The first version (one tile, dark theme, PR #2), the one-summit version (PR #4) and the printed-sheet colours (land
cover tints and brown contours, up to PR #5) stay in the git history.

`index.html` draws the terrain unlit (`material: {unlit: true}`), because the shading is already in the map picture.
In deck.gl 9, `material: false` still lights the mesh with the default material, which darkened the map and shaded it twice.

The deck.gl bundle parses terrain only in a web worker downloaded from a CDN. `index.html` therefore passes its own
loader (`GridTerrainLoader`) that decodes the PNG on the main thread and builds a regular grid mesh. That is what
makes it work offline.

## Plugging in the team's code

* Position. `team8_nav.js` takes the readings as `solve({baro, sb, sa, se, peaks: [{lon, lat, elev_m, m, e}], compass})`
  (m = gyro reading in degrees, e = tilt in degrees) and returns the stages with their probability grids. A phone
  app can send the same readings and draw the same result.
* Route. `reach(lon, lat, capDeg, speed)` once per start, then `pathTo` and `buildRoute` per goal.
* Any web map can use `topo.jpg` and `nogo.png` as image overlays with `bounds`.

## Credits

* Mount Moran elevation, USGS 3DEP 1 m lidar via OpenTopography (WY_GrandTetonElkRefuge_2014, WY_GrandTetonNP_D22,
  WY_North_Converse_2020_D20), public domain.
* Mount Washington elevation, USGS 3DEP 1/3 arc-second DEM, public domain.
* Mount Fuji elevation, Copernicus DEM GLO-30, © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018,
  provided under COPERNICUS by the European Union and ESA, all rights reserved.
* Land cover, ESA WorldCover 10 m 2021 v200, © ESA WorldCover project 2021, contains modified Copernicus Sentinel data (2021)
  processed by the ESA WorldCover consortium, CC BY 4.0.
* Summit, lake and glacier names from Wikidata, CC0.
* Declination from the World Magnetic Model WMM2025, NOAA NCEI and the British Geological Survey, public domain.
* deck.gl © vis.gl contributors, MIT. Noto Sans JP © Google and B612 Mono © The B612 Project Authors, both SIL OFL 1.1
  (via Fontsource).
* The demo spots are synthetic, no real person or position is stored.
