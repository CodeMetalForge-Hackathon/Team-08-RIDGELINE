# Team 8 (project name TBD)

Code Metal Forge Hackathon 2026, Operation Lost and Found.
Search and rescue in dangerous environments, built Nov 7 to Nov 8, 2026 at BASE-X Boston.

> This README is a skeleton. The org rules require every section below, so fill in the TBD parts as the build moves. Ideas under discussion live in [Plan.md](Plan.md).

## What it does

TBD. One or two sentences, written for a responder, not an engineer.

| Item | Answer |
| --- | --- |
| Who uses it | TBD (e.g. a ground search team, a lost hiker) |
| Which objective | TBD (OBJ.01 Locate, OBJ.02 Detect, OBJ.03 Map, OBJ.04 Deliver) |
| What breaks today | TBD (no GPS, no cell, congested radio, smoke, darkness) |
| What we show working | TBD |

## Team

| Name | GitHub | Role |
| --- | --- | --- |
| Noah Gruenert | @NoahGruenert | TBD |
| TBD | @Rshah2067 | TBD |
| TBD | @theDclilly | TBD |
| Seika Karamatsu | @sese0318 | Design, operator interviews, demo story |

## Hardware used

| Hardware | Where it came from | What it does in the system |
| --- | --- | --- |
| Raspberry Pi 5 (baseline kit) | Event kit | TBD |
| TBD | Shared hardware library | TBD |

## How it works

TBD. Add a simple diagram (sensor, edge device, radio link, responder screen).

## Setup and build

```bash
# TBD, exact commands to go from a fresh clone to a running system
```

## How to run the demo

1. TBD
2. TBD
3. TBD

## Live demo

RIDGELINE on the phone and the judge's topo map show the same live solve. You need Python 3 and a browser. Nothing to install, no network, no API keys.

```bash
python tools/live_server.py              # serves the repo on http://localhost:8768
```

1. Judge screen: open http://localhost:8768/terrain_viewer/?area=moran
2. Phone screen: open http://localhost:8768/mockups/phone_screen/ (desktop Chrome with the keyboard works). Press **J** to turn GPS off and **Enter** on Find my position.
3. Aim with the arrow keys (Shift for fine steps) until the hint says *On the summit*, then press **Enter** to Lock. Each Lock shows up on the judge screen within a second: a pencil line from the peak, the triangle of error once three lines cross, and the 95% bubble.
4. After three peaks the phone says **Enough** and *You are here ± N m (95%)*. Click **Reveal** on the judge screen to see the error against the hidden demo spot. **Enter** on the phone uses the fix.
5. Wrong aim: lock while aiming at a different peak. The phone names the lock that does not fit and its line turns red on the judge screen. Aim again and Lock (it replaces that lock), or press Re-aim. **Backspace** removes the last lock.
6. Where to go: after **Use this fix** the phone offers the home point (the first fix, the landing spot), the saved points, or a pin on its map. Tap the map to drop a pin, press and hold to lock it, unlock with **Unlock pin** and then **Unlock**. With the keyboard, focus the map, move the cursor with the arrow keys, Enter drops the pin, hold Enter to lock it. The route keeps off water and off ground steeper than 30° (Able) or 18° (Injured, half speed), is coloured easy, moderate and steep, and says why it goes that way in one sentence. If there is no safe way the phone says **Stay. Signal. Wait.** The judge screen draws the same route.

Phone on the same Wi-Fi: run `python tools/live_server.py --lan` and open `http://<laptop IP>:8768/mockups/phone_screen/`. Phone motion sensors need a secure page, so add `--cert cert.pem --key key.pem` (any self-signed certificate) and open `https://` instead. This path has not been tested on a real phone.

How it works. The phone sends headings in its own frame, so only the differences between peaks count and no compass, magnetic error or declination is involved. `shared/solver.js` (plain ES module, no dependencies, same file in the browser and in Node) scores every 30 m cell inside the barometer band that sees all locked peaks. It removes the unknown heading offset with the circular mean of the residuals and scores azimuth (σ 3°) and elevation angle (σ 1.5°) with a Gaussian. It returns the best cell, the 95% region (area and equivalent radius), Enough or One more (95% radius under 150 m, at least 3 locks, one patch), the best next peak, and which lock to re-aim when the fit is poor. `terrain_viewer/data/moran/solver_grid.json` holds the 30 m grid, 15 peaks (same rules as `find_true_peaks`), a line-of-sight mask per peak (1.7 m eye height, earth curvature with refraction), and three synthetic saved points (the team's rendezvous and two lake shores). `shared/route.js` (same rules) plans the route with the judge screen's model: Dijkstra on the 30 m grid, Tobler's hiking time off trail, no water, nothing over the slope limit. `tools/live_server.py` (standard library only) keeps the latest phone state in memory: the phone POSTs `/lock` after every Lock or route change, the viewer polls `GET /state` every 0.5 s (`terrain_viewer/team8_live.js`).

```bash
node --test tests/solver.test.mjs          # 200 noisy trials, wrong-aim test, speed; writes terrain_viewer/data/moran/solver_eval.json
node --test tests/route.test.mjs           # routes to the saved points, able and injured, never through water or over the limit
node tests/live_e2e.mjs                    # phone + server + viewer in headless Chrome, driven by key presses (CHROME=<path> if needed)
node mockups/phone_screen/verify_ui.mjs    # phone UI logic checks
cd terrain_viewer && python -I scripts/team8_solver_grid.py data/moran   # rebuild solver_grid.json (numpy, scipy, Pillow), about 20 s
```

Measured on the synthetic Mount Moran demo spot. The same 30 m DEM makes the locks and the model, so DEM error, lens and sensor bias, and people who misjudge a summit are not included. Not tested on a real phone.

| Test | Condition | Result |
| --- | --- | --- |
| Position error, 3 locks | 200 trials, heading noise 3°, tilt 1.5°, barometer 10 m, unknown heading frame | median 60 m, p90 95 m, truth inside the 95% region in 98% |
| Position error, 1 and 2 locks | same | median 153 m and 67 m |
| Wrong aim | one lock taken on another peak at least 15° away, 10 pairs × 20 trials | flagged 100%, right lock named 96%, false alarm without a swap 1.5% |
| Solve time | Node 22, one thread, 600 solves | median 0.6 ms, max 55 ms |
| Solve time in the phone page | headless Chrome 156 on the build laptop | 1.5 to 41 ms per Lock |
| Lock to judge screen | `tests/live_e2e.mjs`, Enter on the phone to the first viewer frame painted with it, same laptop, software WebGL, 6 locks × 2 runs | 153 to 642 ms |
| Safe route, able (30°) | demo spot to the 3 saved points | 4.9 to 11.8 km, 3 h 00 to 4 h 26 min, planned in 21 to 157 ms |
| Safe route, injured (18°) | same | no safe route to any of them, so Stay. Signal. Wait. |

## What we measured

| Test | Condition | Result |
| --- | --- | --- |
| TBD | TBD | TBD |

Only list numbers we actually measured at the event.

## Limits and path to deployment

TBD. What does not work yet, and what a real field version would need (power, size, weight, cost, interoperability).

## Credits

Pre-existing open-source libraries, frameworks, SDKs, and datasets used in this project.

| Name | License | Used for |
| --- | --- | --- |
| Medić et al. 2024, Drones 8(3) 76 | CC BY 4.0 | Thermal contrast and altitude numbers for a person in water (`data/literature/`) |
| Burke et al. 2019, Drones 3(4) 78 | CC BY 4.0 | Detection altitude versus contrast and pixel size (`data/literature/`) |
| NOAA NDBC buoy 44013 | U.S. Government data, public domain | Snapshot of water temperature, wind and waves (`data/ndbc/`) |
| SeaDronesSee Multi-Object Tracking (planned) | CC0 1.0 | Replay of real drone telemetry, kept out of the repo (`data/raw/`) |
| USGS 3DEP 1 m lidar (via OpenTopography) | U.S. Government data, public domain | Mount Moran terrain for the judge view (`terrain_viewer/data/moran/`) |
| USGS 3DEP 1/3 arc-second DEM | U.S. Government data, public domain | Mount Washington terrain (`terrain_viewer/data/washington/`) |
| Copernicus DEM GLO-30 | Copernicus DEM licence, free use with attribution (© DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018, provided under COPERNICUS by the EU and ESA) | Mount Fuji terrain (`terrain_viewer/data/fuji/`) |
| ESA WorldCover 10 m 2021 v200 | CC BY 4.0 | Woods, rock and water tints on the map (`terrain_viewer/data/*/topo.jpg`) |
| Wikidata | CC0 1.0 | Summit, lake and glacier names in `terrain_viewer/` |
| World Magnetic Model WMM2025 (NOAA NCEI and BGS) | U.S. Government data, public domain | Magnetic declination per tile, coefficients in `terrain_viewer/scripts/wmm/` |
| deck.gl 9.3.11 | MIT | 3D terrain rendering in `terrain_viewer/`, shipped locally in `terrain_viewer/lib/` |
| Noto Sans JP, B612 Mono (via Fontsource) | SIL OFL 1.1 | Fonts in `terrain_viewer/`, license texts in `terrain_viewer/lib/fonts/` |
| loaders.gl 4.4.3 terrain worker | MIT | Terrain decoding in `mockups/map_3d/`, shipped in `mockups/map_3d/lib/` |
| NumPy, SciPy, rasterio, contourpy, pyserial | BSD-3-Clause | Offline data prep scripts (contour lines with contourpy), and the USB reader in `mockups/sighting_box/` |
| Pillow | MIT-CMU (HPND) | Writing map images in the data prep scripts |
| OpenCV (opencv-python) | Apache 2.0 | Outlines of the probability regions in `mockups/map_3d/export_3d_assets.py` |

## Submission checklist

- [ ] README sections above are filled in
- [ ] No secrets, CUI, ITAR, or real personal data committed (use `.env`, see `.gitignore`)
- [ ] Large files use Git LFS or external links
- [ ] Final commit is on `main` before Sun Nov 8, 12:00 PM EST
- [ ] `git tag submission && git push origin submission`
