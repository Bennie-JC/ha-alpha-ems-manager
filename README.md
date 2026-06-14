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

### Checking learning progress

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
