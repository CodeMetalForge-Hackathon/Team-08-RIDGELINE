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
| USGS 3DEP 1 m lidar (via OpenTopography) | U.S. Government data, public domain | Grand Teton terrain for the judge view (`terrain_viewer/data/`) |
| deck.gl 9.3.11 | MIT | 3D terrain rendering in `terrain_viewer/`, shipped locally in `terrain_viewer/lib/` |
| Noto Sans JP, B612 Mono (via Fontsource) | SIL OFL 1.1 | Fonts in `terrain_viewer/`, license texts in `terrain_viewer/lib/fonts/` |

## Submission checklist

- [ ] README sections above are filled in
- [ ] No secrets, CUI, ITAR, or real personal data committed (use `.env`, see `.gitignore`)
- [ ] Large files use Git LFS or external links
- [ ] Final commit is on `main` before Sun Nov 8, 12:00 PM EST
- [ ] `git tag submission && git push origin submission`
