# RIDGELINE demo script

For the team's rehearsals and the judging slot. Every number here is in the [README](../README.md) or the [technical README](../ridgeline/README.md). Lines in [brackets] are used only when that part is in the build or was verified before judging. Speaking pace is about 130 words a minute.

## Before the judges arrive

① Restart the server on the final build from the repo root (`python -I ridgeline/server.py`), then run `python -I ridgeline/scripts/preflight.py` and fix anything it marks FAIL.

② Phone in airplane mode, connected by USB, app opened once so it is cached. Battery above 50%.

③ Wall cards at the measured marks, floor mark at the measured distance. Clear the last run (C on the board).

④ Board in full screen on the big display (F). The Trust view is one key away (T).

## 90-second version

| Time | Who | Say | Do |
| --- | --- | --- | --- |
| 0:00 to 0:15 | Seika | GPS is the first thing jammed or spoofed. Picture a downed pilot in the mountains. No GPS, no signal, only a phone. RIDGELINE gives them a position from what they can see. | Show the phone in airplane mode. |
| 0:15 to 0:45 | Seika, then Judge | Take the phone. Aim at the three peaks and press Lock each time. The phone tells you which peak is next. | Hand the phone to a judge standing on the floor mark. The judge Locks the three cards. |
| 0:45 to 1:00 | Teammate | Each Lock adds a line on the big screen and the bubble shrinks. It only uses the angles between peaks, so no compass and no declination. The whole solve runs on the phone in about a tenth of a second. | Point at the board, then press R to reveal the true spot and the error in meters. |
| 1:00 to 1:15 | Teammate | On this wall our real-phone test landed 42 meters off, inside a 293 meter circle. The cards stand in for real peaks and the height is simulated, so this is not a field result. | Leave the reveal on the board. |
| 1:15 to 1:30 | Seika | Where to gives a route that stays off water and steep ground, or says Stay, Signal, Wait. [Walk away and the circle grows until the phone asks you to aim again.] It runs on the phone you already carry, with no signal, and sends the fix to ATAK. | Tap Where to on the phone. Open `localhost:8768/api/cot` on the PC to show the Cursor on Target events. |

## 10-minute version

| Time | Who | Say | Do |
| --- | --- | --- | --- |
| 0:00 to 0:45 | Seika | GPS is the first thing jammed or spoofed. RIDGELINE gives an isolated person a position from what they can see. We are Team 8, working on OBJ.01, Locate Isolated People. Picture a downed pilot in rough mountains. The GPS is jammed, there is no cell signal, and all they have is a phone. For a lost hiker it is the same problem. | Title slide. Phone in airplane mode in your hand. |
| 0:45 to 1:15 | Teammate | The old way is a compass and a paper map. A compass gets pulled off near metal, like wreckage or a vehicle, and it needs the local declination, which is easy to get wrong under stress. | Problem slide. |
| 1:15 to 1:45 | Teammate | We use a 30 meter lidar terrain grid near Mount Moran in Grand Teton. Rohan's rule picked the 23 peaks you can aim at. We only use the angles between peaks, so compass north never matters. The map is about 600 KB and the phone solves in about a tenth of a second, offline. | Show the terrain viewer or the board map. |
| 1:45 to 2:15 | Seika | Take the phone. Aim at the three peaks and press Lock each time. These cards are placed so the angles between them match a real spot near Mount Moran. Please stand on the mark. | Hand the phone to a judge. Point to the floor mark and the cards. |
| 2:15 to 3:15 | Teammate, Judge | The phone names the peak to aim at. Put the crosshair on it and press Lock. The small outline is the skyline you should see toward the next peak, so you can check you have the right summit. If you Lock the wrong card, it notices and asks for that card again. | The judge Locks the cards one by one. A teammate points at the lines appearing on the board. |
| 3:15 to 3:45 | Teammate | After two Locks it says Close, not sure yet, one more peak. After three it says Enough, three peaks agree. You get an MGRS grid, a plus or minus radius that holds the truth 95% of the time, and the bubble on the map. | Point at the phone screen, then the board. |
| 3:45 to 4:30 | Seika | Now the answer. On this real phone our wall tests landed 42 meters off inside a 293 meter circle, and 90 meters off inside 278. With the cable pulled and the page reloaded from the phone's own copy, it still solved, 212 meters off inside 298. | Press R on the board to reveal the true spot and today's error. |
| 4:30 to 5:00 | Teammate | Where to offers Rendezvous, Back to start, your marks, or a pin. Removing a pin takes two taps, so it does not move by accident. Routes stay off water and slopes over 30 degrees. | Tap Where to and pick Rendezvous. |
| 5:00 to 5:30 | Teammate | Switch to Injured and the limit drops to 18 degrees, with one sentence on why. If there is no safe way, it says Stay, Signal, Wait. Hide spots are folds in the ground that few summits can see. Signal spots are open ground that many can see. Terrain only, no trees or buildings. | Switch to Injured. Show the Hide and Signal spots. |
| 5:30 to 6:00 | Seika | We never trust one source. Terrain checks GPS. A test reading 2.48 kilometers away fell outside our circle, so the phone said GPS disagrees and did not use it. One 61 meters away was consistent. We do not know why a reading disagrees, so we call it a disagreement, not a spoofing detector. | Settings, Test GPS, far point, then near point. |
| 6:00 to 6:30 | Seika | In 200 simulated fixes with 3 degrees of aim error, the truth was inside the circle 99% of the time. Read the same Locks as compass bearings with a 7 degree compass bias and you get 204 meters and 54%. We get 103 meters and 98%. | Press T on the board for the Trust view. Say that these are simulations. |
| 6:30 to 7:30 | Teammate | [Mentor Brendan asked for Auto-PACE. After the fix the phone keeps its probability map. Walk, and the circle grows. The barometer and the lakes pull it back in. GPS inside the circle is used, outside it is not. The screen shows the PACE tier it relies on. This walk is simulated, at 30 times speed. At the limit the phone buzzes and says Confidence low, aim at 3 peaks again.] Without Auto-PACE, say instead that the phone keeps the last fix and says how old it is. | [Tap Simulated walk and let the circle grow until the prompt.] |
| 7:30 to 8:00 | Seika | Mentor Logan told us not to build another drone or another command and control tool, and to feed the tools people already use. So the fix, its 95% circle, the route and the marks go out as Cursor on Target for ATAK. | Open `localhost:8768/api/cot` on the PC. |
| 8:00 to 8:30 | Seika | We tested that feed with a stand-in TAK client. Real ATAK display is not verified yet. [We showed it in ATAK on this phone this morning.] Mentor Matt said confidence should always show and drop when the fix may be drifting. The phone always shows its radius, and ATAK reads how old the fix is. | Point at the remarks line, Position as of, minutes ago. |
| 8:30 to 9:15 | Teammate | Four of us built this. Rohan, the peak selection rule and the topography and localization prototype. Noah, the terrain data, the map colours on every screen and a route planner prototype. Douglas, the simulated accelerometer and inertial path prototype. Seika, the phone app, the live demo and the measurement record. | Each person raises a hand. |
| 9:15 to 10:00 | Seika | Our honest limits. Cards stand in for mountains, the height is simulated, we have not tested outdoors, real ATAK is unverified, and battery life is not measured. Next come outdoor trials with real ground truth, a real barometer, real ATAK, weather and voice guidance. GPS is the first thing jammed or spoofed. RIDGELINE runs on the phone you already carry, with no signal, and we never trust one source. Thank you. | Hand the phone back and take questions. |

## If asked

① Forest or fog hides the peaks. "Land nav already starts by getting high. Our Signal spots are open ground with a wide view, and until then the phone keeps the last fix and says how old it is."

② Night. "Same as a person with a map, the Mark 1 eyeball. Peaks show against the sky with moonlight or NVGs, and Night mode keeps the screen red."

③ Why no compass. "A compass drifts near metal and needs declination. We only use the angles between peaks, so a bad compass cannot move the fix."

④ How accurate. "On the real phone our wall test landed 42 m off, inside a 293 m circle. In 200 simulated fixes with 3 degree aim error, the truth was inside the circle 99% of the time."

⑤ Preloaded map. "Yes, like any mission planning. Our 10 by 8 km area is about 600 KB."

⑥ Barometer drift. "Up to 30 m off, the truth stays inside the circle in at least 97.5% of fixes. At 60 m it drops to 70%, and we say so."

⑦ GPS spoofing. "A GPS reading outside the terrain circle is not used. In our test a reading 2.48 km off was rejected, and an honest one 61 m off agreed. We call it a disagreement, not proof of spoofing."

⑧ Versus ATAK's own resection. "ATAK's resection needs a compass and map reading. RIDGELINE needs neither, catches a wrong aim, gives a 95% circle, and sends the answer to ATAK."

⑨ Battery. "One fix is under a second of compute. We have not measured battery life yet."

⑩ Weather. "That is our next step. Forecast grids loaded before the mission can recolor the routes."

⑪ How old is the fix in ATAK. "Every update says when the position was made and how many minutes ago, and after 10 minutes it says the person may have moved."

## Do not say

① Field accuracy or outdoor results. Every phone number here is from wall cards indoors.

② Spoofing or jamming detection. Say the GPS reading and the terrain disagree, and the cause is unknown.

③ Real ATAK display, unless the team saw it in ATAK before judging.

④ That the phone never uses the magnetometer. Say the fix only depends on the angles between Locks.

⑤ Which sensor each PACE tier stands for, unless the build shows it on screen.

⑥ Any number that is not in this script or the README.
