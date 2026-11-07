# Data

What is in this folder, where it came from, and what we keep out of the repo on purpose.

## Rules we follow

- No images or positions of real people are committed. The event rules ban them.
- Raw third-party datasets go in `data/raw/`, which is git-ignored. Only small derived tables, snapshots and the scripts that make them go in the repo.
- Every source lists its license below.

## What is here

| Path | What it is | Source | License |
| --- | --- | --- | --- |
| `literature/thermal_person_in_water.csv` | Published numbers on how visible a person in water is to a thermal drone camera (temperature contrast, altitude, pixels on target) | Medić et al. 2024 and Burke et al. 2019 | Both papers are CC BY 4.0. We copied numbers, not text or figures |
| `ndbc/44013_2026-11-07_last72h.txt` | NOAA buoy 44013 in Massachusetts Bay, east of Boston. 430 rows covering the 72 hours up to 2026-11-07 19:20 UTC. Wind, waves, pressure, air and water temperature | NOAA National Data Buoy Center | U.S. Government data, public domain |

## Why these numbers matter

To a thermal camera, a person in the water does not look like 37 °C. In Medić et al. 2024 the sea was 20.7 °C and a swimmer read 22.5 °C, a contrast of only 1.8 °C, yet the head was still detectable up to 110 m. Buoy 44013 read 13.0 °C water and 11.9 °C air at 19:20 UTC on Nov 7. So a simulated target should sit only a few degrees above its background. The literature rows give a baseline detection curve, and our own measured trials replace it on site.

## Notes on the numbers

- `kind` is `observed` (measured in a field test), `model` (the authors' calculation) or `geometry` (apparent size from altitude and lens).
- Medić et al. 2024 give 20.7 °C as the sea temperature in their method section. One later sentence mentions 22.5 °C as the sea temperature, which looks like a typo, so we use 20.7 °C.
- The survival suit contrast of 8.3 °C is our own subtraction (face 29 °C minus sea 20.7 °C).
- Burke et al. 2019 state the detection altitudes once for targets 10 °C and 3 °C warmer than the background, and once with 18 °C and 11 °C. We use the 10 °C and 3 °C wording, because that sentence says explicitly that it is the difference from the background.

## Planned, not stored here

| Source | Plan | Why it is not in the repo |
| --- | --- | --- |
| [SeaDronesSee](https://seadronessee.cs.uni-tuebingen.de/dataset) Multi-Object Tracking | Replay real drone telemetry (GPS, altitude, gimbal pitch, heading and speed for every frame) to drive the map at realistic scale, with simulated targets | The annotations include positions of real swimmers. Download into `data/raw/` locally. The authors' GitHub lists the dataset as CC0 1.0 and the code as MIT |
| Our own thermal trials | About 60 passes per condition (normal and glare) with the thermal camera over the table grid, logged as observation JSON (see `../schema/`) | Will be added here as we record them |

## Refreshing the buoy snapshot

Columns follow the NDBC realtime format. Times are UTC and `MM` means missing. The useful columns are `WSPD` (wind speed, m/s), `WVHT` (wave height, m), `ATMP` (air temperature, °C) and `WTMP` (water temperature, °C).

```bash
curl -o data/raw/44013.txt https://www.ndbc.noaa.gov/data/realtime2/44013.txt
```

## Sources

- Medić D., Bakota M., Jelaska I., Škorput P. (2024). Low Contrast Challenge and Limitations of Thermal Drones in Maritime Search and Rescue—Pilot Study. Drones 8(3), 76. https://doi.org/10.3390/drones8030076
- Burke C., McWhirter P. R., Veitch-Michaelis J., McAree O., Pointon H. A. G., Wich S., Longmore S. (2019). Requirements and Limitations of Thermal Drones for Effective Search and Rescue in Marine and Coastal Areas. Drones 3(4), 78. https://doi.org/10.3390/drones3040078
- NOAA National Data Buoy Center, station 44013. https://www.ndbc.noaa.gov/station_page.php?station=44013
- Varga L. A., Kiefer B., Messmer M., Zell A. (2022). SeaDronesSee: A Maritime Benchmark for Detecting Humans in Open Water. WACV 2022. https://seadronessee.cs.uni-tuebingen.de/dataset
