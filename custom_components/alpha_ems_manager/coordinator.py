"""Data update coordinator for Alpha EMS Manager.

The coordinator is responsible for:

* Reading the configured source sensors (house load, PV, prices, battery).
* Learning the household load profile per 15-minute interval, bucketed by
  season and by weekday/weekend.
* Persisting the learned profile so it survives restarts.
* Combining the learned load with PV forecasts and battery state to estimate
  the reserve energy required between the next sell window and the next buy
  window.

This is a scaffold: the learning and reserve maths are intentionally simple and
self-contained so they can be refined later. No AlphaESS write commands are
performed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BATTERY_CAPACITY_KWH_ENTITY,
    CONF_BATTERY_CURRENT_KWH_SENSOR,
    CONF_BATTERY_SOC_SENSOR,
    CONF_CUMULATIVE_HOUSE_LOAD_SENSOR,
    CONF_FRANK_CHEAPEST_TIME_TODAY_SENSOR,
    CONF_FRANK_CHEAPEST_TIME_TOMORROW_SENSOR,
    CONF_FRANK_MOST_EXPENSIVE_TIME_TODAY_SENSOR,
    CONF_FRANK_MOST_EXPENSIVE_TIME_TOMORROW_SENSOR,
    CONF_FRANK_PRICES_TODAY_SENSOR,
    CONF_FRANK_PRICES_TOMORROW_SENSOR,
    CONF_PV_ACTUAL_TODAY_SENSOR,
    CONF_PV_EAST_SENSOR,
    CONF_PV_FORECAST_TODAY_SENSOR,
    CONF_PV_FORECAST_TOMORROW_SENSOR,
    CONF_PV_WEST_SENSOR,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    INTERVAL_MINUTES,
    INTERVALS_PER_DAY,
    LEARNING_ALPHA,
    MAX_QUARTER_DELTA_KWH,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


def _season_for(moment: datetime) -> str:
    """Return a coarse meteorological season label for the given moment."""
    month = moment.month
    if month in (12, 1, 2):
        return "winter"
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    return "autumn"


def _day_type_for(moment: datetime) -> str:
    """Return 'weekend' for Saturday/Sunday, otherwise 'weekday'."""
    return "weekend" if moment.weekday() >= 5 else "weekday"


def _interval_index(moment: datetime) -> int:
    """Return the 0-based 15-minute interval index within the day."""
    return (moment.hour * 60 + moment.minute) // INTERVAL_MINUTES


def _profile_key(season: str, day_type: str) -> str:
    """Return the storage key for a season/day-type profile."""
    return f"{season}_{day_type}"


@dataclass
class LearningModel:
    """Container for learned per-interval load profiles.

    ``profiles`` maps a ``"{season}_{day_type}"`` key to a list of
    ``INTERVALS_PER_DAY`` floats, each representing the learned energy (kWh)
    consumed during that 15-minute interval.
    """

    profiles: dict[str, list[float]] = field(default_factory=dict)
    # Last observed cumulative house-load reading, used to derive interval deltas.
    last_cumulative_value: float | None = None
    last_interval_index: int | None = None
    # Diagnostics / debug bookkeeping.
    last_delta: float | None = None
    last_slot: int | None = None
    last_update: str | None = None
    update_count: int = 0

    def profile(self, season: str, day_type: str) -> list[float]:
        """Return (creating if needed) the profile for a season/day-type."""
        key = _profile_key(season, day_type)
        if key not in self.profiles:
            self.profiles[key] = [0.0] * INTERVALS_PER_DAY
        return self.profiles[key]

    def to_dict(self) -> dict[str, Any]:
        """Serialise the model for persistent storage."""
        return {
            "profiles": self.profiles,
            "last_cumulative_value": self.last_cumulative_value,
            "last_interval_index": self.last_interval_index,
            "last_delta": self.last_delta,
            "last_slot": self.last_slot,
            "last_update": self.last_update,
            "update_count": self.update_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> LearningModel:
        """Restore a model from persistent storage."""
        if not data:
            return cls()
        return cls(
            profiles=data.get("profiles", {}),
            last_cumulative_value=data.get("last_cumulative_value"),
            last_interval_index=data.get("last_interval_index"),
            last_delta=data.get("last_delta"),
            last_slot=data.get("last_slot"),
            last_update=data.get("last_update"),
            update_count=data.get("update_count", 0),
        )


class AlphaEmsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate state reading, learning and reserve calculation."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.entry = entry
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.model = LearningModel()

    # -- Configuration helpers -------------------------------------------------

    def _config(self, key: str) -> str | None:
        """Return a configured entity id, preferring options over data."""
        return self.entry.options.get(key, self.entry.data.get(key))

    def _state(self, key: str) -> State | None:
        """Return the Home Assistant state object for a configured entity."""
        entity_id = self._config(key)
        if not entity_id:
            return None
        return self.hass.states.get(entity_id)

    def _float(self, key: str) -> float | None:
        """Return a configured entity's numeric state, or None if unavailable."""
        state = self._state(key)
        if state is None or state.state in (None, "", "unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    # -- Persistent storage ----------------------------------------------------

    async def async_load_store(self) -> None:
        """Load the learned model from disk."""
        stored = await self._store.async_load()
        self.model = LearningModel.from_dict(stored)

    async def async_save_store(self) -> None:
        """Persist the learned model to disk."""
        await self._store.async_save(self.model.to_dict())

    # -- Learning --------------------------------------------------------------

    def _learn_house_load(self, now: datetime) -> bool:
        """Update the learned profile from the cumulative house-load sensor.

        The source sensor is cumulative and resets daily. On every update we
        compute the delta since the previous reading and, when valid, fold it
        into the current quarter-hour slot using an exponential moving average.

        Returns ``True`` when the learned model changed and should be persisted.
        """
        source_entity = self._config(CONF_CUMULATIVE_HOUSE_LOAD_SENSOR)
        cumulative = self._float(CONF_CUMULATIVE_HOUSE_LOAD_SENSOR)

        self.model.update_count += 1
        self.model.last_update = now.isoformat()

        _LOGGER.debug(
            "House-load source %s read value=%s (update #%s)",
            source_entity,
            cumulative,
            self.model.update_count,
        )

        if cumulative is None:
            _LOGGER.debug(
                "House-load source %s unavailable; skipping learning",
                source_entity,
            )
            return True

        slot = _interval_index(now)
        previous = self.model.last_cumulative_value

        # First reading after start/reset: only establish a baseline.
        if previous is None:
            self.model.last_cumulative_value = cumulative
            self.model.last_interval_index = slot
            _LOGGER.debug(
                "Established house-load baseline=%.3f kWh at slot %s",
                cumulative,
                slot,
            )
            return True

        delta = cumulative - previous
        _LOGGER.debug(
            "House-load delta calculated: %.3f - %.3f = %.3f kWh at slot %s",
            cumulative,
            previous,
            delta,
            slot,
        )

        # A negative delta means the daily counter reset at midnight: rebase
        # without recording negative consumption.
        if delta < 0:
            self.model.last_cumulative_value = cumulative
            self.model.last_interval_index = slot
            _LOGGER.debug(
                "Negative delta (%.3f kWh) treated as midnight reset; rebased "
                "baseline to %.3f kWh",
                delta,
                cumulative,
            )
            return True

        # An implausibly large delta is treated as a spike and ignored, but the
        # baseline is still advanced so the next delta is sensible.
        if delta > MAX_QUARTER_DELTA_KWH:
            self.model.last_cumulative_value = cumulative
            self.model.last_interval_index = slot
            _LOGGER.debug(
                "Delta %.3f kWh exceeds spike limit %.1f kWh; ignoring sample",
                delta,
                MAX_QUARTER_DELTA_KWH,
            )
            return True

        # Valid sample: advance baseline and fold the delta into the slot.
        self.model.last_cumulative_value = cumulative
        self.model.last_interval_index = slot
        self.model.last_delta = round(delta, 3)
        self.model.last_slot = slot

        season = _season_for(now)
        day_type = _day_type_for(now)
        profile = self.model.profile(season, day_type)

        previous_slot_value = profile[slot]
        if previous_slot_value == 0.0:
            profile[slot] = delta
        else:
            profile[slot] = (
                LEARNING_ALPHA * delta + (1 - LEARNING_ALPHA) * previous_slot_value
            )

        _LOGGER.debug(
            "Slot %s updated for %s_%s: %.3f -> %.3f kWh (delta=%.3f)",
            slot,
            season,
            day_type,
            previous_slot_value,
            profile[slot],
            delta,
        )
        return True

    def _learned_slots_count(self, now: datetime) -> int:
        """Return the number of non-zero learned slots for the active profile."""
        profile = self.model.profile(_season_for(now), _day_type_for(now))
        return sum(1 for value in profile if value > 0.0)

    def _predicted_daily_load(self, now: datetime) -> float:
        """Return the learned total load (kWh): sum of the 96 slot values."""
        profile = self.model.profile(_season_for(now), _day_type_for(now))
        return round(sum(profile), 3)

    def _predicted_remaining_load(self, now: datetime) -> float:
        """Return learned load (kWh) from the current slot to end of day."""
        profile = self.model.profile(_season_for(now), _day_type_for(now))
        index = _interval_index(now)
        return round(sum(profile[index:]), 3)

    # -- Reserve calculation ---------------------------------------------------

    def _required_reserve(self, now: datetime) -> float:
        """Estimate reserve energy (kWh) needed until the next buy window.

        Kept intentionally simple for now: the reserve equals the learned load
        still expected for the rest of today, clamped to the battery capacity.
        PV/price-window awareness will refine this later. Because it is derived
        directly from the learned remaining load, it is non-zero whenever any
        load has been learned.
        """
        remaining_load = self._predicted_remaining_load(now)

        capacity = self._float(CONF_BATTERY_CAPACITY_KWH_ENTITY)
        reserve = max(remaining_load, 0.0)
        if capacity is not None:
            reserve = min(reserve, capacity)
        return round(reserve, 3)

    # -- Coordinator update ----------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Read sources, learn, and compute derived values."""
        now = dt_util.now()

        changed = self._learn_house_load(now)

        # Persist learned data after each learning cycle.
        if changed:
            await self.async_save_store()
            _LOGGER.debug(
                "Learned model saved to store (%s profiles, %s updates)",
                len(self.model.profiles),
                self.model.update_count,
            )

        battery_current = self._float(CONF_BATTERY_CURRENT_KWH_SENSOR)
        battery_capacity = self._float(CONF_BATTERY_CAPACITY_KWH_ENTITY)
        battery_soc = self._float(CONF_BATTERY_SOC_SENSOR)

        required_reserve = self._required_reserve(now)
        reserve_satisfied = (
            battery_current is not None and battery_current >= required_reserve
        )

        return {
            "season": _season_for(now),
            "day_type": _day_type_for(now),
            "interval_index": _interval_index(now),
            "intervals_per_day": INTERVALS_PER_DAY,
            "predicted_daily_load_kwh": self._predicted_daily_load(now),
            "predicted_remaining_load_kwh": self._predicted_remaining_load(now),
            "pv_forecast_today_kwh": self._float(CONF_PV_FORECAST_TODAY_SENSOR),
            "pv_forecast_tomorrow_kwh": self._float(CONF_PV_FORECAST_TOMORROW_SENSOR),
            "pv_actual_today_kwh": self._float(CONF_PV_ACTUAL_TODAY_SENSOR),
            "pv_east_kwh": self._float(CONF_PV_EAST_SENSOR),
            "pv_west_kwh": self._float(CONF_PV_WEST_SENSOR),
            "frank_price_today": self._float(CONF_FRANK_PRICES_TODAY_SENSOR),
            "frank_price_tomorrow": self._float(CONF_FRANK_PRICES_TOMORROW_SENSOR),
            "frank_cheapest_time_today": self._raw_state(
                CONF_FRANK_CHEAPEST_TIME_TODAY_SENSOR
            ),
            "frank_most_expensive_time_today": self._raw_state(
                CONF_FRANK_MOST_EXPENSIVE_TIME_TODAY_SENSOR
            ),
            "frank_cheapest_time_tomorrow": self._raw_state(
                CONF_FRANK_CHEAPEST_TIME_TOMORROW_SENSOR
            ),
            "frank_most_expensive_time_tomorrow": self._raw_state(
                CONF_FRANK_MOST_EXPENSIVE_TIME_TOMORROW_SENSOR
            ),
            "battery_current_kwh": battery_current,
            "battery_capacity_kwh": battery_capacity,
            "battery_soc": battery_soc,
            "required_reserve_kwh": required_reserve,
            "reserve_satisfied": reserve_satisfied,
            "learned_profiles": len(self.model.profiles),
            # Diagnostic / debug fields surfaced on the predicted-load sensor.
            "source_entity": self._config(CONF_CUMULATIVE_HOUSE_LOAD_SENSOR),
            "source_value": self._float(CONF_CUMULATIVE_HOUSE_LOAD_SENSOR),
            "last_house_load": self.model.last_cumulative_value,
            "last_delta": self.model.last_delta,
            "last_slot": self.model.last_slot,
            "learned_slots_count": self._learned_slots_count(now),
            "update_count": self.model.update_count,
            "last_update": self.model.last_update,
        }

    def _raw_state(self, key: str) -> str | None:
        """Return the raw string state of a configured entity."""
        state = self._state(key)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        return state.state
