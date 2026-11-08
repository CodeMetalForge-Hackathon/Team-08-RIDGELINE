# RIDGELINE

A terrain position estimate computed on the phone without GPS or a network connection. The pilot aims at three peaks they can name and taps Lock on each one. Relative peak angles, a height band and USGS 3DEP lidar terrain give an estimate with a modelled 95% radius, solved in about 0.1 s. A constant heading offset is fitted rather than assuming compass north. The browser may fall back to an absolute orientation stream, so this is not proof that the magnetometer is never used. The demo height band is simulated.

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
| Phone | `/` | System check, then the aim scope (rear camera behind the crosshair, heading tape relative to the last Lock, steady brackets, Lock button with vibration), the ±m meter, the bubble on the topo map, then the fix in MGRS and lat lon and the way out. After the 2nd Lock a small silhouette shows the horizon expected toward the next peak. Where to? offers Rendezvous, Back to start, marks, a pin on the map, and three Hide and three Signal spots, healthy 30° or injured 18°, with STAY SIGNAL WAIT when no way stays under the limit. An estimate on ground steeper than the limit uses a candidate route start on nearby walkable ground inside the 95% circle, with an explicit warning that the connection from the current position is unverified, and every new fix selects Rendezvous (routes use the viewer's planner `terrain_viewer/team8_nav.js`, read only). Settings has Test GPS, Night (red) and Clear marks. A Send to ATAK panel shows the CoT the server streams. Works offline after one load. |
| Board | `/board` | The same solve on the printed topo map, redrawn the moment a Lock arrives (long poll, about 0.1 s on the PC). Click anywhere on the map to drop a test pilot there. The board simulates three Locks with 3° aim error from that spot and solves again. R shows the true spot and the error in meters, C clears. It also draws the phone's Hide (H1 to H3) and Signal (S1 to S3) spots, the GPS verdict and the ATAK link. |
| Cards | `/cards` | Printable wall layout and one card per peak, with the horizon toward that peak drawn from the lidar. |
| Trust | `/trust` (T on the board) | Simulated error and 95% coverage for 1 to 10° aim error, a barometer reference that is off by up to 60 m, the same Locks solved the compass way, and a live run of solver.js against the viewer's team8_nav.js. |

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

## After the fix

* Offline. A service worker loads every file from the PC first, waits 2.5 s, and keeps a copy of everything opened once. After one load with the cable the app opens, solves and plans a route with no PC, and the newest state reaches the board when the link comes back.
* MGRS. The fix is shown in MGRS with as many digits as the 95% radius allows (the demo tile is 12T WP), checked against an independent UTM formula (Snyder) at 6 points to 0.23 mm.
* GPS cross-check. Settings has Test GPS, a simulated reading (the app never reads real GPS). Outside the terrain estimate's modelled 95% circle it shows GPS DISAGREES and the distance, inside it shows GPS CONSISTENT. Neither result establishes which position is correct, and disagreement is not a spoofing detector. Terrain, height, orientation or the injected test point may be wrong. The far test point was 2.48 km from the estimate and disagreed, while the near test point was 61 m away and was consistent. The legacy wire verdict `REJECTED` remains for compatibility and means disagreement only.
* Expected skyline. `skyline.js` casts 121 rays across ±15° around the next target from the current estimate (30 m grid with the true summit heights put back, curvature and refraction included), so the pilot can check by eye that the crosshair is on the right summit.
* Hide and Signal. `hide.js` walks the 30 m grid outward from the fix (Tobler off-trail speed, half speed when injured, water and slopes over the limit blocked). It offers three HIDE spots seen by the fewest of the 23 summits and three SIGNAL spots on ground of 15° or less seen by the most, each at least 250 m apart within a 30 min walk. Terrain only, trees and buildings are not in the model, so a HIDE spot is a fold in the ground and never a promise. At the demo spot (a canyon only 5 summits can see) HIDE spots are seen by 2 of 23 summits at 14 to 18 min, SIGNAL spots by 4 or 5 at 8 to 20 min. Injured, the fix cell is 19.4°, so the candidate route start is 162 m W on ground under 18° (see below). This is not a verified walking connection from the estimate.
* Walkable start. A hand-held fix can land a few hundred meters off on a cliff. If the fix cell is over the limit, on water or on a ledge boxed in by steep ground, `hide.js` picks the nearest cell inside the 95% circle that sits on open walkable ground, the phone checks that the route planner can leave it (up to 5 tries), and routes and Hide and Signal start there. The fix itself never moves. The phone marks the candidate start and its offset, without drawing a connecting line. The connection from the current position is not checked, so the display asks the user to confirm their position and terrain before using the candidate route. Distances, walking times, HIDE and SIGNAL reach are computed from that candidate, not from a validated connection to the estimated location. The Gate B fix landed on a 32° cell, so the healthy candidate is 42 m NE (3.71 km to Rendezvous, 3 HIDE and 3 SIGNAL spots) and the injured candidate is 90 m N (the route ends 562 m short of Rendezvous, and the phone says the last 610 m is steeper than 18° or crosses a cliff, 3 HIDE and 3 SIGNAL spots), checked in a desktop browser at phone size. With nothing walkable inside the circle the phone still says STAY SIGNAL WAIT.
* Water. Routes never cross water. A pin in a lake or on an island ends at the closest shore you can reach, the phone shows the planner's own sentence on why a route stops short (water, an island or steep ground), and when you already stand at that shore it says so instead of STAY SIGNAL WAIT.

## Auto-PACE, the fix keeps living

`pace.js` (browser and Node, like `solver.js`) keeps the fix's probability map on the same 30 m grid after the fix and rewrites it whenever a sensor reports, a grid Bayes filter. The phone and the board show what the position rests on now as PACE tiers, P GPS, A terrain fix (how many minutes ago, how far walked since), C steps (count, where the heading comes from, barometer use) and E re-aim.

* Start. `solver.js` returns the normalized map with the option `keep_p` (nothing else changes, tested).
* Steps. `devicemotion` acceleration magnitude, smoothed, a peak 1.2 m/s² over the 2 s mean, 0.28 to 2 s apart. Stride is assumed, 0.7 m healthy and 0.45 m injured.
* Heading. The same orientation stream the Locks used, turned to true north with the offset the peaks fitted (true = phone − offset), so the peaks calibrate the compass. Flat phone, its top edge. Held up, the camera. Another stream, no fresh sample or no fitted offset means heading unknown.
* Motion. The map is moved by the walk and widened by the walk's error, 1σ = distance × max(8%, sin 6°), growing with the distance since the last anchor (fix, new fix or GPS), so a steady heading or stride bias adds up instead of averaging out. With the heading unknown the 95% radius grows by the distance walked in every direction and the center stays. The move is whole cells plus a carried sub-cell offset instead of the bilinear shift in the plan, because every bilinear shift by a fraction f of a cell adds its own blur of f(1 − f) cell², so a walk in 15 m steps would spread faster than the formula. The widening is an exact-variance box blur. Measured spread against the formula, 60.3 m against 60.3 m and 126.2 m against 126.2 m. Lake cells are set to 0 and the map is normalized again.
* Barometer (simulated in the browser). The offset between the barometer and the ground under the fix map is kept as the reference, then each reading multiplies a height band (σ 15 m), at most once per 100 m walked so repeated readings do not make it overconfident.
* GPS (Test GPS, simulated). A reading inside the live 95% circle AGREES and is multiplied in (σ 15 m) and the position rests on GPS. Outside, it DISAGREES and is never used. The wire values `AGREE` and `REJECTED` for the board and CoT are unchanged.
* Re-aim. A new fix after walking is multiplied into the walked map. When the two overlap less than 1% (Bhattacharyya coefficient) the new fix is taken alone and the phone says the steps were off.
* Limits. ±300 m or less is GOOD, ±600 m or less CAUTION. Over 600 m, or over 2.5 times the radius at the fix, the phone vibrates, says "Confidence low. Aim at 3 peaks again." and marks the nearest SIGNAL spot (`hide.js`, from the live position) as the place to re-aim.
* No step stream for 5 s while the screen is on the fix (screen off, sensor stopped) counts as walking up to 1 m/s in any direction, and the A tier shows how much of the distance is assumed. Without motion permission no steps are counted and the circle does not grow, but the A tier keeps the age of the fix.
* What goes out. Once the fix screen is up, `lat`, `lon` and `r95_m` sent to the board and ATAK are the live Auto-PACE position, and the ATAK remarks say how far was walked and whether a GPS reading was used. Routes, Hide and Signal stay anchored to the terrain fix.
* Switches. Settings has "Auto-PACE after the fix" and "Count steps with the motion sensor", both on by default (`PACE_DEFAULT` in `phone/index.html`, set to false to ship a feature off).
* Simulated walk, labelled SIMULATED on the phone and the board. It walks the current route from the true demo spot at 30× (or 120×) with a 4° heading error, steps 5% longer than assumed and a barometer read from the terrain plus the reference error at the fix. "Heading lost (pocket)" walks it with no usable heading. Simulated re-aim walks the simulated person to the Re-aim spot the phone shows (as a move from where they really are), or to the nearest spot within 600 m that sees three peaks, and Locks three peaks there with the set aim error.

## Hand-off to ATAK

The server creates Cursor on Target events for the fix, its modelled 95% circle, the candidate route, HIDE and SIGNAL marks, and a disagreeing simulated GPS point. These are intended for ATAK native items, but their display inside real ATAK is not yet verified. The point is labelled GPS DISAGREES and states that the cause is unknown. `ce` is the 1 sigma circular error in the CoT base event schema, so ce = r95 / 2.4477. `hae` is ground + 1.7 m plus the GEOID18 offset for the tile (about −8.7 m), because the lidar heights are NAVD88. The fix reaches ATAK only with 2 or more Locks and no mismatch, and a reset deletes the items.

1. Keep the server running and run `adb reverse tcp:8087 tcp:8087`.
2. In ATAK-CIV, Settings, Network Preferences, Manage Server Connections, Add. Address 127.0.0.1, no authentication, Advanced Options, TCP, then set the port to 8087.
3. The board's ATAK line shows "linked, 1 client".

With no ATAK setup at all, start the server with `--atak-push 127.0.0.1:4242` after `adb forward tcp:4242 tcp:4242` to use ATAK's default TCP input (one event per connection, as ATAK reads it).

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

Other tests, run on Nov 8, 2026.

| Command | Result |
| --- | --- |
| `node --test ridgeline/test/hide.test.js` | 9 of 9. The reach (1,244 cells) matches an independent search, every summit count and slope rechecked, 3.5 to 7 ms per call after the first. The walkable start is the nearest open cell under the limit inside the circle, gives the Gate B fix Hide and Signal spots, leaves the healthy demo start unchanged, and falls back to STAY on water |
| `node --test ridgeline/test/lock.test.js` | 6 of 6. Taps, clicks or Enter presses while an accepted Lock is being solved and drawn, or within 600 ms of it, are ignored, so a double tap records the peak once. A real bad aim still replaces the suspect Lock, and the control is released after the solve, even when the solve fails. The test runs the code cut from `phone/index.html` |
| `node --test ridgeline/test/skyline.test.js` | 6 of 6. The skyline at the 3 wall peaks is within 0.001° of the predicted summit angles, agrees with the visibility masks on 99.9% of visible pairs over 300 random cells, and stays within 0.062° of the wall cards. About 1.5 ms per call |
| Gate B, by hand on the real phone (Galaxy S24 Ultra, Chrome 156, Nov 8 4:56 EST) | Three paper marks on a wall, measured with a tape and Locked by hand. The angles between Locks were within 2° of the layout (24.2° left and 56.1° right against 25.8° and 54.4°), the phone said Enough, and the fix landed 42 m from the true spot inside its ±293 m bubble. Again at 5:01 on the current version, 90 m off inside ±278 m, Enough, with Rendezvous selected on its own and a 3.40 km route on the phone screen. An earlier try at 4:28 with the marks 5 to 6° off landed 360 m away, inside its ±363 m bubble. Three earlier tries with the marks in the wrong order were each flagged as a mismatch with a request to aim at the last card again |
| Offline on the real phone (Nov 8 5:03 EST) | Cable out. The page reloaded from the service worker copy with no PC, three Locks gave Enough, ±298 m, 212 m from the true spot, and about 1 s after the cable went back in the board showed that fix |
| `python -I -m unittest discover -s ridgeline/test -p "test_cot.py"` | 11 of 11. Point, ce, hae, circle, route, marks, the disagreeing GPS test point, ping and pong, a late client, deletes after reset, push mode, a busy CoT port, the Auto-PACE wording (walked distance, GPS used, re-aim). The Lock relay stays under 0.25 s with ATAK connected |
| `node --test ridgeline/test/pace.test.js` | 10 of 10. `keep_p` changes nothing else. A 500 m walk north moves the map 500 m and widens it as the formula says (within 5%), with the heading unknown the 95% radius grows by the distance and the center stays. Lake cells hold 0 after a walk toward the lake. On a slope with the barometer band the radius is 211 m against 293 m without, and both hold the truth. GPS 2.5 km away is rejected and leaves the map unchanged, 40 m away agrees and takes the radius under 80 m. A consistent new fix is multiplied in, one 1.5 km off after the steps were off is taken alone. The tiers ask for a re-aim over 600 m or 2.5 times the fix. One update of the Moran map has a median of 3.9 to 6.1 ms (the barometer band 4.3 to 6.0 ms, a GPS reading 1.6 to 2.1 ms, single calls up to 28 ms while the PC was busy). The step detector counts 18 ± 2 steps for 10 s at 1.8 Hz, none when still or for one bump |
| Auto-PACE in a desktop browser at phone size (375 × 812) and the board at 1280 × 800, Nov 8 7:00 to 7:30 EST | Simulated walk to Rendezvous (3.48 km), heading from the peaks, 30×. The radius went from ±269 m to ±290 m at 1.36 km (27 m from the simulated walker), ±558 m at 2.89 km and ±747 m at 3.31 km counted, where the phone and the board asked to re-aim and marked a Signal spot 990 m E (18 min). The walker stayed inside the circle the whole way. A second walk from a ±304 m fix peaked at ±594 m and ended at ±564 m by the lake, so with the heading known this route does not always reach the limit. Heading lost, 30×, ±278 m grew to ±640 m after 1.27 km (65 s of simulation) and asked to re-aim, Signal 376 m SW, walker inside (234 m off). Test GPS at the fix, the near point agreed 11 m off and took ±269 m to ±64 m, the far point disagreed at 2.54 km and changed nothing. Mid-walk at ±142 m the near point took it to ±51 m and the far point was rejected. Simulated re-aim at ±747 m walked 611 m to the Re-aim spot, the new fix (±318 m) was multiplied in with 36% overlap, ±279 m, 11 m from the walker. Synthetic `devicemotion` for 6 s at 1.8 Hz counted 11 steps (11 expected), and 5 s after it stopped the walk was counted as assumed |

Barometer reference error at the demo wall, 3° aim error, 200 fixes per row (from `/trust`).

| Barometer error | Median error | 90th percentile | Truth inside 95% | Median ± |
| --- | --- | --- | --- | --- |
| −60 m | 191 m | 249 m | 70.0% | 293 m |
| −30 m | 120 m | 257 m | 98.0% | 269 m |
| 0 m | 107 m | 226 m | 99.5% | 293 m |
| +30 m | 107 m | 231 m | 97.5% | 321 m |
| +60 m | 155 m | 363 m | 70.5% | 360 m |
| Band off | 194 m | 1.9 km | 98.5% | 2.1 km |

A reference up to 30 m off still leaves the truth inside the bubble in at least 97.5% of fixes. At 60 m that drops to 70% and the re-aim check does not fire, so a wrong barometer gives no warning. Switching the band off keeps the truth inside but widens the ± from about 300 m to 2.1 km.

`node ridgeline/scripts/trust.js` (about 2 minutes) adds the aim error sweep behind `/trust`. At the demo wall with 3° aim error the median error is 103 m with the truth inside the bubble 98% of the time. The same Locks read as compass bearings with a 7° compass bias give 204 m and 54%. At 10° aim error both methods are 400 to 450 m off and the bubble holds the truth only about half the time, because the app assumes 3°. On 30 random drops solved by both solvers, the two answers were a median 49 m apart and each sat inside the other's 95% area 93% of the time.

## Not tested yet

* Anything inside real ATAK (the connection dialog, how the items look, the delete events). ATAK-CIV was not installed on the test phone, so the CoT was checked with a stand-in TAK client only.
* On the phone, Night mode over a live camera, the Copy button and installing to the home screen.
* Which orientation stream a held phone uses. Chrome on this Galaxy sends `deviceorientation` (gyro) only while the phone moves and `deviceorientationabsolute` all the time. The page prefers the gyro stream, falls back to the absolute one, and never mixes them within one fix.
* Vibration on Lock, airplane mode with the cable, the rear camera behind the crosshair, and the walkable start on the phone itself (the measured fixes on the phone did not land on steep ground, so it was seen only in a desktop browser at phone size).
* Elevation angles are recorded but not used (wall cards do not reproduce real tilt).
* Auto-PACE outside. No outdoor walk has been done. The barometer is simulated in the browser, the stride is assumed, step counting has only seen synthetic motion, and the walking heading has not been read from a real phone (a desktop browser has no orientation stream, so the browser checks used the simulated walk). Gyro drift during the walk is not modelled beyond the 6° heading error. The barometer reference error is systematic, so a wrong reference shifts every band the same way.
* `node --test ridgeline/test` (a folder) fails on Node 22 with MODULE_NOT_FOUND. Run `node --test ridgeline/test/*.test.js`.

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
| `trust/index.html`, `scripts/trust.js`, `data/trust.json` | Trust view and the simulation behind it. |
| `hide.js`, `test/hide.test.js` | Hide and Signal spots within a short walk of the fix, and the walkable start. |
| `test/lock.test.js` | The phone's Lock input guard, run on the code taken from `phone/index.html`. |
| `skyline.js`, `test/skyline.test.js` | Expected horizon toward the next peak. |
| `pace.js`, `test/pace.test.js` | Auto-PACE, the fix map kept live after the fix (steps, heading, barometer band, GPS check, re-aim). |
| `cot.py`, `test/test_cot.py` | Cursor on Target events and the ATAK link (server mode on 8087, push mode). |
| `phone/sw.js`, `phone/manifest.webmanifest` | Offline copy and home-screen install. |

## Credits

USGS 3DEP 1 m lidar DEM via OpenTopography (public domain). ESA WorldCover 10 m 2021 v200, CC BY 4.0. Place names from Wikidata, CC0. Magnetic declination from the World Magnetic Model 2025 (public domain). Topo map image from `terrain_viewer/`.

## Real-phone GPS rejection, 2026-11-08

Seika confirmed that GPS REJECTED was displayed on the real phone. That is what the screen showed at that time. The same result is now labelled GPS DISAGREES, with the cause unknown. The exact build, input and distance were not recorded in this confirmation. This records the phone UI result, not a live RF spoofing trial. Repeat the complete demo on the final merged display before submission.
