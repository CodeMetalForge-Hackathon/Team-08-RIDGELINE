# Terrain Fix 3D map (Team-8, Code Metal Forge)

One page that shows the GPS-denied land navigation idea in 3D. On the left is a bird's-eye view of the 10 x 8 km Mount Moran area, and on the right is what the hiker should see from the current best guess. The map steps through the four stages of the fix (GPS jammed, barometer, first peak, two more peaks) and ends with a safe-slope route to the rendezvous.

Everything runs offline. The libraries are in `lib/` at fixed versions and the page makes no request outside the local server.

## Run it

From this folder

```
python -I -m http.server 8780 --bind 127.0.0.1
```

then open http://127.0.0.1:8780/ in Chrome or Edge. In Claude Code the same server is the `codemetal-3d-map` entry of `.claude/launch.json`.

Opening `index.html` straight from disk does not work, because the browser blocks reading `state.json` from a file page. The page says so if it happens.

The first load takes a few seconds while the 3D terrain is built (a "Building the 3D terrain" note shows meanwhile). The browser tab has to be visible, since a hidden tab does not draw frames.

## Demo controls

| Control | Key | What it does |
| --- | --- | --- |
| Next, Back | Right and Left arrows (also Space, PageDown, PageUp) | The first Next jams GPS, then steps 1 to 4. The cyan area shrinks and the camera moves in |
| Injury, Healthy 30° or Ankle hurt 18° | I | Changes the red no-go area and the route. At step 4 with 18° there is no safe route, so the page shows "Stay, signal, wait" and the Auto-PACE ladder moves to E |
| Jam GPS or Restore GPS | G | Switches the sensor table and the PACE level. Restoring GPS ends the fix, so jamming it again starts from step 1 |
| Where it works | W | Coverage map, teal where 3 or more of the 10 peaks are in view |
| Reset view | R | Puts both cameras back after dragging |

Drag the map to pan, right-drag (or Ctrl+drag) to turn and tilt it, and scroll to zoom. The eye view can be dragged to look around.

## What each step shows

| Step | 3D map | Eye view |
| --- | --- | --- |
| GPS on | Blue GPS dot, red no-go slopes | View from the GPS position, nothing to point at |
| 1 GPS is jammed | Whole 82 km² tinted cyan | Hidden, "No fix yet" |
| 2 Barometer, 2530 m ±25 m | The contour band that fits, 5.2 km² | Best guess so far, low confidence |
| 3 First peak, 148° | 0.66 km² left, one yellow bearing line | Mt Moran glows with a pillar of light, "Point here next" |
| 4 Two more peaks | 0.12 km², three lines cross, fix 42 m from the hidden true spot, white route 6.7 km | Faces between the route and the three peaks used |

All numbers come from `state.json` and match the sample images made on 2026-11-07 (same model, same random seeds).

## Remake the data

```
python -I export_3d_assets.py <dem.tif> <this folder> --stats <sample stats.json>
```

It takes about 50 s and rewrites `data/*.png` and `state.json`. The model part (grid Bayes filter, von Mises bearings, barometer, next-peak choice, Dijkstra with Tobler speed and a slope limit) is copied from `make_map_preview.py` of 2026-11-07 without changes. The Monte Carlo table is copied from that run's `stats.json` instead of being run again. A re-run on 2026-11-07 gave byte-identical files.

| File | What it is |
| --- | --- |
| `data/terrain_rgb.png` | Elevation as Mapbox terrain-RGB, 1025 x 1025, about 10 m, EPSG:4326. Height = -10000 + (R x 65536 + G x 256 + B) x 0.1 m |
| `data/base.png` | Hillshade and color background, about 5 m |
| `data/prob_0.png` to `prob_3.png` | Where you can be at each stage, transparent cyan |
| `data/nogo_30.png`, `nogo_18.png` | Red where the slope is over 30° or 18°, or a cliff |
| `data/coverage.png` | How many peaks are in view |
| `state.json` | Bounds, peaks, bearing observations, stages (titles, 95% outlines, estimates, eye cameras), routes, truth, rendezvous, sensors, Monte Carlo table, credits and assumptions (schema `team8-3dmap/v1`) |

`data/dem_5m.tif` and `data/dem_10m.tif` were not made by this export and the page does not use them.

## deck.gl 9.3.11 notes

Things that were found by testing in the browser and that the code works around.

1. The bundled terrain loader only works in a worker, so `terrain-worker.js` of loaders.gl 4.4.3 is served from `lib/` and passed as `loadOptions.terrain.workerUrl`.
2. Draped layers that arrive in the same frame as the first TerrainExtension layer never show. An empty "primer" draped layer is there from the start, and the real draped layers are added after the terrain mesh has loaded.
3. FirstPersonView sizes pixel-unit icons and text for objects 1 m away, so markers on peaks 5 km off shrank below one pixel. The eye view sets `focalDistance` to 5000 m and scales each pixel size by distance / 5000.
4. In FirstPersonView, a positive pitch looks down.

## Limits

This is a simulation on the same DEM that the model uses. Pointing error, the difference between the DEM and the real ground, and peaks outside the data are not included, and nothing here was tested in the field. The route time uses Tobler's hiking function off trail.

## Credits and data rules

- Elevation from the USGS 3D Elevation Program (3DEP) 1 m DEM, public domain, U.S. Geological Survey
- Accessed through OpenTopography (opentopography.org), which mosaicked the 3DEP tiles. Projects WY_GrandTetonElkRefuge_2014, WY_GrandTetonNP_D22, WY_North_Converse_2020_D20
- deck.gl 9.3.11, MIT License, Copyright vis.gl contributors (`lib/deck.gl@9.3.11/LICENSE`)
- loaders.gl 4.4.3 terrain worker, MIT License, Copyright vis.gl contributors (`lib/loaders.gl-terrain@4.4.3/LICENSE`)

The 1 m source data (about 222 MB) is not in this folder and must not go into any repository. The page has no images or locations of real people. The "true spot" is a simulated hiker.
