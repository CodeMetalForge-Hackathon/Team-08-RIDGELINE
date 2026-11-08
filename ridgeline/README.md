# RIDGELINE

A position fix with no GPS, no network and no compass. The pilot aims a phone at three peaks they can name and taps Lock on each one. The angles between those peaks, a barometer height band and the USGS 3DEP lidar terrain give a position with a 95% radius, solved on the phone in about 0.1 s.

The live demo replaces the real mountains with three printed cards on a wall. The cards are placed so that, seen from a mark on the floor, the angles between them match the true angles from a spot in the Mount Moran area of Grand Teton National Park. The judge stands on the mark, Locks the three cards, and the big screen draws the pencil lines, the error triangle and the 95% bubble. Press R to compare against the true spot.

## Run it

Everything runs offline on one PC and one Android phone. No package install is needed (Python 3 standard library, Node 18 or newer only for the tests).

1. Start the server from the repo root.

   ```bash
   python -I ridgeline/server.py
   ```

   It binds to 127.0.0.1:8768 only.

2. Phone over USB. Turn on USB debugging on the phone, plug it in, accept the "allow USB debugging" prompt, then run

   ```bash
   adb reverse tcp:8768 tcp:8768
   ```

   and open `http://localhost:8768` in Chrome on the phone. Airplane mode is fine, the USB cable carries the Locks. `localhost` counts as a secure page, so the motion sensors, wake lock and full screen work without HTTPS.

3. Big screen. Open `http://localhost:8768/board` on the PC (F for full screen).

4. Cards. Print `ridgeline/cards/ridgeline_wall_cards.pdf` on US Letter, landscape (or open `http://localhost:8768/cards` to change the judge distance and print again). Page 1 is the wall layout. With the judge 2.00 m from the wall the aim marks go 1.69 m left, 0.51 m left and 1.69 m right of the center mark.

## What the screens do

| Screen | Address | What it shows |
| --- | --- | --- |
| Phone | `/` | System check, then the aim scope (heading tape relative to the last Lock, steady brackets, Lock button with vibration), the ±m meter, the bubble on the topo map, then the final coordinates. |
| Board | `/board` | The same solve on the printed topo map, redrawn the moment a Lock arrives (long poll, about 0.1 s on the PC). Click anywhere on the map to drop a test pilot there. The board simulates three Locks with 3° aim error from that spot and solves again. R shows the true spot and the error in meters, C clears. |
| Cards | `/cards` | Printable wall layout and one card per peak, with the horizon toward that peak drawn from the lidar. |

Phone keys for a laptop dry run. Arrows aim, Enter locks, A aims at the current card (demo assist, keyboard only), N skips a card, U undoes, S opens settings.

## How the fix works

`solver.js` runs the same code in the phone, on the board and in Node.

* Grid Bayes on a 30 m grid (345 × 266 cells, 10.4 × 8.0 km) exported from the 5 m terrain of the viewer by `scripts/export_grid.py`.
* Bearings. The phone heading has an unknown constant offset (gyro drift, walls, magnetic noise), so each cell fits its own offset and only the differences between Locks count. This is the classic three-point resection, with 3° aim error per Lock.
* Barometer band. Ground + 1.7 m eye height compared with the barometer altitude, σ 25 m. In the demo the barometer reading is simulated (ground + 1.7 m + 5 m error).
* Visibility. A cell that cannot see a locked summit on the 10 m terrain (earth curvature and refraction included) is down-weighted ×0.3, not removed.
* The ±m on screen is the radius around the best cell that holds 95% of the probability. The bubble is the 95% and 50% highest density region.
* Mismatch check. With three or more Locks, a fit worse than χ² 14 flags the newest Lock that, read as another peak (a repeated photo included), makes the fit good again, and the phone asks for that card again.

Peaks are found with Rohan's `find_true_peaks` rule converted to meters (800 m isolation, 2,500 m or higher, 150 m above the 1 km mean), 23 peaks in the Moran tile. Names within 300 m come from Wikidata, and unnamed summits are labeled by height.

## Test results (Gate A)

```bash
node --test ridgeline/test/solver.test.js
```

Last run on Nov 8, 2026, 6 of 6 passing.

| Test | Result |
| --- | --- |
| Demo spot, 3 Locks, 3° aim error, random phone offset, 200 runs | truth inside the 95% bubble 99.0%, error median 98 m (90th percentile 184 m), ±m median 293 m, slowest solve 156 ms, false mismatch 0 of 200 |
| Same card Locked twice | caught, Lock 3 flagged as a repeat of card 1 |
| Lock on a different real peak (Pk 3681) | caught, Lock 3 flagged |
| 120 random spots across the tile that see 3 peaks | truth inside the bubble 99.2%, error median 132 m, 90th percentile 540 m |

These are simulations with Gaussian aim error. They do not include a person holding a phone.

## Not tested yet

* Hand-held aiming on the real phone (Galaxy S24 Ultra, Chrome 156). The page loads over `adb reverse` and the orientation stream arrives, but nobody has Locked the printed cards by hand yet. That is Gate B.
* Which orientation stream a held phone uses. Chrome on this Galaxy sends `deviceorientation` (gyro) only while the phone moves and `deviceorientationabsolute` all the time. The page prefers the gyro stream, falls back to the absolute one, and never mixes them within one fix.
* Vibration on Lock, airplane mode with the cable, and the red night screen.
* Elevation angles are recorded but not used (wall cards do not reproduce real tilt).

## Files

| Path | What |
| --- | --- |
| `server.py` | Serves the pages and data, relays Locks from the phone to the board (`/api/state`, `/api/wait`, `/api/reset`). |
| `solver.js` | The solver (browser and Node). |
| `phone/index.html` | Phone app. |
| `board/index.html` | Big screen. |
| `cards/index.html`, `cards/ridgeline_wall_cards.pdf` | Wall cards and layout. |
| `data/moran.json` | 30 m grid, land mask, 23 peaks with visibility masks, demo spot (603 KB). |
| `scripts/export_grid.py` | Builds `data/moran.json` from `terrain_viewer/data/moran/`. About 80 s. |
| `scripts/pick_demo.js` | Picks the demo spot and the three wall peaks by running the real solver on noisy Locks. |
| `test/solver.test.js` | Gate A tests. |

## Credits

USGS 3DEP 1 m lidar DEM via OpenTopography (public domain). ESA WorldCover 10 m 2021 v200, CC BY 4.0. Place names from Wikidata, CC0. Magnetic declination from the World Magnetic Model 2025 (public domain). Topo map image from `terrain_viewer/`.
