"""Constants for the Alpha EMS Manager integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "alpha_ems_manager"
NAME: Final = "Alpha EMS Manager"
VERSION: Final = "0.1.0"

# Platforms provided by this integration.
PLATFORMS: Final = ["sensor", "binary_sensor"]

# How often the coordinator refreshes its data. Learning works on quarter-hour
# (15-minute) load deltas, so the coordinator runs once per 15-minute slot.
DEFAULT_SCAN_INTERVAL: Final = timedelta(minutes=15)

# Maximum plausible household consumption in a single 15-minute slot (kWh).
# Deltas above this are treated as invalid spikes and ignored.
MAX_QUARTER_DELTA_KWH: Final = 10.0

# Persistent storage (learned data).
STORAGE_VERSION: Final = 1
STORAGE_KEY: Final = f"{DOMAIN}_learning"

# Learning granularity: number of 15-minute intervals in a day (24 * 4).
INTERVAL_MINUTES: Final = 15
INTERVALS_PER_DAY: Final = 96

# Exponential moving average weight applied when learning new observations.
# A small alpha favours historical stability; a larger alpha adapts faster.
LEARNING_ALPHA: Final = 0.25

# Profile keys. A single "global" profile aggregates everything; season/day-type
# profiles are maintained alongside it for future, more granular planning.
GLOBAL_PROFILE_KEY: Final = "global"

# Learning confidence model targets.
CONFIDENCE_DAYS_TARGET: Final = 30
CONFIDENCE_UPDATES_TARGET: Final = 200
# Minimum learned slots before predictions are considered reliable.
MIN_CONFIDENT_SLOTS: Final = 12

# --- Configuration keys -------------------------------------------------------

# Household load (cumulative, resets daily).
CONF_CUMULATIVE_HOUSE_LOAD_SENSOR: Final = "cumulative_house_load_sensor"

# PV production.
CONF_PV_ACTUAL_TODAY_SENSOR: Final = "pv_actual_today_sensor"
CONF_PV_FORECAST_TODAY_SENSOR: Final = "pv_forecast_today_sensor"
CONF_PV_FORECAST_TOMORROW_SENSOR: Final = "pv_forecast_tomorrow_sensor"
CONF_PV_EAST_SENSOR: Final = "pv_east_sensor"
CONF_PV_WEST_SENSOR: Final = "pv_west_sensor"

# Frank dynamic prices.
CONF_FRANK_PRICES_TODAY_SENSOR: Final = "frank_prices_today_sensor"
CONF_FRANK_PRICES_TOMORROW_SENSOR: Final = "frank_prices_tomorrow_sensor"
CONF_FRANK_CHEAPEST_TIME_TODAY_SENSOR: Final = "frank_cheapest_time_today_sensor"
CONF_FRANK_MOST_EXPENSIVE_TIME_TODAY_SENSOR: Final = (
    "frank_most_expensive_time_today_sensor"
)
CONF_FRANK_CHEAPEST_TIME_TOMORROW_SENSOR: Final = "frank_cheapest_time_tomorrow_sensor"
CONF_FRANK_MOST_EXPENSIVE_TIME_TOMORROW_SENSOR: Final = (
    "frank_most_expensive_time_tomorrow_sensor"
)

# Battery.
CONF_BATTERY_CURRENT_KWH_SENSOR: Final = "battery_current_kwh_sensor"
CONF_BATTERY_CAPACITY_KWH_ENTITY: Final = "battery_capacity_kwh_entity"
CONF_BATTERY_SOC_SENSOR: Final = "battery_soc_sensor"

# Default entity ids used to pre-populate the config flow.
DEFAULTS: Final = {
    CONF_CUMULATIVE_HOUSE_LOAD_SENSOR: "sensor.alphaess_today_s_house_load",
    CONF_PV_ACTUAL_TODAY_SENSOR: "sensor.alphaess_today_s_energy_from_pv",
    CONF_PV_FORECAST_TODAY_SENSOR: "sensor.solcast_pv_forecast_forecast_today",
    CONF_PV_FORECAST_TOMORROW_SENSOR: "sensor.solcast_pv_forecast_forecast_tomorrow",
    CONF_PV_EAST_SENSOR: "sensor.achterkant",
    CONF_PV_WEST_SENSOR: "sensor.voorkant",
    CONF_FRANK_PRICES_TODAY_SENSOR: "sensor.frank_prices_today",
    CONF_FRANK_PRICES_TOMORROW_SENSOR: "sensor.frank_prices_tomorrow",
    CONF_FRANK_CHEAPEST_TIME_TODAY_SENSOR: "sensor.frank_cheapest_time_today",
    CONF_FRANK_MOST_EXPENSIVE_TIME_TODAY_SENSOR: "sensor.frank_most_expensive_time_today",
    CONF_FRANK_CHEAPEST_TIME_TOMORROW_SENSOR: "sensor.frank_cheapest_time_tomorrow",
    CONF_FRANK_MOST_EXPENSIVE_TIME_TOMORROW_SENSOR: "sensor.frank_most_expensive_time_tomorrow",
    CONF_BATTERY_SOC_SENSOR: "sensor.alphaess_soc_battery",
}

# Required configuration keys.
REQUIRED_KEYS: Final = (
    CONF_CUMULATIVE_HOUSE_LOAD_SENSOR,
    CONF_PV_ACTUAL_TODAY_SENSOR,
    CONF_PV_FORECAST_TODAY_SENSOR,
    CONF_PV_FORECAST_TOMORROW_SENSOR,
    CONF_FRANK_PRICES_TODAY_SENSOR,
    CONF_FRANK_PRICES_TOMORROW_SENSOR,
    CONF_FRANK_CHEAPEST_TIME_TODAY_SENSOR,
    CONF_FRANK_MOST_EXPENSIVE_TIME_TODAY_SENSOR,
    CONF_FRANK_CHEAPEST_TIME_TOMORROW_SENSOR,
    CONF_FRANK_MOST_EXPENSIVE_TIME_TOMORROW_SENSOR,
    CONF_BATTERY_CURRENT_KWH_SENSOR,
    CONF_BATTERY_CAPACITY_KWH_ENTITY,
)

# Optional configuration keys.
OPTIONAL_KEYS: Final = (
    CONF_PV_EAST_SENSOR,
    CONF_PV_WEST_SENSOR,
    CONF_BATTERY_SOC_SENSOR,
)
