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

# --- PV learning --------------------------------------------------------------

# Meteorological seasons used for season-aware PV correction factors.
SEASONS: Final = ("winter", "spring", "summer", "autumn")

# Exponential moving average weight for the PV forecast correction factor.
PV_FACTOR_ALPHA: Final = 0.2

# Clamp range for a single day's Solcast forecast error factor
# (actual / forecast).
PV_FACTOR_MIN: Final = 0.50
PV_FACTOR_MAX: Final = 1.20

# Number of learned PV days targeted for full PV learning confidence.
PV_CONFIDENCE_DAYS_TARGET: Final = 30
PV_CONFIDENCE_UPDATES_TARGET: Final = 200

# --- Reserve learning ---------------------------------------------------------

# Nominal usable battery capacity (kWh) used for the protective reserve floor.
BATTERY_CAPACITY_KWH: Final = 22.8

# The reserve must never drop below 10% of capacity.
BATTERY_FLOOR_FACTOR: Final = 0.10
BATTERY_FLOOR_KWH: Final = BATTERY_CAPACITY_KWH * BATTERY_FLOOR_FACTOR  # 2.28 kWh

# Extra headroom above the floor that defines a "safe" day for success learning.
RESERVE_SUCCESS_MARGIN_KWH: Final = 2.0

# Exponential moving average weight for the reserve correction factor.
RESERVE_FACTOR_ALPHA: Final = 0.1

# A reserve miss pushes the factor up toward this multiple of its current value.
RESERVE_MISS_TARGET_MULTIPLIER: Final = 1.10

# Correction-factor bounds. It never drops below neutral and is capped at 2.0.
RESERVE_FACTOR_MIN: Final = 1.0
RESERVE_FACTOR_MAX: Final = 2.0

# Distinct learned days targeted for a mature reserve correction.
RESERVE_CONFIDENCE_DAYS_TARGET: Final = 30

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

# EV charger.
CONF_EV_CHARGER_POWER_SENSOR: Final = "ev_charger_power_sensor"

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
    CONF_EV_CHARGER_POWER_SENSOR,
)

# --- Trade Prediction Engine --------------------------------------------------

# Hard-coded entity for the minimum-spread input helper configured by the user.
MINIMUM_SPREAD_ENTITY: Final = "input_number.minimum_spread_helper"

# Default battery parameters used by the trade engine.
TRADE_BATTERY_CAPACITY_KWH: Final = 22.8
TRADE_CHARGE_EFFICIENCY: Final = 0.95
TRADE_DISCHARGE_EFFICIENCY: Final = 0.95
TRADE_ROUNDTRIP_EFFICIENCY: Final = 0.90
TRADE_MAX_CHARGE_POWER_KW: Final = 10.0
TRADE_MAX_DISCHARGE_POWER_KW: Final = 10.0

# Learning model target for trade prediction confidence.
TRADE_CONFIDENCE_DAYS_TARGET: Final = 30

# Dutch daylight slot bounds for PV distribution (slot = hour*4 + minute//15).
PV_DAYLIGHT_START_SLOT: Final = 24   # 06:00
PV_DAYLIGHT_END_SLOT: Final = 84     # 21:00
