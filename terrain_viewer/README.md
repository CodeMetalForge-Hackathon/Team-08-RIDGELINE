# Team-8 terrain viewer (prototype)

GPS-denied land navigation, judge view. A downed pilot with no GPS is located from pre-loaded terrain,
a barometer, and pointing a phone at Mount Moran, then walked out on slopes that are safe for their condition.
One HTML page, deck.gl 9.3.11 shipped locally, no network and no API keys.

## Open it

* Double-click `index.html`. It reads `data/bundle.js` (images as data URLs), so it works from `file://` offline.
* Or serve the folder: `python -m http.server 8792` in this folder, then open http://localhost:8792/.
* Preview panes that open a local file as a `data:` snapshot (the Claude desktop app's built-in browser does this)
  cannot read the `lib` and `data` folders. The page says so and links to the server address. Use the server there.
* Any window size works. The panels are one layout designed at 1440 x 760 and zoom down together, and under
  760 px wide only the phone and the step bar stay.

## What the judge sees

| Step | Map | Phone (no map) |
| --- | --- | --- |
| 0 GPS lost | whole tile is possible (76.8 km² of land) | GPS OFF |
| 1 Barometer | contour band, 2394 m ±30 m | altitude |
| 2 Bearing | line of sight from Mount Moran, 136° ±5°, wedge | skyline strip with the peak boxed |
| 3 Tilt | elevation angle +21.3° ±1° gives distance, 0.286 km² left | lat, lon, MGRS |
| 4 Route | A* with Tobler time, 30° limit, 4.95 km, about 1 h 49 min | distance, time, arrow |
| 5 Walk | trail animation, dead-reckoning circle that grows and re-fixes | compass tape, arrow, next instruction, live coordinates |
| Reveal | hidden true spot, 36 m from the fix | error in metres |

Toggles: `2D map`, `3D terrain`, `Eye view` (FirstPersonView from the fix, drag to look around),
`Healthy 30°` and `Injured 18°` (injured has no safe route here, so the phone says STAY, SIGNAL, WAIT).
Keys: left and right arrows change the step, space pauses the walk, Q and E turn the phone heading.
Deep links: `?step=5&t=2400&view=3d&health=healthy`, add `still=1` for headless screenshots.

## Rebuild the data

```
python -I scripts/team8_terrain_prep.py <output_USGS1m.tif> data      # about 15 s
python -I scripts/team8_simulate.py data                              # about 45 s (searches a demo spot)
python -I scripts/team8_bundle.py data                                # writes data/bundle.js
```

Needs rasterio, numpy, scipy, Pillow. The 221 MB 1 m mosaic is not in this folder.

Fonts only change if you swap them: `npm pack @fontsource/noto-sans-jp@5.3.1 @fontsource/b612-mono@5.3.0` in an empty folder,
extract each `.tgz` into a folder of the same name, then `python -I scripts/team8_fonts.py <that folder> lib/fonts`.

## Design rules

* The terrain is the subject. Chrome is black and white with 1px hairlines, no shadows, glows or blur, one 8px radius.
* Cyan, gold and red are data colours only: where you can be, the peak you aim at (and its line of sight), danger.
* Labels are small uppercase with wide tracking. The instruments (compass tape, status tags, clock, step numbers) use
  B612 Mono, the typeface Airbus commissioned for cockpit displays. Readouts with decimals use Noto Sans JP with tabular
  figures, because B612 Mono centres the decimal point in a full cell and 0.286 starts to read as two numbers.
* One button style (outlined, white when pressed), `aria-pressed` on toggles, `aria-current` on the active step,
  visible focus rings, a label on the walk slider, and `prefers-reduced-motion` turns off the camera flights and the auto walk.
* No captions under pictures. Credits sit in one footer line.

## Files

| File | Content |
| --- | --- |
| `data/terrain_rgb_5m.png`, `terrain_rgb_10m.png` | Mapbox terrain-RGB, h = -10000 + (R*65536 + G*256 + B) * 0.1, EPSG:4326 grid, edges = `bounds` |
| `data/terrain_color_5m.png` (`.jpg`) | hillshade x slope safety, green under 15°, yellow under 25°, orange under 35°, red 35° and over, water blue, 100 m and 500 m contours |
| `data/lake_mask_5m.png` | hydro-flattened water |
| `data/terrain_meta.json` | bounds [west, south, east, north], grid, decoder, peaks (5 names matched to Wikipedia coordinates within 50 m and 3 m), slope shares, credits |
| `data/prob_1_baro.png`, `prob_2_bearing.png`, `prob_3_elev.png` | 10 m grid, same bounds. R = 255 * sqrt(P / Pmax), G = 255 inside the 95% region, B = 255 inside the 50% region |
| `data/sim.json` | truth (hidden), measurements, stage areas, estimate, routes (`path` [lon, lat, z] and `timestamps` s), phone legs, skyline from the fix, line of position |
| `data/bundle.js` | everything above that the page needs, as one script (10 m mesh, 5 m texture) |
| `lib/deck.gl@9.3.11/` | deck.gl standalone bundle and its MIT license |
| `lib/fonts/fonts.css` | Noto Sans JP (latin, 400 600 700) and B612 Mono (400 700) as data URLs, because Chrome blocks font files on `file://`. OFL texts beside it |

The deck.gl bundle parses terrain only in a web worker downloaded from a CDN. `index.html` therefore
passes its own loader (`GridTerrainLoader`) that decodes the PNG on the main thread and builds a regular
grid mesh (0.8 M vertices from the 10 m image, about 0.1 s). That is what makes it work offline.

## Plugging in the team's code

* Position (Rohan): write each step as a PNG in the `prob_*.png` format on the same bounds, or a float
  grid that `team8_bundle.py` can convert. Stage areas and the estimate go in `sim.json` under `stages` and `estimate`.
* Route: `sim.json` → `routes.healthy` and `routes.injured` (`null` means no safe route), each with
  `path`, `timestamps`, `length_m`, `time_s`, `max_slope_deg`. `legs` holds the phone instructions.
* Noah's dash-leaflet map can use `terrain_color_5m.png` and the prob PNGs as image overlays with `bounds`.

## Model (mock)

Grid Bayes filter on 10 m cells. Barometer Gaussian σ 30 m, bearing Gaussian σ 5° (×0.02 where Mount Moran
is hidden by terrain), elevation angle Gaussian σ 1° with curvature and refraction (k = 0.13). Sensor errors
added on purpose: +12 m, +2.0°, -0.35°. Route: A* with Tobler off-trail speed (×0.6), slope limit on a
30 m smoothed DEM, cliffs (any 5 m patch over 45°) and water blocked, injured walks at half speed.
Dead reckoning on the phone: 4% step-length error and 3° heading bias, circle grows 5% of distance,
re-fix when it grows 90 m past the fix radius.

Not modelled: the same DEM is used for truth and model, peaks outside the tile (the Grand Teton is 8 km south),
magnetic declination (about 10° east, not yet checked), forest blocking the view, real sensor noise.

## Credits

USGS 3DEP 1 m lidar via OpenTopography (WY_GrandTetonElkRefuge_2014, WY_GrandTetonNP_D22,
WY_North_Converse_2020_D20), public domain. Peak names from Wikipedia coordinate boxes (Mount Moran,
Thor Peak, Traverse Peak, Raynolds Peak, Bivouac Peak), unnamed summits shown by elevation.
deck.gl © vis.gl contributors, MIT. Noto Sans JP © Google and B612 Mono © The B612 Project Authors, both SIL OFL 1.1
(via Fontsource). The demo spot is synthetic, no real person or position is stored.
