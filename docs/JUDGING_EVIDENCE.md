# RIDGELINE judging evidence

Prepared on November 8, 2026. This document distinguishes recorded observations, simulated distributions and work still to verify. It does not add new phone measurements. The demonstration entry point is [the repository README](../README.md).

## Official criteria and evidence

The [organizer's GitHub rules](https://github.com/CodeMetalForge-Hackathon/.github/blob/main/profile/README.md) list the following four criteria. No numerical weights are published. The rules place relatively little weight on pitch polish, code cleanliness or completion of a production product. The [event site](https://www.codemetal.ai/forge-hackathon) also states the mission and the November 8, 2026 noon EST cutoff.

| Criterion | Relevant implementation | Evidence available | Evidence still missing |
| --- | --- | --- | --- |
| Real-world relevance | Named-peak observations, visible uncertainty, re-aim requests, terrain-constrained routes and MGRS output | The phone and board implement a complete observation-to-estimate flow. Route tests exercise water constraints | Usability with an unfamiliar operator, field outcomes and evidence that the suggested action is useful under real visibility and injury constraints |
| Working Live Demonstration | Phone solver, live board relay, service worker and route display | Three recorded phone trials below, including an offline reload and reconnect | A consecutive five-trial record on the final revision, a full airplane-mode trial and a repeatable demonstration by someone other than the builder |
| Deployability | Local data, phone computation, Python standard-library server and CoT output | Committed assets and executable setup commands. CoT tests use a stand-in client, and real ATAK-CIV showed the fix, circle and route on Nov 8 | ATAK deletion after a Reset, a real height source, outdoor sensor behavior, power use and route feasibility |
| Ideation | Relative-angle estimation with a fitted heading offset, model uncertainty and mismatch handling | Solver and simulation tests demonstrate the approach under their stated assumptions | A measured comparison with an established workflow using the same observations and task conditions |

The primary mission is OBJ.01, locating an isolated person without reliable GPS. CoT provides a route toward OBJ.04 when communications are available. The current prototype does not establish independent operation for every rescue environment.

## Evidence classes

| Class | What belongs here | What it supports |
| --- | --- | --- |
| Recorded phone observations | Physical handset held by a person, actual Lock observations, settings and timestamps | Operation of that phone workflow under those controlled conditions |
| Simulation | Generated angular noise, board-map clicks, sensitivity sweeps and the Trust distributions | Behavior of the model under specified inputs |
| Automated checks | Solver, route, visibility and CoT assertions | Regressions covered by those assertions on the revision actually tested |
| Field validation | Independent outdoor locations, real landmarks and actual height calibration | Not yet recorded for this prototype |

The Manus prototype's useful documentation idea is adopted here as a strict separation of individual measurements from simulated distributions. It does not imply that the Manus implementation has been merged or validated as the demonstration app. Do not combine these classes into a single success rate.

## Recorded phone observations

Source for all three rows is [RIDGELINE's existing Gate A and Gate B record](../ridgeline/README.md#test-results-gate-a). The device was a Galaxy S24 Ultra using Chrome 156, the targets were wall cards reproducing angles from the selected Moran terrain location, and the height was simulated. These are preserved historical reports, not a new test of this review revision. Per-trial raw logs and exact revision IDs are not attached in that source.

| Local time on 2026-11-08, EST | Recorded input or condition | Recorded output | Interpretation |
| --- | --- | --- | --- |
| 04:56 | Tape-measured wall layout. Relative angles 24.2° left and 56.1° right, compared with planned 25.8° and 54.4° | Enough, 42 m position error, 293 m displayed radius | Successful controlled phone trial. Error was within the displayed radius |
| 05:01 | Hand-held Locks on the current version at that time | Enough, 90 m error, 278 m radius. Rendezvous selected automatically and a 3.40 km route displayed | Successful controlled trial with route display. Traversability of a real 3.40 km route was not tested |
| 05:03 | Cable removed, phone page reloaded from the service worker copy, three Locks | Enough, 212 m error, 298 m radius. Latest fix appeared on the board about 1 s after reconnection | Offline page reload and local computation recorded. Airplane mode was not recorded |

The same source also retains an earlier 04:28 wall attempt with target geometry 5° to 6° off, a 360 m position error and a 363 m radius. Three earlier attempts with the cards in the wrong order each triggered a mismatch and a request to re-aim. Those outcomes must not be erased or counted as successful position fixes. Layout changes mean these historical attempts are not a consecutive repeatability series.

## Simulated results

Source is [the existing solver and Trust record](../ridgeline/README.md#test-results-gate-a). These figures were generated with controlled input assumptions, not by people aiming at real mountains.

| Experiment | Recorded result | Limit |
| --- | --- | --- |
| 200 runs at the demo location, three Locks, 3° Gaussian aim noise and a random constant heading offset | Median error 98 m, 90th percentile 184 m, truth in the nominal 95% region in 99% of runs | Depends on the chosen location, error distribution and height model |
| 120 sampled locations in the tile with three visible peaks | Median error 132 m, 90th percentile 540 m, 99.2% inclusion | The sample excludes locations without the required visible peaks |
| Height reference biased by −60 m or +60 m, 200 simulated fixes per row | Inclusion fell to 70.0% and 70.5% respectively, without the re-aim check firing | A narrow probability region can be wrong when the height assumption is wrong |
| Height band removed in the sensitivity experiment | Median displayed radius about 2.1 km, 98.5% inclusion | Greater uncertainty remains visible instead of being hidden by a precise height assumption |

The nominal 95% region contains 95% of the solver's probability under its model. It is not an independently calibrated 95% guarantee for a person outdoors. A simulated constant compass bias is a controlled comparison, not a benchmark against ATAK or every physical compass.

## Relationship to ATAK

ATAK already offers mapping, information sharing and a resection workflow for estimating position from landmarks. The [ATAK user guide v4.5 mirror](https://toyon.github.io/LearnATAK/atak_resource/v4.5/ATAK_User_Guide.pdf) describes its resection tool on page 50. This is a versioned manual mirror, not evidence of all capabilities in the currently deployed ATAK version. The [US Department of Defense source repository](https://github.com/deptofdefense/AndroidTacticalAssaultKit-CIV) is an additional primary reference for the existing system.

RIDGELINE's contribution to test is the guided sequence of landmark observations, fitting a constant heading offset, displaying the model uncertainty, detecting some inconsistent observations and passing an estimate into an existing responder workflow. Resection itself is established. No head-to-head ATAK performance test has been recorded, and we do not claim that ATAK lacks uncertainty features or must always use one particular sensor.

CoT messages are generated and tested against a stand-in client. On Nov 8 real ATAK-CIV on our test phone connected and showed the fix, its 95% circle, the route and the marks. Deletion after a Reset is unverified. A disagreement between a supplied GPS point and the terrain estimate indicates a cross-check is needed, not which source is wrong and not proof of spoofing. The browser's absolute-orientation fallback also prevents a blanket claim that the hardware is magnetometer-free.

## Five-trial repeatability record

This table is intentionally unfilled. **Not measured** means no result exists in this record. Do not populate it with the three historical observations or with simulation results.

Before beginning, record the exact app revision, phone model, browser version, operator, target layout, standing distance, selected orientation stream if available, height input, and connection mode. Keep these fixed for the five consecutive primary trials. If a correction changes the setup, preserve the original attempts and start a clearly labeled new series.

| Trial | Time, EST | Usable fix or failure | Total task time, s | Solve time, ms | Error, m | Displayed radius, m | Within radius | Re-aim requests and notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Not measured | Not measured | Not measured | Not measured | Not measured | Not measured | Not measured | Not measured |
| 2 | Not measured | Not measured | Not measured | Not measured | Not measured | Not measured | Not measured | Not measured |
| 3 | Not measured | Not measured | Not measured | Not measured | Not measured | Not measured | Not measured | Not measured |
| 4 | Not measured | Not measured | Not measured | Not measured | Not measured | Not measured | Not measured | Not measured |
| 5 | Not measured | Not measured | Not measured | Not measured | Not measured | Not measured | Not measured | Not measured |

① Reload the final app version while connected and verify the assets are available offline. Place the cards using the supplied layout and mark the observer position. Do not use keyboard aiming assistance or board-map simulation for phone trials.

② Start timing when the operator presses Start terrain fix. End the task timer when a usable fix appears. Record the solver's computation time separately if it is exposed, otherwise leave it Not measured. Do not describe computation time as the time taken by a person to obtain a fix.

③ Save the actual Lock observations and displayed outcome before revealing the known location. Record the error and radius, then any route chosen. If there is no fix, retain the failure row and reason. Re-aims and retries are part of the attempt, so do not restart the timer to improve the result.

④ Complete all five consecutive attempts. Report usable fixes out of all five attempts, every failure and any changes of setup. Report error and time statistics with the number of usable results. Five trials can demonstrate repeatability in this setup, but cannot validate a 95% field-coverage claim.

⑤ Run a separate offline check with the cable removed, airplane mode on and Wi-Fi confirmed off. Reload the app, make new Locks and verify local results. Record any permission or cache failure. Reconnect and time delivery of the latest state to the board. Keep this separate from the fixed-condition five-trial series.

⑥ Retain regression cases for a wrong landmark, a repeated Lock, a water pin, an island pin, an unreachable destination and an uncertain route origin. Describe which were automated, which were tested through a desktop browser and which were observed on the phone. A drawn route does not establish field safety.

## Capture each trial locally

After each new set of phone observations, run this command from the repository root. Replace the trial ID for each attempt. Keep the server running.

```bash
python -I ridgeline/scripts/capture_trial.py --trial-id gate-b-01 --condition "Galaxy wall cards, cable connected" --input-mode sensor --reference wall-cards
```

The standard-library recorder reads `/api/state` without changing it. It appends a local JSONL record to `.local/ridgeline-trials.jsonl`, which is excluded from Git. Use `--server http://localhost:8809` for the isolated review server. Input mode and conditions are operator declarations, not sensor detections. Use `--reference none` outside the wall-card setup.

Failures and missing fixes remain in the record. The recorder flags reused trial IDs and repeated Lock observations, including when GPS, health, marks or height settings change. Do not count a duplicate as another trial. The wall-card error compares with the synthetic terrain reference, not with an outdoor surveyed position. Task time, phone-to-board latency and inclusion in the irregular probability region are not measured by this recorder. Record those separately rather than filling them with solver time or circle inclusion.

## Review validation, 2026-11-08

The isolated review branch passed 28 Node tests (solver, Hide, skyline and three-area water routing) and 20 Python tests (CoT and trial recording). Desktop browser checks exercised three keyboard-assisted Locks, position and route display, the far GPS test point, and the injured route candidate. This was not another physical-phone trial. The candidate-start text identifies the unverified connection and its map has no direct connecting line.

A local capture from that keyboard run recorded an error of 67 m against the synthetic wall reference and a displayed radius of 304 m. Recapturing the same observations under a new trial ID was marked duplicate. This tests the recorder and is excluded from the five-trial phone series.

## Before submission

Use the exact revision intended for main, run the commands in [the root README](../README.md#what-we-measured), and retain the results. Confirm the remaining team names and roles. Check the final phone and board behavior after any service-worker update. Preserve the evidence boundaries in the live explanation and in the submitted README.

The organizer requires a README covering the project, team, setup, hardware and demo, an OSI-compliant license, and a final default-branch commit by November 8, 2026 at 12:00 EST with a `submission` tag pushed. This document does not certify that the branch or tag has been submitted.
