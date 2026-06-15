# ha-alpha-ems-manager

Self-learning Energy Management System for AlphaESS and Home Assistant.

**Alpha EMS Manager** is a learning EMS integration for AlphaESS battery
management in Home Assistant. It learns household load per 15-minute interval
from the AlphaESS cumulative house-load sensor, builds seasonal and
weekday/weekend patterns, combines Solcast PV forecasts, Frank dynamic quarter
prices and battery state, and estimates the reserve energy required between the
next sell window and the next buy window.

> **Status:** early scaffold (v0.1.0). It learns, forecasts and recommends, but
> does **not** send any write/control commands to AlphaESS yet.

## Features

- 🧠 Self-learning household load profile per 15-minute interval
  (96 intervals/day), bucketed by season and weekday/weekend.
- ☀️ Integrates Solcast PV forecasts (today/tomorrow, optional east/west).
- 💶 Reads Frank dynamic prices and cheapest/most-expensive time windows.
- 🔋 Tracks battery current energy, capacity and (optional) state of charge.
- 📐 Scaffold reserve calculation combining learned load and remaining PV.
- 💾 Learned data persists across restarts.
- 🔧 Built on `DataUpdateCoordinator` with a UI config flow + options flow.
- 📊 Diagnostics support.

## Installation

### HACS (recommended)

1. In HACS, add this repository as a custom repository (category: *Integration*).
2. Install **Alpha EMS Manager**.
3. Restart Home Assistant.
4. Go to **Settings → Devices & Services → Add Integration** and search for
   *Alpha EMS Manager*.

### Manual

1. Copy `custom_components/alpha_ems_manager` into your Home Assistant
   `config/custom_components` directory.
2. Restart Home Assistant.
3. Add the integration from the UI.

## Configuration

The config flow asks for the following entities:

| Field | Required | Example |
| --- | --- | --- |
| Cumulative house load sensor | ✅ | `sensor.alphaess_today_s_house_load` |
| PV actual today sensor | ✅ | `sensor.alphaess_today_s_energy_from_pv` |
| PV forecast today sensor | ✅ | `sensor.solcast_pv_forecast_forecast_today` |
| PV forecast tomorrow sensor | ✅ | `sensor.solcast_pv_forecast_forecast_tomorrow` |
| PV east sensor | ⬜ | `sensor.achterkant` |
| PV west sensor | ⬜ | `sensor.voorkant` |
| Frank prices today sensor | ✅ | `sensor.frank_prices_today` |
| Frank prices tomorrow sensor | ✅ | `sensor.frank_prices_tomorrow` |
| Frank cheapest time today sensor | ✅ | `sensor.frank_cheapest_time_today` |
| Frank most expensive time today sensor | ✅ | `sensor.frank_most_expensive_time_today` |
| Frank cheapest time tomorrow sensor | ✅ | `sensor.frank_cheapest_time_tomorrow` |
| Frank most expensive time tomorrow sensor | ✅ | `sensor.frank_most_expensive_time_tomorrow` |
| Battery current energy (kWh) sensor | ✅ | — |
| Battery capacity (kWh) entity | ✅ | — |
| Battery state of charge sensor | ⬜ | `sensor.alphaess_soc_battery` |

You can change these later from the integration's **Configure** (options) screen.

## Entities

### Sensors

- `Predicted daily load` — learned total load for today (sum of 96 global slots).
- `Predicted remaining load` — learned load from now until end of day.
- `Required reserve` — estimated reserve energy needed until the next buy window.
- `PV forecast today` / `PV forecast tomorrow`.
- `Battery current energy`.
- `Recommendation` — `hold` or `charge` (advisory only).
- `Learning confidence` — 0–100% estimate of how trustworthy the profile is.
- `Learning days` — number of distinct days with learned data.
- `Learned slots count` — number of the 96 quarter-hour slots learned so far.
- `Last quarter load` — energy attributed to the most recent quarter (kWh).
- `Profile status` — lifecycle label (`learning` / `improving` / `ready`) with
  full diagnostic attributes (see below).
- `PV correction factor` — the effective Solcast correction (`global × season`).
- `Corrected PV forecast today` / `Corrected PV forecast tomorrow` (kWh).
- `Expected remaining PV today` — corrected forecast minus actual PV so far.
- `PV learning confidence` — 0–100% estimate of PV correction trustworthiness.
- `PV profile status` — PV lifecycle label with full PV diagnostic attributes.

### Binary sensors

- `Reserve satisfied` — whether current battery energy meets the required reserve.

## How learning works

Alpha EMS Manager learns from a **cumulative daily house-load sensor**
(e.g. `sensor.alphaess_today_s_house_load`) that counts up through the day and
resets to 0 at midnight. Every quarter-hour boundary (:00, :15, :30, :45) the
integration:

1. Reads the current cumulative value.
2. Computes `raw_delta = current − previous`.
3. Ignores invalid samples — a negative delta is treated as the midnight reset,
   and a delta above 10 kWh per 15 minutes is treated as a spike.
4. Distributes the delta across **every missed quarter slot** (so a value that
   arrives late is spread over the slots it actually covers, not dumped into
   one).
5. Folds the per-slot value into both a `global` profile and the current
   `season_daytype` profile using an exponential moving average.

### How long until predictions are useful?

| Elapsed learning | What to expect |
| --- | --- |
| **First day** | Values are low and partial — only the slots seen so far are learned, so `Predicted daily load` will be well below your real daily total. This is normal. |
| **1 full day** | Basic, rough prediction for the slots that occurred. |
| **~7 days** | Usable prediction for typical days. |
| **~30 days** | Stable prediction; confidence climbs toward its target. |
| **90+ days** | Seasonal patterns start to separate (per season/day-type profiles). |
| **365 days** | Full seasonal learning across the whole year. |

Because learning is incremental, the **first day always shows low values** — the
profile only contains the quarter-hours observed so far, and confidence stays
low until enough slots and days accumulate.

## How PV learning works

In parallel with house-load learning, Alpha EMS Manager runs a second,
self-learning system that **corrects the Solcast PV forecast** using your own
measured production.

### How Solcast correction works

1. Throughout the day the integration tracks the latest *actual PV today*
   (`sensor.alphaess_today_s_energy_from_pv`) and the *Solcast forecast today*.
2. At midnight (day rollover) it finalises the completed day and computes the
   forecast error factor:

   ```
   error_factor = actual_today / forecast_today
   ```

3. The factor is clamped to a sane range (`0.50 … 1.20`) to reject outliers,
   then folded into a **global** factor and a **per-season** factor with an
   exponential moving average:

   ```
   new_factor = old_factor × 0.80 + error_factor × 0.20
   ```

4. Corrected forecasts are then produced on every update:

   ```
   corrected_forecast_today    = forecast_today    × global_factor × season_factor
   corrected_forecast_tomorrow = forecast_tomorrow × global_factor × season_factor
   ```

5. The PV still expected for the rest of today is:

   ```
   expected_remaining_pv_today = max(corrected_forecast_today − actual_pv_today, 0)
   ```

Both factors start neutral at `1.0`, so before any learning the corrected
forecast equals the raw Solcast forecast.

### How reserve is calculated

The reserve only covers the **remaining** part of the day, so it uses
`predicted_remaining_load` (never the full daily load) and subtracts the PV that
is still expected:

```
safety_margin   = predicted_remaining_load × (1 − confidence/100) × 0.25
required_reserve = predicted_remaining_load − expected_remaining_pv_today + safety_margin
```

`required_reserve` is clamped between `0` and the battery capacity. The
recommendation is then simply:

- `hold` when `battery_current_energy > required_reserve`
- `charge` when `battery_current_energy < required_reserve`

Reserve learning is evaluated **once per day**, not every quarter-hour. The
integration tracks the day's minimum battery energy and, at the day rollover,
records at most one miss (day minimum touched the floor) or one success (day
minimum stayed above `floor + 2.0 kWh`) for the finished day, nudging the
reserve correction factor accordingly.

### How PV confidence grows

PV confidence grows with the number of distinct learned days (and update count),
so it follows the same maturing curve as house-load learning:

| Elapsed PV learning | What to expect |
| --- | --- |
| **Day 1** | Rough estimate — corrected forecast ≈ raw Solcast forecast. |
| **~7 days** | Reasonable correction of systematic forecast bias. |
| **~30 days** | Stable correction factor. |
| **90+ days** | Season-aware correction (per-season factors diverge). |

### Checking PV learning progress

Open the **PV profile status** sensor and inspect its attributes:

- `actual_pv_today`, `raw_forecast_today`, `raw_forecast_tomorrow`
- `corrected_forecast_today`, `corrected_forecast_tomorrow`
- `expected_remaining_pv_today`
- `global_pv_factor`, `season_pv_factor`
- `last_pv_error`, `last_pv_error_factor`, `pv_learning_days`
- `season`, `storage_loaded`, `storage_saved`, `last_update`

### Checking house-load learning progress

Open the **Profile status** sensor and inspect its attributes:

- `source_entity`, `source_value`, `current_house_load`, `previous_house_load`
- `last_raw_delta`, `last_delta_per_slot`, `distributed_slots`
- `previous_slot`, `current_slot`, `learned_slots_count`
- `update_count`, `last_update`, `season`, `day_type`, `profile_key`
- `storage_loaded`, `storage_saved`

These show exactly what was read, how the delta was distributed, and whether the
learned data is being persisted.


## Troubleshooting

### Predicted load stays at 0

The integration learns from **quarter-hour deltas** of the cumulative house-load
sensor, so it needs time and valid source data before values appear.

1. **Verify the cumulative house load sensor updates.** Open the entity
   configured as *Cumulative house load sensor* (e.g.
   `sensor.alphaess_today_s_house_load`) and confirm its value increases during
   the day and resets at midnight. If it is `unknown`/`unavailable`, learning is
   skipped.
2. **Wait until at least one 15-minute delta is captured.** The coordinator runs
   on startup to establish a baseline and then every 15 minutes. The first real
   learned value only appears after a second update produces a valid positive
   delta — so allow at least 15–30 minutes after setup.
3. **Inspect the debug attributes.** The *Predicted daily load* sensor exposes
   `source_entity`, `source_value`, `last_house_load`, `last_delta`,
   `last_slot`, `learned_slots_count`, `update_count` and `last_update`. These
   show whether deltas are being read and stored.
4. **Enable debug logging** for the integration and check the logs:

   ```yaml
   # configuration.yaml
   logger:
     default: warning
     logs:
       custom_components.alpha_ems_manager: debug
   ```

   You should see log lines for the source value read, the delta calculated, the
   slot updated and the store being saved.

> Deltas below 0 kWh are treated as the midnight reset (baseline rebased), and
> deltas above 10 kWh per 15 minutes are treated as invalid spikes and ignored.

## Roadmap

- Sell-time/buy-time aware reserve windows using Frank price timestamps.
- Confidence/accuracy metrics for the learned profile.
- Optional AlphaESS control (charge/discharge scheduling) — not in this release.

## Disclaimer

This is an independent project and is not affiliated with AlphaESS, Solcast or
Frank Energie. Use at your own risk.

## License

[MIT](LICENSE) © Bennie-JC
