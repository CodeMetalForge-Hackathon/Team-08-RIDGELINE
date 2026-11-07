# Message schema (proposal)

A proposal for the JSON that the sensor, the simulator and the map exchange. It follows the conventions already in `cop.py` on the `Base-Layer-Map` branch, with `ts` in unix seconds and `bounds` as `[[south, west], [north, east]]`. Nothing here is final until the team agrees.

## Why a separate observation message

The map already shows where a sensor looked. To show how well it looked, each sweep also has to carry the conditions it saw and whether it detected anything, cell by cell. We store only raw measurements, never computed probabilities, so the map can always be rebuilt from the log with a different detection model, and duplicate or late messages give the same map.

## Messages

| File | Sent by | Meaning |
| --- | --- | --- |
| `observation.example.json` | sensor or simulator, once per sweep | What one pass saw, per grid cell |
| `calibration.example.json` | whoever fits the detection curve | Coefficients of the detection curve and how many trials they came from |
| `cell_state.example.json` | the engine, back to the map | Status, probability and re-search reason for each cell |

## Observation fields

| Field | Type | Meaning |
| --- | --- | --- |
| `type` | string | Always `observation` |
| `id` | string | Unique per sweep. A repeated id is ignored |
| `ts` | number | Capture time, unix seconds |
| `sensor` | string | Sensor id, for example `thermal-1` |
| `platform` | string | Asset that carried the sensor, same id as in `/api/assets` |
| `bounds` | array | Footprint of the whole sweep |
| `cells[].cell` | string | Grid cell name, for example `C3` |
| `cells[].range_m` | number | Distance from the sensor to the cell center |
| `cells[].bg_median_c` | number | Median background temperature in the cell |
| `cells[].bg_std_c` | number | Spread of the background temperature in the cell |
| `cells[].detected` | boolean | Whether the detector fired in this cell |
| `cells[].strength` | number | Detector score from 0 to 1 |

In the table demo, `range_m` is at table scale, and the grid is drawn on the map from an origin and a cell size set in config.

## Calibration fields

| Field | Meaning |
| --- | --- |
| `coef` | Logistic curve. `pod = sigmoid(b0 + b_range * range_m + b_contrast * (target_c - bg_median_c) + b_std * bg_std_c)` |
| `target_c` | Measured temperature of the target used in the trials |
| `false_alarm_rate` | Chance of a detection in an empty cell |
| `trials` | Number of trials behind the fit |

The example coefficients are placeholders. A person in water reads only about 2 °C above the sea (see `../data/literature/`), so `target_c` has to come from what we actually measure, never a fixed 37 °C.

## Cell state fields

| Field | Meaning |
| --- | --- |
| `status` | One of `NOT_SEARCHED`, `CLEARED`, `SEARCHED_NOT_TRUSTED`, `POSSIBLE_DETECTION` |
| `prob` | Probability that the person is in this cell |
| `cum_pod` | Combined chance that the passes so far would have seen the person here |
| `last_seen_ts` | Time of the last pass over this cell |
| `reason` | Short text for the re-search list, for example `warm background, likely glare` |
