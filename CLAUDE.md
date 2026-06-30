# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Alpha EMS Manager** is a Home Assistant custom integration (v0.1.0) that builds a self-learning Energy Management System for AlphaESS battery installations. It learns household load profiles, corrects Solcast PV forecasts, and calculates battery reserve requirements — currently **advisory only** (no write commands to AlphaESS yet).

## Development Workflow

There is no build system, test suite, or CI/CD pipeline. Development is done by:

1. Copying `custom_components/alpha_ems_manager/` into Home Assistant's `config/custom_components/`
2. Restarting Home Assistant
3. Adding the integration via **Settings → Devices & Services → Add Integration**

Enable debug logging during development:
```yaml
# configuration.yaml
logger:
  default: warning
  logs:
    custom_components.alpha_ems_manager: debug
```

To add tests in the future, the standard HA testing library is `pytest-homeassistant-custom-component`.

## Architecture

### Key Files

| File | Role |
|---|---|
| `coordinator.py` (~1125 lines) | All learning logic, data aggregation, HA store persistence |
| `sensor.py` (~381 lines) | 20+ sensor entities reading from coordinator data |
| `const.py` | Learning parameters (α values), thresholds, config keys, entity defaults |
| `config_flow.py` | UI config + options flow (voluptuous schema, entity selectors) |
| `__init__.py` | Entry lifecycle: setup, quarter-hour scheduling, unload |
| `binary_sensor.py` | Single `reserve_satisfied` binary sensor |
| `diagnostics.py` | Full model state export for user debugging |

### Three Learning Models (all in `coordinator.py`)

**`LearningModel` — House load profiling**
- Reads a cumulative daily kWh sensor (resets at midnight)
- Fires at :00/:15/:30/:45 via `async_track_time_change`
- Computes `raw_delta = current − previous`, rejects negatives (midnight reset) and spikes >10 kWh
- Distributes delta across all missed slots (handles late readings by splitting evenly)
- Updates global 96-slot profile + per-`season_daytype` profile using EMA (α=0.25)

**`PvLearningModel` — Solcast forecast correction**
- At day rollover: computes `error_factor = actual_pv / solcast_forecast` (clamped 0.50–1.20)
- Folds into global and per-season correction factors using EMA (α=0.2)
- Produces corrected forecasts: `forecast × global_factor × season_factor`

**`ReserveLearningModel` — Reserve correction**
- Tracks daily battery minimum; at rollover judges miss (hit floor) or success (stayed above floor + 2 kWh)
- At most one observation per calendar day
- Nudges correction factor using EMA (α=0.1), bounded 1.0–2.0

### Data Flow

```
Source HA entities
    ↓
async_track_time_change → quarter-hour trigger
    ↓
async_request_refresh() → _async_update_data()
    ↓
_learn_house_load() + _learn_pv() + _learn_reserve()
    ↓
Persist to HA Store if changed
    ↓
coordinator.data dict → sensors/binary sensors
```

### Persistence

All model state lives in HA's built-in `Store` (JSON files in `.storage/`). Models are loaded once at setup (`async_load_store()`) and saved after each meaningful change (`async_save_store()`). The `from_dict()` methods on each model handle backward-compatible deserialization.

## Important Constants (`const.py`)

| Constant | Value | Purpose |
|---|---|---|
| `LEARNING_ALPHA` | 0.25 | Load EMA — adapts in ~4 updates |
| `PV_FACTOR_ALPHA` | 0.2 | PV EMA — slower, more stable |
| `RESERVE_FACTOR_ALPHA` | 0.1 | Reserve EMA — most conservative |
| `CONFIDENCE_TARGET_DAYS` | 30 | Days needed for full load confidence |
| `CONFIDENCE_TARGET_UPDATES` | 200 | Updates needed for full load confidence |
| `LEARNING_INTERVALS` | 96 | Quarter-hour slots per day |
| `BATTERY_FLOOR_KWH` | 2.28 | 10% of 22.8 kWh default capacity |
| `RESERVE_SUCCESS_MARGIN_KWH` | 2.0 | Buffer above floor to count as success |

## Reserve Calculation Formula

```
safety_margin    = predicted_remaining_load × (1 − confidence/100) × 0.25
required_reserve = predicted_remaining_load − expected_remaining_pv_today + safety_margin
```

Clamped to [0, battery_capacity]. Recommendation: `hold` if `battery_energy > required_reserve`, else `charge`.

## Home Assistant Patterns Used

- `DataUpdateCoordinator` — centralized async data hub
- `async_track_time_change(hass, callback, minute=[0,15,30,45])` — wall-clock triggering
- `Store` — persistent JSON storage (not external DB)
- `CoordinatorEntity` — all sensors/binary sensors are coordinator-backed
- `SensorEntityDescription` with `value_fn` and `attributes_fn` callables
- `async_setup_entry` / `async_unload_entry` — standard entry lifecycle
- `_attr_has_entity_name = True` — entity name separate from device name in UI

## Debugging Sensors

Key sensor attributes for diagnosing learning:
- **Profile status**: `source_value`, `last_raw_delta`, `distributed_slots`, `learned_slots_count`, `season`, `day_type`
- **PV profile status**: `global_pv_factor`, `season_pv_factor`, `actual_pv_today`, `corrected_forecast_today`
- **Reserve correction factor**: `reserve_learning_days`, `reserve_miss_count`, `day_min_battery_energy`

Full model state is also available via the diagnostics endpoint.
